from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from notion_excel_sync.domain.analyzer import (
    AnalysisContext,
    AnalyzerManifest,
    DomainAnalyzer,
)
from notion_excel_sync.domain.analyzers.common import (
    find_entry,
    find_value,
    non_empty,
    normalized_text,
    parse_date,
    primary_case_key,
)
from notion_excel_sync.models import AnalyzerResult, JsonValue, sha256_json


@dataclass(frozen=True, slots=True)
class _EventDate:
    value: str
    event_type: str
    source_header: str
    priority: int


class CaseHistoryAnalyzer(DomainAnalyzer):
    """Build one chronological, source-bound event for each case and Excel row.

    An event key deliberately excludes dates and summaries. Corrections to the
    same source row therefore update the already approved history page instead
    of producing a duplicate event. The analyzer never invents an occurrence
    date: rows without a parseable event date do not produce Notion changes.
    """

    manifest = AnalyzerManifest(
        name="case_history",
        version="1.0.0",
        description=(
            "Builds human-readable chronological case events from explicit "
            "Excel dates and source facts."
        ),
        header_signals=(
            "등록결정일",
            "완료일",
            "종료일",
            "처리일",
            "연락일",
            "접수일",
            "받은날짜",
            "수신일",
            "출원일",
            "의뢰일",
            "착수일",
            "시작일",
        ),
        dependencies=("case_identity",),
        outputs=("case_history",),
        target_databases=("사건 히스토리",),
        knowledge_topics=("case-history", "office-sop", "procedure-rule"),
        priority=45,
    )

    _EVENT_DATE_FIELDS: tuple[tuple[str, tuple[str, ...], str, int], ...] = (
        ("등록결정", ("등록결정일", "등록결정 일자", "결정일"), "등록결정", 60),
        ("업무완료", ("완료일", "완료일자", "종료일", "처리일", "종결일"), "업무완료", 50),
        ("연락", ("연락일", "연락일시", "통화일", "메일일", "문자일"), "연락", 40),
        ("출원", ("출원일", "출원일자", "신청일"), "출원", 35),
        ("업무접수", ("접수일", "접수일자", "받은날짜", "수신일"), "업무접수", 30),
        ("업무시작", ("의뢰일", "수임일", "착수일", "시작일"), "업무시작", 20),
    )
    _SUMMARY_FIELDS = (
        "업무명",
        "작업설명",
        "업무",
        "절차명",
        "사건종류",
        "현재상태",
    )
    _DETAIL_FIELDS = (
        "사건종류",
        "업무종류",
        "작업설명",
        "현재상태",
        "내부상태",
        "공식절차",
        "접수일",
        "받은날짜",
        "완료일",
        "종료일",
        "비고",
        "메모",
    )
    _NEXT_ACTION_FIELDS = (
        "다음조치",
        "후속조치",
        "해야할일",
        "요청사항",
        "자료요청",
        "회신요청",
    )
    _DEADLINE_FIELDS = (
        "법정기일",
        "등록마감일",
        "회신기한",
        "제출기한",
        "마감일",
        "내부기일",
        "다음연락일",
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        event_date, invalid_date = self._event_date(context)
        if event_date is None:
            if invalid_date is not None:
                header, raw = invalid_date
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="사건 히스토리 날짜",
                        title=f"{header} 날짜 확인",
                        reason=(
                            "사건 히스토리의 발생일을 임의로 만들지 않고 "
                            "원본 날짜의 사람 확인을 요청합니다."
                        ),
                        current_value=raw,
                        confidence=0.2,
                    )
                )
            return result

        case_keys = self._case_keys(context)
        if not case_keys:
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="사건 히스토리 식별",
                    title="사건번호 확인",
                    reason=(
                        "발생일은 확인했지만 사람에게 표시할 사건번호가 없어 "
                        "사건 히스토리를 만들지 않았습니다."
                    ),
                    confidence=0.2,
                )
            )
            return result
        summary_subject = self._first_text(context, self._SUMMARY_FIELDS)
        next_action = self._first_text(context, self._NEXT_ACTION_FIELDS)
        next_deadline = self._next_deadline(context, after=event_date.value)
        details = self._details(context)
        collected_at = self._collected_at(context.metadata.get("captured_at"))
        source = context.source_refs[0]
        source_position = f"Excel {source.sheet} 시트 {source.row}행"

        emitted: list[dict[str, str]] = []
        for case_key in case_keys:
            event_id = self._event_id(case_key, context.record.key)
            title = f"{case_key} · {event_date.value} · {event_date.event_type}"
            summary = self._summary(
                subject=summary_subject,
                event_type=event_date.event_type,
                source_header=event_date.source_header,
            )
            values: list[tuple[str, JsonValue, float]] = [
                ("히스토리명", title, 0.99),
                ("이벤트ID", event_id, 1.0),
                ("사건번호", case_key, 1.0),
                ("발생일", event_date.value, 0.99),
                ("이벤트유형", event_date.event_type, 0.96),
                ("요약", summary, 0.94),
                ("출처유형", "Excel", 1.0),
                ("출처위치", source_position, 1.0),
                ("원본버전", source.version_id, 1.0),
                ("원본해시", source.file_hash, 1.0),
                ("신뢰도", 0.96, 1.0),
                ("검토상태", "승인완료", 1.0),
                ("사람확인", True, 1.0),
                ("사건", [case_key], 1.0),
            ]
            if collected_at:
                values.append(("수집일시", collected_at, 1.0))
            if details:
                values.append(("상세내용", details, 0.92))
            if next_action:
                values.append(("다음조치", next_action, 0.9))
            if next_deadline:
                values.append(("다음기한", next_deadline, 0.96))

            for property_name, value, confidence in values:
                proposal = self.proposed_change(
                    context,
                    database="사건 히스토리",
                    entity_key=event_id,
                    property_name=property_name,
                    proposed_value=value,
                    reason=(
                        f"{source_position}의 명시된 {event_date.source_header}와 "
                        "사건 정보를 시간순 사건 이벤트로 정리했습니다."
                    ),
                    confidence=confidence,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
            emitted.append(
                {
                    "event_id": event_id,
                    "case_key": case_key,
                    "occurred_on": event_date.value,
                    "event_type": event_date.event_type,
                }
            )

        result.derived["case_history_events"] = emitted
        return result

    @classmethod
    def _event_date(
        cls,
        context: AnalysisContext,
    ) -> tuple[_EventDate | None, tuple[str, JsonValue] | None]:
        candidates: list[_EventDate] = []
        first_invalid: tuple[str, JsonValue] | None = None
        for _, aliases, event_type, priority in cls._EVENT_DATE_FIELDS:
            header, raw = find_entry(context.record, aliases)
            if header is None or not non_empty(raw):
                continue
            parsed = parse_date(raw)
            if parsed is None:
                if first_invalid is None:
                    first_invalid = (header, raw)
                continue
            candidates.append(
                _EventDate(
                    value=parsed,
                    event_type=event_type,
                    source_header=header,
                    priority=priority,
                )
            )
        if not candidates:
            return None, first_invalid
        return max(candidates, key=lambda item: (item.value, item.priority)), first_invalid

    @staticmethod
    def _case_keys(context: AnalysisContext) -> list[str]:
        raw = context.derived("case_identity").get("case_keys")
        if isinstance(raw, list):
            keys = [
                str(value).strip()
                for value in raw
                if str(value).strip()
                and not CaseHistoryAnalyzer._internal_key(str(value).strip())
            ]
            if keys:
                return list(dict.fromkeys(keys))
        fallback = str(
            context.derived("case_identity").get("primary_case_key")
            or primary_case_key(context.record)
        ).strip()
        return [fallback] if fallback and not CaseHistoryAnalyzer._internal_key(fallback) else []

    @staticmethod
    def _internal_key(value: str) -> bool:
        folded = value.casefold()
        return (
            folded.startswith(("source:", "sha256:", "local:", "row:"))
            or ":sha256:" in folded
        )

    @staticmethod
    def _event_id(case_key: str, record_key: str) -> str:
        identity = sha256_json(
            {
                "case_key": case_key,
                "source_record_key": record_key,
                "slot": "primary",
            }
        )
        return f"case-history:{identity[:32]}"

    @staticmethod
    def _first_text(context: AnalysisContext, fields: Iterable[str]) -> str | None:
        for field in fields:
            value = normalized_text(find_value(context.record, (field,)))
            if value:
                return value
        return None

    @classmethod
    def _summary(
        cls,
        *,
        subject: str | None,
        event_type: str,
        source_header: str,
    ) -> str:
        if subject:
            return cls._truncate(f"{subject} · {event_type}", 500)
        return f"{source_header} 기준 {event_type}"

    @classmethod
    def _details(cls, context: AnalysisContext) -> str | None:
        parts: list[str] = []
        seen_headers: set[str] = set()
        for field in cls._DETAIL_FIELDS:
            header, raw = find_entry(context.record, (field,))
            value = normalized_text(raw)
            if not header or not value or header in seen_headers:
                continue
            seen_headers.add(header)
            parts.append(f"{header}: {value}")
        return cls._truncate(" | ".join(parts), 1900) if parts else None

    @classmethod
    def _next_deadline(
        cls,
        context: AnalysisContext,
        *,
        after: str,
    ) -> str | None:
        candidates: list[str] = []
        for field in cls._DEADLINE_FIELDS:
            parsed = parse_date(find_value(context.record, (field,)))
            if parsed and parsed >= after:
                candidates.append(parsed)
        return min(candidates) if candidates else None

    @staticmethod
    def _collected_at(value: object) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).isoformat()
            except ValueError:
                return None
        return None

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 1].rstrip() + "…"
