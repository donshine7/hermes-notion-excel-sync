from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from notion_excel_sync.knowledge.index import WikiIndexError
from notion_excel_sync.knowledge.policy import redact_sensitive_text
from notion_excel_sync.knowledge.retrieval import WikiRetriever
from notion_excel_sync.models import KnowledgeRef, RecordChange

if TYPE_CHECKING:
    from notion_excel_sync.domain.analyzer import AnalyzerManifest
    from notion_excel_sync.domain.router import AnalysisPipeline


_SAFE_VALUE_HEADER_TOKENS = (
    "사업명",
    "지원사업",
    "전담기관",
    "수행기관",
    "사업연도",
    "연도",
    "국가코드",
    "출원국",
    "국가",
    "사건종류",
    "권리구분",
    "업무구분",
    "절차",
    "진행상태",
    "상태",
    "통화",
    "비용종류",
    "증빙종류",
)
_SENSITIVE_HEADER_TOKENS = (
    "고객",
    "의뢰인",
    "출원인",
    "발명자",
    "담당자",
    "성명",
    "이름",
    "연락",
    "전화",
    "휴대폰",
    "메일",
    "email",
    "주소",
    "계좌",
    "주민",
    "생년",
    "사건번호",
)


@dataclass(frozen=True, slots=True)
class AnalyzerKnowledgeSnapshot:
    available: bool
    binding: dict[str, str | int] = field(default_factory=dict)
    verified_refs_by_analyzer: dict[str, tuple[KnowledgeRef, ...]] = field(
        default_factory=dict
    )
    shadow_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class AnalyzerKnowledgeProvider:
    """Retrieve once per analyzer/batch; never grant the retriever write access."""

    retriever: WikiRetriever

    @staticmethod
    def _query_text(
        manifest: AnalyzerManifest,
        changes: list[RecordChange],
    ) -> str:
        parts = [*manifest.knowledge_topics, *manifest.header_signals]
        remaining = 2_000
        for change in changes:
            record = change.after or change.before
            if record is None:
                continue
            for header, value in sorted(record.values.items(), key=lambda item: item[0]):
                if value in (None, ""):
                    continue
                header_text = str(header).strip()
                folded_header = header_text.casefold()
                if any(token in folded_header for token in _SENSITIVE_HEADER_TOKENS):
                    continue
                # Headers help retrieve generic rules without copying a whole
                # case into a query. Only a narrow set of categorical fields
                # may contribute values (for example program year or country).
                fragment = header_text
                if any(token in folded_header for token in _SAFE_VALUE_HEADER_TOKENS):
                    safe_value, findings = redact_sensitive_text(str(value))
                    if not findings and safe_value:
                        fragment = f"{header_text} {safe_value[:120]}"
                if len(fragment) > remaining:
                    break
                parts.append(fragment)
                remaining -= len(fragment)
            if remaining <= 0:
                break
        return " ".join(str(item) for item in parts if str(item).strip())

    def prepare(
        self,
        changes: list[RecordChange],
        pipeline: AnalysisPipeline,
    ) -> AnalyzerKnowledgeSnapshot:
        with self.retriever.index.generation_lock(timeout_seconds=30.0):
            return self._prepare_under_generation_lock(changes, pipeline)

    def _prepare_under_generation_lock(
        self,
        changes: list[RecordChange],
        pipeline: AnalysisPipeline,
    ) -> AnalyzerKnowledgeSnapshot:
        try:
            snapshot = self.retriever.index.status()
        except WikiIndexError:
            return AnalyzerKnowledgeSnapshot(available=False)
        selected: set[str] = set()
        for change in changes:
            record = change.after or change.before
            if record is None:
                continue
            selected.update(pipeline.router.route(record).analyzer_names)
        verified: dict[str, tuple[KnowledgeRef, ...]] = {}
        shadow_counts: dict[str, int] = {}
        for analyzer in pipeline.registry.ordered(selected) if selected else ():
            topics = analyzer.manifest.knowledge_topics
            if not topics:
                continue
            query = self._query_text(analyzer.manifest, changes)
            shadow_hits = self.retriever.query(
                query,
                topics=topics,
                verified_only=False,
            )
            shadow_counts[analyzer.manifest.name] = len(shadow_hits)
            if (
                self.retriever.config.rollout_mode != "verified"
                or not analyzer.manifest.uses_verified_knowledge
            ):
                continue
            verified_hits = self.retriever.query(
                query,
                topics=topics,
                verified_only=True,
            )
            if verified_hits:
                verified[analyzer.manifest.name] = tuple(
                    hit.ref for hit in verified_hits
                )
        return AnalyzerKnowledgeSnapshot(
            available=True,
            binding=snapshot.binding(),
            verified_refs_by_analyzer=verified,
            shadow_counts=dict(sorted(shadow_counts.items())),
        )
