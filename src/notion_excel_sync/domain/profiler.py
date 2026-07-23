from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Iterable

from notion_excel_sync.domain.analyzers.common import normalize_header
from notion_excel_sync.models import JsonValue, SourceRecord


class InferredType(StrEnum):
    EMPTY = "empty"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    DATE = "date"
    DATETIME = "datetime"
    TEXT = "text"
    LIST = "list"
    MIXED = "mixed"


SEMANTIC_SIGNALS: dict[str, tuple[str, ...]] = {
    "case": ("사건번호", "당소사건번호", "출원번호", "사건종류", "수임상품"),
    "party": ("의뢰인", "출원인", "발명자", "당사자", "비용부담자"),
    "group": ("그룹", "사업명", "지원사업", "협약번호", "관리기관"),
    "workflow": ("업무명", "업무종류", "절차", "접수일", "완료일", "비고"),
    "status": ("상태", "진행상태", "현재상태"),
    "deadline": ("기일", "기한", "마감", "회신일"),
    "actual_cost": ("공급가", "부가세", "관납료", "약정총액", "청구액", "입금액"),
    "support_evidence": ("증빙", "인정금액", "지원금", "자부담", "인정건수"),
    "registration_followup": ("등록결정", "등록마감", "분할결정", "후속상태"),
    "contact": ("연락", "전화", "이메일", "문자", "회신"),
    "document_material": ("발명자료", "명세서", "도면", "문서", "자료상태"),
}

# Exact normalized aliases that an active analyzer deterministically consumes. A
# semantic guess alone is not enough: an unfamiliar "...상태" column must still
# produce an inert draft until an analyzer declares how it maps the value.
CONSUMED_COLUMN_SIGNALS: dict[str, tuple[str, ...]] = {
    "case_identity": (
        "당소 사건번호",
        "사건번호",
        "관리번호",
        "케이스번호",
        "사건종류",
        "권리구분",
        "출원종류",
        "수임상품",
        "상품",
        "수임유형",
        "의뢰일",
        "수임일",
        "출원번호",
        "출원일",
    ),
    "party": (
        "의뢰인",
        "고객",
        "고객명",
        "출원인",
        "권리자",
        "발명자",
        "비용부담자",
        "청구처",
        "고객담당자",
        "담당자명",
        "당사자구분",
        "당사자유형",
        "법인구분",
    ),
    "group": (
        "그룹",
        "그룹명",
        "정부지원사업",
        "지원사업명",
        "사업명",
        "협약번호",
        "사업협약번호",
        "그룹유형",
        "사업유형",
        "사업연도",
        "지원연도",
        "관리기관",
        "주관기관",
        "지원기관",
        "지원한도",
        "사업한도",
    ),
    "workflow_status": (
        "업무명",
        "업무",
        "절차명",
        "업무종류",
        "업무구분",
        "절차종류",
        "절차대분류",
        "대분류",
        "접수일",
        "접수일자",
        "완료일",
        "처리일",
        "종결일",
        "내부상태",
        "진행상태",
        "업무상태",
        "현재상태",
        "사건상태",
        "공식상태",
        "원본비고",
        "비고",
        "메모",
    ),
    "actual_cost": (
        "공급가",
        "공급가액",
        "부가세",
        "관납료",
        "관납료확정",
        "관납료예상",
        "예상관납료",
        "약정총액",
        "계약금액",
        "실제사건비용",
        "할인액",
        "청구액",
        "입금액",
        "선수금차감액",
        "예치금차감액",
        "미납액",
        "청구일",
        "입금일",
    ),
    "government_support_evidence": (
        "증빙유형",
        "증빙방식",
        "인정업무",
        "증빙구성",
        "증빙대상사건",
        "대상사건",
        "증빙사건번호",
        "인정건수",
        "증빙건수",
        "대상건수",
        "인정대상금액",
        "증빙액",
        "증빙금액",
        "정부지원금",
        "지원금액",
        "자부담",
        "자부담액",
        "부가세처리",
        "VAT처리",
        "산정방식",
        "증빙산식",
        "인정기준",
    ),
    "registration_followup": (
        "등록결정일",
        "결정일",
        "등록마감일",
        "등록마감",
        "등록료납부기한",
        "등록후속상태",
        "후속상태",
        "분할결정",
        "분할출원결정",
        "다음연락일",
        "연락예정일",
    ),
    "contact": (
        "연락일",
        "연락일시",
        "통화일",
        "메일일",
        "문자일",
        "연락방법",
        "연락수단",
        "채널",
        "연락대상",
        "수신자",
        "연락처명",
        "상대방명",
        "연락내용",
        "통화내용",
        "메일내용",
        "문자내용",
        "연락결과",
        "회신결과",
    ),
    "document_material": (
        "발명자료",
        "발명자료상태",
        "명세서",
        "명세서상태",
        "도면",
        "도면상태",
        "자료요청",
        "요청자료",
        "문서상태",
        "자료상태",
        "거절이유",
        "OA요지",
    ),
}


@dataclass(slots=True)
class ColumnProfile:
    header: str
    normalized_header: str
    inferred_type: InferredType
    non_null_count: int
    null_ratio: float
    unique_count: int
    semantic_candidates: list[str] = field(default_factory=list)
    consumer_candidates: list[str] = field(default_factory=list)
    examples: list[JsonValue] = field(default_factory=list)


@dataclass(slots=True)
class SheetProfile:
    sheet: str
    row_count: int
    columns: list[ColumnProfile]
    detected_domains: list[str]


@dataclass(slots=True)
class SourceCatalog:
    sheets: list[SheetProfile]
    unclassified_columns: list[tuple[str, str]]
    unconsumed_columns: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _value_type(value: JsonValue) -> InferredType:
    if value is None or value == "":
        return InferredType.EMPTY
    if isinstance(value, bool):
        return InferredType.BOOLEAN
    if isinstance(value, datetime):
        return InferredType.DATETIME
    if isinstance(value, date):
        return InferredType.DATE
    if isinstance(value, int):
        return InferredType.INTEGER
    if isinstance(value, float):
        return InferredType.NUMBER
    if isinstance(value, list):
        return InferredType.LIST
    return InferredType.TEXT


def _combined_type(values: list[JsonValue]) -> InferredType:
    types = {_value_type(value) for value in values if value not in (None, "")}
    if not types:
        return InferredType.EMPTY
    if types <= {InferredType.INTEGER}:
        return InferredType.INTEGER
    if types <= {InferredType.INTEGER, InferredType.NUMBER}:
        return InferredType.NUMBER
    if types <= {InferredType.DATE, InferredType.DATETIME}:
        return InferredType.DATETIME if InferredType.DATETIME in types else InferredType.DATE
    return next(iter(types)) if len(types) == 1 else InferredType.MIXED


def _semantic_candidates(header: str) -> list[str]:
    normalized = normalize_header(header)
    return [
        domain
        for domain, signals in SEMANTIC_SIGNALS.items()
        if any(
            normalize_header(signal) in normalized
            or normalized in normalize_header(signal)
            for signal in signals
        )
    ]


def _consumer_candidates(header: str) -> list[str]:
    normalized = normalize_header(header)
    return [
        analyzer
        for analyzer, signals in CONSUMED_COLUMN_SIGNALS.items()
        if normalized in {normalize_header(signal) for signal in signals}
    ]


class SourceProfiler:
    """Profiles source rows without interpreting or mutating their domain values."""

    def profile(self, records: Iterable[SourceRecord]) -> SourceCatalog:
        by_sheet: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in records:
            by_sheet[record.sheet].append(record)

        sheets: list[SheetProfile] = []
        unclassified: list[tuple[str, str]] = []
        unconsumed: list[tuple[str, str]] = []
        for sheet_name in sorted(by_sheet):
            sheet_records = by_sheet[sheet_name]
            headers = sorted({header for record in sheet_records for header in record.values})
            columns: list[ColumnProfile] = []
            domain_counts: Counter[str] = Counter()
            for header in headers:
                values = [record.values.get(header) for record in sheet_records]
                non_null = [value for value in values if value not in (None, "")]
                semantics = _semantic_candidates(header)
                consumers = _consumer_candidates(header)
                domain_counts.update(semantics)
                if not semantics:
                    unclassified.append((sheet_name, header))
                if not consumers:
                    unconsumed.append((sheet_name, header))
                examples: list[JsonValue] = []
                seen: set[str] = set()
                for value in non_null:
                    marker = repr(value)
                    if marker not in seen:
                        seen.add(marker)
                        examples.append(value)
                    if len(examples) == 3:
                        break
                columns.append(
                    ColumnProfile(
                        header=header,
                        normalized_header=normalize_header(header),
                        inferred_type=_combined_type(values),
                        non_null_count=len(non_null),
                        null_ratio=(len(values) - len(non_null)) / len(values) if values else 0.0,
                        unique_count=len({repr(value) for value in non_null}),
                        semantic_candidates=semantics,
                        consumer_candidates=consumers,
                        examples=examples,
                    )
                )
            sheets.append(
                SheetProfile(
                    sheet=sheet_name,
                    row_count=len(sheet_records),
                    columns=columns,
                    detected_domains=sorted(domain_counts),
                )
            )
        return SourceCatalog(
            sheets=sheets,
            unclassified_columns=unclassified,
            unconsumed_columns=unconsumed,
        )
