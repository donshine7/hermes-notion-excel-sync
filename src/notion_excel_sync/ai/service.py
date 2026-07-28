from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Mapping

from notion_excel_sync.ai.cache import AIAnalysisCache
from notion_excel_sync.ai.models import (
    AIAnalysis,
    AIEnrichmentReport,
    AIRequest,
    AISchemaError,
    validate_analysis,
)
from notion_excel_sync.ai.provider import (
    AIProviderError,
    AIProviderUnavailable,
    StructuredAIProvider,
)
from notion_excel_sync.domain.analyzers.common import record_case_numbers
from notion_excel_sync.knowledge.policy import redact_sensitive_text
from notion_excel_sync.models import (
    JsonValue,
    KnowledgeRef,
    ProposedChange,
    ReviewItem,
    SourceRecord,
    sha256_json,
)

if TYPE_CHECKING:
    from notion_excel_sync.adapters.hiworks_mail import HiworksMailMessage
    from notion_excel_sync.domain.router import AnalysisRun


_COMPLEX_PARTY = re.compile(r"[/_]|신규법인|미정|확인\s*필요", re.IGNORECASE)
_PARTY_VALUES = frozenset({"개인", "법인", "기관", "정부지원기관", "기타"})


@dataclass(slots=True)
class StructuredAIService:
    """Selective AI enrichment; failures never block deterministic analysis."""

    provider: StructuredAIProvider
    cache: AIAnalysisCache
    rollout_mode: str = "shadow"
    privacy_mode: str = "local_only"
    max_calls_per_sync: int = 8
    max_mail_calls_per_sync: int = 4
    max_input_chars: int = 12_000
    assist_confidence: float = 0.80
    verified_confidence: float = 0.95
    _calls: int = field(default=0, init=False, repr=False)
    _mail_calls: int = field(default=0, init=False, repr=False)
    _circuit_open: bool = field(default=False, init=False, repr=False)
    _cache_hits: int = field(default=0, init=False, repr=False)
    _failures: int = field(default=0, init=False, repr=False)
    _last_failure_code: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.rollout_mode not in {"shadow", "assist", "verified"}:
            raise ValueError("AI rollout mode must be shadow, assist, or verified")
        if self.privacy_mode not in {"local_only", "redacted_remote"}:
            raise ValueError("AI privacy mode must be local_only or redacted_remote")
        if min(
            self.max_calls_per_sync,
            self.max_mail_calls_per_sync,
            self.max_input_chars,
        ) <= 0:
            raise ValueError("AI limits must be positive")
        if not 0 <= self.assist_confidence <= self.verified_confidence <= 1:
            raise ValueError("AI confidence thresholds are invalid")
        self._calls = 0
        self._mail_calls = 0
        self._circuit_open = False
        self._cache_hits = 0
        self._failures = 0
        self._last_failure_code = None

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def last_failure_code(self) -> str | None:
        return self._last_failure_code

    def analyze_mail_message(
        self,
        message: HiworksMailMessage,
    ) -> AIAnalysis | None:
        if (
            self._circuit_open
            or self._calls >= self.max_calls_per_sync
            or self._mail_calls >= self.max_mail_calls_per_sync
        ):
            return None
        subject = message.header.subject.strip()
        body = message.body_text[: self.max_input_chars]
        sent_at = (
            message.header.sent_at.isoformat()
            if message.header.sent_at is not None
            else None
        )
        corpus = "\n".join(
            item
            for item in (
                f"제목: {subject}" if subject else "",
                f"발송일: {sent_at}" if sent_at else "",
                f"본문: {body}" if body else "",
            )
            if item
        )
        if not corpus:
            return None
        if self.privacy_mode == "redacted_remote":
            safe_subject, _ = redact_sensitive_text(subject)
            safe_body, _ = redact_sensitive_text(body)
            corpus = "\n".join(
                item
                for item in (
                    f"제목: {safe_subject}" if safe_subject else "",
                    f"발송일: {sent_at}" if sent_at else "",
                    f"본문: {safe_body}" if safe_body else "",
                )
                if item
            )
        else:
            safe_subject = subject
            safe_body = body
        source_hash = sha256_json(
            {
                "uidl": message.header.uidl,
                "message_id": message.header.message_id,
                "subject": subject,
                "sent_at": sent_at,
                "body": message.body_text,
            }
        )
        known_cases = tuple(sorted(_case_numbers(corpus)))
        request = AIRequest(
            task_type="email_case_event",
            source_hash=source_hash,
            source_type="hiworks_email",
            source_locator=sha256_json({"uidl": message.header.uidl})[:32],
            payload={
                "subject": safe_subject[:500],
                "body": safe_body,
                "sent_at": (
                    message.header.sent_at.isoformat()
                    if message.header.sent_at is not None
                    else None
                ),
                "attachments": [
                    {
                        "filename": (
                            redact_sensitive_text(item.filename)[0]
                            if self.privacy_mode == "redacted_remote"
                            else item.filename
                        ),
                        "content_type": item.content_type,
                        "size_bytes": item.size_bytes,
                    }
                    for item in message.attachments[:20]
                ],
            },
            evidence_corpus=corpus,
            known_case_numbers=known_cases,
        )
        before = self._calls
        analysis = self._analyze(request)
        if self._calls > before:
            self._mail_calls += 1
        return analysis

    def enrich_excel_runs(
        self,
        runs: Iterable[AnalysisRun],
        *,
        verified_context_by_analyzer: Mapping[
            str, tuple[dict[str, str], ...]
        ] | None = None,
        verified_refs_by_analyzer: Mapping[
            str, tuple[KnowledgeRef, ...]
        ] | None = None,
    ) -> AIEnrichmentReport:
        report = AIEnrichmentReport()
        candidates = sorted(
            (
                (self._priority(run), run)
                for run in runs
                if self._task_for_run(run) is not None
            ),
            key=lambda item: (-item[0], item[1].change.key),
        )
        for _, run in candidates:
            if self._circuit_open or self._calls >= self.max_calls_per_sync:
                report.skipped += 1
                continue
            record = run.change.after or run.change.before
            task_type = self._task_for_run(run)
            if record is None or task_type is None:
                report.skipped += 1
                continue
            request = self._excel_request(
                task_type,
                record,
                run,
                verified_context_by_analyzer or {},
            )
            report.attempted += 1
            calls_before = self._calls
            cache_before = self._cache_hits
            analysis = self._analyze(request)
            if self._cache_hits > cache_before:
                report.cache_hits += 1
            if analysis is None:
                if self._calls > calls_before:
                    report.failures += 1
                    failure_code = self.last_failure_code or "unknown_failure"
                    report.failure_codes[failure_code] = (
                        report.failure_codes.get(failure_code, 0) + 1
                    )
                report.skipped += int(self._calls == calls_before)
                continue
            report.completed += 1
            if self.rollout_mode == "shadow":
                report.shadow_results += 1
                continue
            if analysis.overall_confidence < self.assist_confidence:
                report.skipped += 1
                continue
            replacement = (
                self._verified_party_replacement(run, analysis)
                if self.rollout_mode == "verified"
                and not analysis.needs_review
                and analysis.overall_confidence >= self.verified_confidence
                else None
            )
            if replacement is not None:
                original, proposed = replacement
                report.replaced_operation_ids.add(original.operation_id)
                report.proposed_changes.append(proposed)
                continue
            report.review_items.append(
                self._review_item(
                    record,
                    analysis,
                    run,
                    verified_refs_by_analyzer or {},
                )
            )
        return report

    def _analyze(self, request: AIRequest) -> AIAnalysis | None:
        try:
            cached = self.cache.get(request.request_digest)
        except (OSError, ValueError):
            cached = None
        if cached is not None:
            self._cache_hits += 1
            return cached
        if self._circuit_open or self._calls >= self.max_calls_per_sync:
            return None
        self._calls += 1
        self._last_failure_code = None
        try:
            response = self.provider.analyze(request)
            analysis = validate_analysis(request, response)
            self.cache.put(analysis)
            return analysis
        except AIProviderUnavailable:
            self._record_failure("provider_unavailable")
            return None
        except AIProviderError:
            self._record_failure("provider_response_error")
            return None
        except AISchemaError as exc:
            self._record_failure(_schema_failure_code(exc))
            return None
        except OSError:
            self._record_failure("cache_io_error")
            return None
        except ValueError:
            self._record_failure("validated_value_rejected")
            return None

    def _record_failure(self, code: str) -> None:
        self._last_failure_code = code
        self._failures += 1
        self._circuit_open = True

    def _excel_request(
        self,
        task_type: str,
        record: SourceRecord,
        run: AnalysisRun,
        verified_context_by_analyzer: Mapping[
            str, tuple[dict[str, str], ...]
        ],
    ) -> AIRequest:
        fields = [
            {"name": str(name), "value": value}
            for name, value in record.values.items()
            if value not in (None, "")
        ]
        if self.privacy_mode == "redacted_remote":
            fields = [
                {
                    "name": item["name"],
                    "value": _redacted_json_value(item["value"]),
                }
                for item in fields
            ]
        evidence = "\n".join(
            f"{item['name']}: {item['value']}" for item in fields
        )
        analyzer_name = {
            "government_support_evidence": "government_support_evidence",
            "party_resolution": "party",
            "case_history_summary": "case_history",
        }.get(task_type, "")
        wiki_context = list(
            verified_context_by_analyzer.get(analyzer_name, ())
        )
        if self.privacy_mode == "redacted_remote":
            wiki_context = [
                {
                    **item,
                    "excerpt": redact_sensitive_text(
                        str(item.get("excerpt", ""))
                    )[0],
                }
                for item in wiki_context
            ]
        wiki_evidence = "\n".join(
            (
                f"검증된 Wiki {item.get('display_name')}: "
                f"{item.get('excerpt')}"
            )
            for item in wiki_context
        )
        evidence = "\n".join(
            item for item in (evidence, wiki_evidence) if item
        )[: self.max_input_chars]
        if self.privacy_mode == "redacted_remote":
            evidence, _ = redact_sensitive_text(evidence)
        findings = [
            {
                "analyzer": result.analyzer,
                "reviews": [
                    {
                        "type": item.review_type,
                        "title": item.title,
                        "reason": item.reason,
                        "confidence": item.confidence,
                    }
                    for item in result.review_items[:5]
                ],
            }
            for result in run.results
        ]
        source = record.source_refs[0] if record.source_refs else None
        return AIRequest(
            task_type=task_type,
            source_hash=record.content_hash,
            source_type="excel",
            source_locator=(
                f"{source.sheet}:{source.row}" if source is not None else record.key
            ),
            payload={
                "sheet": record.sheet,
                "row": record.row,
                "fields": fields,
                "deterministic_findings": findings,
                "verified_wiki_context": wiki_context,
            },
            evidence_corpus=evidence,
            known_case_numbers=tuple(record_case_numbers(record)),
        )

    @staticmethod
    def _task_for_run(run: AnalysisRun) -> str | None:
        analyzer_names = {result.analyzer for result in run.results}
        reviews = [item for result in run.results for item in result.review_items]
        record = run.change.after or run.change.before
        if record is None:
            return None
        if "government_support_evidence" in analyzer_names and reviews:
            return "government_support_evidence"
        if "party" in analyzer_names and (
            reviews
            or any(
                _COMPLEX_PARTY.search(str(value))
                for value in record.values.values()
                if isinstance(value, str)
            )
        ):
            return "party_resolution"
        if "case_history" in analyzer_names and reviews:
            return "case_history_summary"
        if not run.routing.analyzer_names:
            return "excel_row_semantics"
        return None

    @staticmethod
    def _priority(run: AnalysisRun) -> int:
        task = StructuredAIService._task_for_run(run)
        return {
            "party_resolution": 40,
            "government_support_evidence": 30,
            "case_history_summary": 20,
            "excel_row_semantics": 10,
        }.get(task or "", 0)

    @staticmethod
    def _review_item(
        record: SourceRecord,
        analysis: AIAnalysis,
        run: AnalysisRun,
        verified_refs_by_analyzer: Mapping[str, tuple[KnowledgeRef, ...]],
    ) -> ReviewItem:
        claim_summary = {
            claim.field: claim.value for claim in analysis.claims
        }
        conflict = (
            f" | 충돌: {'; '.join(analysis.conflicts)}"
            if analysis.conflicts
            else ""
        )
        reason = (
            f"구조화 AI가 원본 인용을 검증한 뒤 제안했습니다. "
            f"모델={analysis.model}, 분석해시={analysis.output_digest[:16]}, "
            f"신뢰도={analysis.overall_confidence:.0%}{conflict}"
        )
        analyzer_name = {
            "government_support_evidence": "government_support_evidence",
            "party_resolution": "party",
            "case_history_summary": "case_history",
        }.get(analysis.task_type, "")
        knowledge_refs = [
            ref
            for result in run.results
            for item in result.review_items
            for ref in item.knowledge_refs
        ]
        knowledge_refs.extend(
            verified_refs_by_analyzer.get(analyzer_name, ())
        )
        return ReviewItem(
            review_type="AI 구조화 분석",
            title=f"AI 검토 제안 · {record.sheet} {record.row}행",
            reason=reason,
            proposed_value={
                "summary": analysis.summary,
                "claims": claim_summary,
                "needs_review": analysis.needs_review,
            },
            confidence=analysis.overall_confidence,
            source_refs=list(record.source_refs),
            knowledge_refs=list(dict.fromkeys(knowledge_refs)),
        )

    def _verified_party_replacement(
        self,
        run: AnalysisRun,
        analysis: AIAnalysis,
    ) -> tuple[ProposedChange, ProposedChange] | None:
        party_type = analysis.claim("party_type")
        canonical = analysis.claim("canonical_party_name")
        if (
            party_type is None
            or not isinstance(party_type.value, str)
            or party_type.value not in _PARTY_VALUES
            or party_type.confidence < self.verified_confidence
        ):
            return None
        proposals = [
            change
            for result in run.results
            if result.analyzer == "party"
            for change in result.proposed_changes
        ]
        type_changes = [
            change
            for change in proposals
            if isinstance(change.proposed_value, str)
            and change.proposed_value in _PARTY_VALUES
        ]
        if len(type_changes) != 1:
            return None
        original = type_changes[0]
        if canonical is not None:
            entity_name = original.entity_key.removeprefix("party:")
            if (
                not isinstance(canonical.value, str)
                or canonical.value.casefold() != entity_name.casefold()
            ):
                return None
        replacement = ProposedChange(
            target_database=original.target_database,
            entity_key=original.entity_key,
            property_name=original.property_name,
            kind=original.kind,
            current_value=original.current_value,
            proposed_value=party_type.value,
            analyzer="llm_party_resolution",
            analyzer_version=analysis.prompt_version,
            confidence=min(
                analysis.overall_confidence,
                party_type.confidence,
            ),
            reason=(
                "구조화 AI가 원본에 실제 존재하는 당사자명과 인용문을 근거로 "
                "구분을 제안했습니다. "
                f"모델={analysis.model}, 프롬프트={analysis.prompt_version}, "
                f"분석해시={analysis.output_digest[:16]}"
            ),
            source_refs=list(original.source_refs),
            knowledge_refs=list(original.knowledge_refs),
        )
        return original, replacement


def _case_numbers(text: str) -> set[str]:
    patterns = (
        r"\b(?:10|20|30|40)-\d{4}-\d{4,10}\b",
        r"\bPCT/[A-Z]{2}\d{4}/\d{4,10}\b",
    )
    return {
        match.group(0).upper()
        for pattern in patterns
        for match in re.finditer(pattern, text, flags=re.IGNORECASE)
    }


def compact_analysis_payload(analysis: AIAnalysis) -> dict[str, JsonValue]:
    """Return a bounded Wiki-safe projection without hidden prompts or credentials."""

    return {
        "schema_version": "1.0",
        "task_type": analysis.task_type,
        "summary": analysis.summary,
        "claims": {
            claim.field: claim.value for claim in analysis.claims
        },
        "conflicts": list(analysis.conflicts),
        "needs_review": analysis.needs_review,
        "overall_confidence": analysis.overall_confidence,
        "model": analysis.model,
        "prompt_version": analysis.prompt_version,
        "analysis_digest": analysis.output_digest,
    }


def _redacted_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return redact_sensitive_text(value)[0]
    if isinstance(value, list):
        return [_redacted_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _redacted_json_value(item)
            for key, item in value.items()
        }
    return value


def _schema_failure_code(error: AISchemaError) -> str:
    message = str(error)
    if "response fields" in message or "claim does not exactly" in message:
        return "response_shape_rejected"
    if "claim field" in message:
        return "claim_field_rejected"
    if "evidence" in message or "quote" in message:
        return "evidence_rejected"
    if any(
        marker in message
        for marker in (
            "case number",
            "party name",
            "canonical party",
            "party_type",
            "ISO date",
            "explicitly present",
        )
    ):
        return "grounding_rejected"
    return "schema_or_grounding_rejected"
