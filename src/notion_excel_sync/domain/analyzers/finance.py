from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping

from notion_excel_sync.domain.analyzer import (
    AnalysisContext,
    AnalyzerManifest,
    DomainAnalyzer,
    ValueKey,
)
from notion_excel_sync.domain.analyzers.common import (
    find_before,
    find_entry,
    find_value,
    non_empty,
    normalize_header,
    normalized_text,
    parse_count,
    parse_date,
    parse_money,
    primary_case_key,
    split_case_numbers,
)
from notion_excel_sync.models import AnalyzerResult, JsonValue, SourceRecord, SourceRef

if TYPE_CHECKING:
    from notion_excel_sync.domain.router import AnalysisRun


ACTUAL_COST_CONTEXT_METADATA = "actual_cost_context"
EVIDENCE_CONTEXT_METADATA = "government_support_evidence_context"

_EVIDENCE_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("가출원", ("가출원", "임시출원", "provisional")),
    ("PCT출원", ("pct", "국제출원")),
    ("상표출원", ("상표",)),
    ("디자인출원", ("디자인",)),
    ("특허출원", ("특허", "본출원")),
)
_VAT_TREATMENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("불인정", ("불인정", "인정불가", "제외")),
    ("면세", ("면세", "비과세")),
    ("별도", ("별도", "별도부담")),
    ("포함", ("포함", "내포")),
)


def _normalize_evidence_type(value: object) -> str:
    """Map free-form Excel evidence labels to the fixed Notion select contract."""

    raw = (normalized_text(value) or "").casefold()
    for canonical, signals in _EVIDENCE_TYPE_RULES:
        if any(signal.casefold() in raw for signal in signals):
            return canonical
    return "기타"


def _normalize_vat_treatment(value: object) -> str | None:
    """Map an explicit VAT value to the fixed Notion select contract."""

    raw = (normalized_text(value) or "").casefold()
    if not raw:
        return None
    for canonical, signals in _VAT_TREATMENT_RULES:
        if any(signal.casefold() in raw for signal in signals):
            return canonical
    return "확인필요"


@dataclass(frozen=True, slots=True)
class ActualCostContextEntry:
    """Read-only actual-cost fact from the full current workbook snapshot."""

    case_key: str
    cost_key: str
    total_actual_cost: int | None
    source_refs: tuple[SourceRef, ...]
    evidence_scope: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceContextEntry:
    """Row-level evidence fact from the full current workbook snapshot."""

    record_key: str
    evidence: Mapping[str, JsonValue]
    source_refs: tuple[SourceRef, ...]


def _case_keys(context: AnalysisContext) -> list[str]:
    keys = context.derived("case_identity").get("case_keys", [])
    if isinstance(keys, list) and keys:
        return [str(key) for key in keys]
    return [primary_case_key(context.record)]


def _evidence_key(program_key: str, evidence_type: str | None) -> str:
    normalized_program = normalized_text(program_key) or "unresolved"
    normalized_type = normalized_text(evidence_type) or "통합증빙"
    return f"evidence:{normalized_program}:{normalized_type}"


def _record_evidence_scope(record: SourceRecord) -> str | None:
    program = normalized_text(
        find_value(record, ("정부지원사업", "지원사업명", "사업명", "증빙사업"))
    )
    agreement = normalized_text(find_value(record, ("협약번호", "사업협약번호")))
    if not program and not agreement:
        return None
    raw_evidence_type = normalized_text(
        find_value(record, ("증빙유형", "증빙방식", "인정업무", "증빙구성"))
    )
    has_evidence_signal = bool(raw_evidence_type) or any(
        non_empty(find_value(record, aliases))
        for aliases in (
            ("인정건수", "증빙건수", "대상건수"),
            ("인정대상금액", "증빙액", "증빙금액"),
            ("정부지원금", "지원금액"),
            ("자부담", "자부담액"),
        )
    )
    if not has_evidence_signal:
        return None
    return _evidence_key(
        agreement or program or "",
        _normalize_evidence_type(raw_evidence_type),
    )


class ActualCostAnalyzer(DomainAnalyzer):
    """Analyzes actual case economics, never government-support evidence allocation."""

    manifest = AnalyzerManifest(
        name="actual_cost",
        version="1.1.0",
        description="Analyzes actual agreed, billed, paid, and official-fee amounts per case.",
        header_signals=(
            "공급가",
            "부가세",
            "관납료",
            "약정총액",
            "청구액",
            "입금액",
            "미납액",
            "선수금차감",
            "입금일",
            "청구일",
        ),
        dependencies=("case_identity",),
        outputs=("actual_cost", "billing"),
        target_databases=("비용·청구",),
        knowledge_topics=("actual-cost", "billing-policy"),
        priority=30,
    )

    _MONEY_FIELDS = (
        ("공급가", ("공급가", "공급가액", "수임료공급가"), 0.98),
        ("부가세", ("부가세", "VAT"), 0.98),
        ("관납료확정", ("관납료확정", "실관납료"), 0.96),
        ("관납료예상", ("관납료예상", "예상관납료"), 0.94),
        ("약정총액", ("약정총액", "계약금액", "실제사건비용"), 0.97),
        ("할인액", ("할인액", "할인금액"), 0.95),
        ("청구액", ("청구액", "청구금액"), 0.98),
        ("입금액", ("입금액", "입금금액"), 0.98),
        ("선수금차감액", ("선수금차감액", "예치금차감액"), 0.96),
        ("미납액", ("미납액", "잔금", "미수금"), 0.96),
    )
    _DATE_FIELDS = (
        ("청구일", ("청구일", "청구일자")),
        ("입금일", ("입금일", "입금일자")),
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        case_key = _case_keys(context)[0]
        cost_key = f"{case_key}:cost:{context.record.key}"
        amounts: dict[str, int] = {}

        for property_name, aliases, confidence in self._MONEY_FIELDS:
            header, raw = find_entry(context.record, aliases)
            if property_name == "관납료예상" and header and "예상" not in header:
                continue
            if property_name == "관납료확정" and header and "예상" in header:
                continue
            if not non_empty(raw):
                continue
            if property_name == "관납료확정" and normalize_header(header) == "관납료":
                confidence = 0.72
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="실제 사건 비용",
                        title="관납료 확정·예상 구분 확인",
                        reason=(
                            "일반 관납료 열을 확정액 후보로 보존했으나 "
                            "예상액인지 확정액인지 사용자 확인이 필요합니다."
                        ),
                        current_value=raw,
                        proposed_value="관납료확정",
                        confidence=confidence,
                    )
                )
            value = parse_money(raw)
            if value is None:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="실제 사건 비용",
                        title=f"{property_name} 금액 확인",
                        reason="실제 사건 비용 값을 금액으로 파싱하지 못했습니다.",
                        current_value=raw,
                        confidence=0.2,
                    )
                )
                continue
            amounts[property_name] = value
            proposal = self.proposed_change(
                context,
                database="비용·청구",
                entity_key=cost_key,
                property_name=property_name,
                proposed_value=value,
                current_value=parse_money(find_before(context.change.before, aliases)),
                reason="사건 자체의 실제 약정·청구·입금 데이터를 분석했습니다.",
                confidence=confidence,
            )
            if proposal:
                result.proposed_changes.append(proposal)

        for property_name, aliases in self._DATE_FIELDS:
            raw = find_value(context.record, aliases)
            if not non_empty(raw):
                continue
            value = parse_date(raw)
            if value is None:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="실제 사건 비용",
                        title=f"{property_name} 날짜 확인",
                        reason="청구 또는 입금 날짜를 정규화하지 못했습니다.",
                        current_value=raw,
                        confidence=0.2,
                    )
                )
                continue
            proposal = self.proposed_change(
                context,
                database="비용·청구",
                entity_key=cost_key,
                property_name=property_name,
                proposed_value=value,
                current_value=parse_date(find_before(context.change.before, aliases)),
                reason="사건 자체의 실제 청구·입금 일자를 분석했습니다.",
                confidence=0.96,
            )
            if proposal:
                result.proposed_changes.append(proposal)

        supply = amounts.get("공급가")
        vat = amounts.get("부가세")
        official_fee = amounts.get("관납료확정", amounts.get("관납료예상", 0))
        discount = amounts.get("할인액", 0)
        agreed = amounts.get("약정총액")
        if supply is not None and vat is not None:
            calculated_total = supply + vat + official_fee - discount
            if agreed is not None and abs(calculated_total - agreed) > 1:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="비용 계산 불일치",
                        title="실제 사건 약정총액 불일치",
                        reason="공급가+부가세+관납료-할인액과 약정총액이 일치하지 않습니다.",
                        current_value=agreed,
                        proposed_value=calculated_total,
                        confidence=0.99,
                    )
                )
            elif agreed is None:
                amounts["약정총액"] = calculated_total
                proposal = self.proposed_change(
                    context,
                    database="비용·청구",
                    entity_key=cost_key,
                    property_name="약정총액",
                    proposed_value=calculated_total,
                    reason="명시된 실제 비용 구성요소로 약정총액을 계산했습니다.",
                    confidence=0.88,
                )
                if proposal:
                    result.proposed_changes.append(proposal)

        billed = amounts.get("청구액")
        paid = amounts.get("입금액")
        deposit = amounts.get("선수금차감액", 0)
        outstanding = amounts.get("미납액")
        if billed is not None and paid is not None:
            calculated_outstanding = max(0, billed - paid - deposit)
            if outstanding is not None and abs(outstanding - calculated_outstanding) > 1:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="비용 계산 불일치",
                        title="실제 사건 미납액 불일치",
                        reason="청구액-입금액-선수금차감액과 미납액이 일치하지 않습니다.",
                        current_value=outstanding,
                        proposed_value=calculated_outstanding,
                        confidence=0.99,
                    )
                )
            elif outstanding is None:
                amounts["미납액"] = calculated_outstanding
                proposal = self.proposed_change(
                    context,
                    database="비용·청구",
                    entity_key=cost_key,
                    property_name="미납액",
                    proposed_value=calculated_outstanding,
                    reason="실제 청구·입금·선수금 차감액으로 미납액을 계산했습니다.",
                    confidence=0.9,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
            status = (
                "입금완료"
                if calculated_outstanding == 0
                else ("일부입금" if paid + deposit > 0 else "미입금")
            )
            proposal = self.proposed_change(
                context,
                database="비용·청구",
                entity_key=cost_key,
                property_name="입금상태",
                proposed_value=status,
                current_value=normalized_text(
                    find_before(context.change.before, ("입금상태", "수납상태"))
                ),
                reason="실제 청구액과 수납액의 차이로 입금상태를 계산했습니다.",
                confidence=0.92,
            )
            if proposal:
                result.proposed_changes.append(proposal)

        has_cost_date = any(
            non_empty(find_value(context.record, aliases)) for _, aliases in self._DATE_FIELDS
        )
        if amounts or has_cost_date:
            proposal = self.proposed_change(
                context,
                database="비용·청구",
                entity_key=cost_key,
                property_name="항목명",
                proposed_value=f"{case_key} 실제 사건 비용 [{context.record.key}]",
                reason="실제 사건 비용 레코드의 안정적인 제목을 생성했습니다.",
                confidence=0.99,
            )
            if proposal:
                result.proposed_changes.insert(0, proposal)

        if amounts or has_cost_date:
            total_actual = amounts.get("약정총액")
            if total_actual is None and supply is not None:
                total_actual = supply + (vat or 0) + official_fee - discount
            result.derived.update(
                {
                    "cost_key": cost_key,
                    "case_key": case_key,
                    "actual_cost": amounts,
                    "total_actual_cost": total_actual,
                }
            )
        return result


def build_actual_cost_context(
    records: Iterable[SourceRecord],
) -> dict[str, list[ActualCostContextEntry]]:
    """Index actual-cost facts from the full snapshot without emitting proposals."""

    result: dict[str, list[ActualCostContextEntry]] = defaultdict(list)
    for record in records:
        amounts: dict[str, int] = {}
        has_cost_signal = False
        for property_name, aliases, _ in ActualCostAnalyzer._MONEY_FIELDS:
            header, raw = find_entry(record, aliases)
            if property_name == "관납료예상" and header and "예상" not in header:
                continue
            if property_name == "관납료확정" and header and "예상" in header:
                continue
            if not non_empty(raw):
                continue
            has_cost_signal = True
            value = parse_money(raw)
            if value is not None:
                amounts[property_name] = value
        has_cost_signal = has_cost_signal or any(
            non_empty(find_value(record, aliases))
            for _, aliases in ActualCostAnalyzer._DATE_FIELDS
        )
        if not has_cost_signal:
            continue
        case_key = primary_case_key(record)
        total_actual = amounts.get("약정총액")
        supply = amounts.get("공급가")
        if total_actual is None and supply is not None:
            official_fee = amounts.get(
                "관납료확정", amounts.get("관납료예상", 0)
            )
            total_actual = (
                supply
                + amounts.get("부가세", 0)
                + official_fee
                - amounts.get("할인액", 0)
            )
        result[case_key].append(
            ActualCostContextEntry(
                case_key=case_key,
                cost_key=f"{case_key}:cost:{record.key}",
                total_actual_cost=total_actual,
                source_refs=tuple(record.source_refs),
                evidence_scope=_record_evidence_scope(record),
            )
        )
    return dict(result)


class GovernmentSupportEvidenceAnalyzer(DomainAnalyzer):
    """Builds evidence packages; it does not alter actual case cost records."""

    manifest = AnalyzerManifest(
        name="government_support_evidence",
        version="2.0.0",
        description="Analyzes which cases and amounts substantiate a government-support program.",
        header_signals=(
            "정부지원사업",
            "지원사업명",
            "증빙유형",
            "증빙대상사건",
            "인정건수",
            "증빙액",
            "인정대상금액",
            "정부지원금",
            "자부담",
            "부가세처리",
        ),
        keyword_signals=("가출원 3건", "증빙", "정부지원금", "자부담", "인정단가"),
        dependencies=("case_identity", "group", "actual_cost"),
        outputs=("support_evidence",),
        target_databases=("정부지원사업 증빙",),
        knowledge_topics=(
            "government-support-evidence",
            "eligible-cost",
            "proof-documents",
        ),
        priority=50,
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        record = context.record
        group = context.derived("group").get("group")
        program = normalized_text(
            find_value(record, ("정부지원사업", "지원사업명", "사업명", "증빙사업"))
        )
        if not program and isinstance(group, dict):
            program = normalized_text(group.get("name"))
        agreement = normalized_text(find_value(record, ("협약번호", "사업협약번호")))
        if not program and not agreement:
            # A bare evidence keyword may route here; never invent a program.
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="정부지원사업 증빙",
                    title="증빙 대상 사업 미확정",
                    reason="증빙 신호는 있으나 대상 정부지원사업을 식별하지 못했습니다.",
                    confidence=0.15,
                )
            )
            return result

        raw_evidence_type = normalized_text(
            find_value(record, ("증빙유형", "증빙방식", "인정업무", "증빙구성"))
        )
        evidence_type = _normalize_evidence_type(raw_evidence_type)
        explicit_cases = split_case_numbers(
            find_value(record, ("증빙대상사건", "대상사건", "증빙사건번호"))
        )
        case_keys = explicit_cases or _case_keys(context)
        count_raw = find_value(record, ("인정건수", "증빙건수", "대상건수"))
        count = parse_count(count_raw) or parse_count(raw_evidence_type)
        if count is None and explicit_cases:
            count = len(explicit_cases)

        eligible = parse_money(find_value(record, ("인정대상금액", "증빙액", "증빙금액")))
        subsidy = parse_money(find_value(record, ("정부지원금", "지원금액")))
        self_payment = parse_money(find_value(record, ("자부담", "자부담액")))
        raw_vat_treatment = normalized_text(
            find_value(record, ("부가세처리", "VAT처리"))
        )
        vat_treatment = _normalize_vat_treatment(raw_vat_treatment)
        method = normalized_text(find_value(record, ("산정방식", "증빙산식", "인정기준")))
        evidence_key = self._evidence_key(agreement or program, evidence_type)

        if raw_evidence_type is None or evidence_type == "기타":
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="정부지원사업 증빙",
                    title="증빙유형 확인 필요",
                    reason=(
                        "원본 증빙유형을 고정된 Notion 선택값으로 확정하지 못해 "
                        "'기타'로 분류했습니다."
                    ),
                    current_value=raw_evidence_type,
                    proposed_value=evidence_type,
                    confidence=0.55,
                )
            )
        if raw_vat_treatment is not None and vat_treatment == "확인필요":
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="정부지원사업 증빙",
                    title="부가세 처리 확인 필요",
                    reason=(
                        "원본 부가세 처리 값을 고정된 Notion 선택값으로 확정하지 "
                        "못했습니다."
                    ),
                    current_value=raw_vat_treatment,
                    proposed_value=vat_treatment,
                    confidence=0.55,
                )
            )

        fields = (
            ("증빙명", f"{program or agreement} - {evidence_type or '증빙안'}", 0.94),
            ("증빙유형", evidence_type, 0.92),
            ("대상 사건", case_keys, 0.88 if not explicit_cases else 0.98),
            ("인정 건수", count, 0.94),
            ("산정방식", method, 0.9),
            ("인정대상금액", eligible, 0.96),
            ("정부지원금", subsidy, 0.96),
            ("자부담", self_payment, 0.96),
            ("부가세 처리", vat_treatment, 0.92),
        )
        for property_name, value, confidence in fields:
            if value is None or value == []:
                continue
            proposal = self.proposed_change(
                context,
                database="정부지원사업 증빙",
                entity_key=evidence_key,
                property_name=property_name,
                proposed_value=value,
                reason="정부지원사업을 어떤 사건과 금액으로 증빙할지 별도 증빙안으로 분석했습니다.",
                confidence=confidence,
            )
            if proposal:
                result.proposed_changes.append(proposal)

        if count is not None and explicit_cases and count != len(explicit_cases):
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="정부지원사업 증빙",
                    title="증빙 건수와 대상 사건 수 불일치",
                    reason="인정건수와 명시된 증빙 대상 사건 수가 다릅니다.",
                    current_value={"인정건수": count, "대상사건수": len(explicit_cases)},
                    confidence=0.99,
                )
            )
        if eligible is not None and subsidy is not None and self_payment is not None:
            if abs(eligible - subsidy - self_payment) > 1:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="정부지원사업 증빙",
                        title="지원금·자부담 합계 불일치",
                        reason="정부지원금과 자부담의 합이 인정대상금액과 다릅니다.",
                        current_value=eligible,
                        proposed_value=subsidy + self_payment,
                        confidence=0.99,
                    )
                )

        actual_total = context.derived("actual_cost").get("total_actual_cost")
        if eligible is not None and isinstance(actual_total, (int, float)):
            if len(case_keys) == 1 and eligible > actual_total:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="정부지원사업 증빙",
                        title="증빙액이 실제 사건 비용을 초과",
                        reason="한 사건의 증빙 인정액이 분석된 실제 사건 비용보다 큽니다.",
                        current_value=actual_total,
                        proposed_value=eligible,
                        confidence=0.99,
                    )
                )
        elif eligible is not None:
            result.warnings.append("증빙액과 비교할 실제 사건 비용이 이 행에 없어 별도 확인이 필요합니다.")

        group_key = group.get("key") if isinstance(group, dict) else None
        result.derived["evidence"] = {
            "key": evidence_key,
            "program": program,
            "agreement_number": agreement,
            "group_key": group_key,
            "case_keys": case_keys,
            "evidence_type": evidence_type,
            "raw_evidence_type": raw_evidence_type,
            "recognized_count": count,
            "eligible_amount": eligible,
            "subsidy_amount": subsidy,
            "self_payment_amount": self_payment,
            "vat_treatment": vat_treatment,
            "raw_vat_treatment": raw_vat_treatment,
            "calculation_method": method,
            "actual_cost_reference": actual_total,
            "actual_cost_key": context.derived("actual_cost").get("cost_key"),
        }
        return result

    @staticmethod
    def _evidence_key(program_key: str, evidence_type: str | None) -> str:
        return _evidence_key(program_key, evidence_type)

    def aggregate_runs(
        self,
        runs: list["AnalysisRun"],
        *,
        current_values: Mapping[ValueKey, JsonValue] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Consolidate row-local facts into auditable program-level evidence packages.

        A repeated program amount is treated as one program target rather than being
        multiplied by the number of Excel rows. Distinct row-level target amounts are
        summed and flagged for review. Actual cost capacity is always deduplicated by
        cost-record key before allocation.
        """

        current_values = current_values or {}
        grouped: dict[str, list[tuple[AnalysisRun, AnalyzerResult, dict[str, JsonValue]]]] = (
            defaultdict(list)
        )
        for run in runs:
            evidence_result = next(
                (result for result in run.results if result.analyzer == self.manifest.name),
                None,
            )
            if evidence_result is None:
                continue
            evidence = evidence_result.derived.get("evidence")
            if isinstance(evidence, dict) and evidence.get("key"):
                grouped[str(evidence["key"])].append((run, evidence_result, evidence))

        if not grouped:
            return

        full_evidence_by_key: dict[str, list[EvidenceContextEntry]] = defaultdict(list)
        evidence_context = (metadata or {}).get(EVIDENCE_CONTEXT_METADATA)
        if isinstance(evidence_context, (list, tuple)):
            for entry in evidence_context:
                if not isinstance(entry, EvidenceContextEntry):
                    continue
                evidence_key = entry.evidence.get("key")
                if evidence_key:
                    full_evidence_by_key[str(evidence_key)].append(entry)

        actual_costs_by_case: dict[
            str, dict[str, tuple[int | None, list[SourceRef], str | None]]
        ] = defaultdict(dict)
        context_index = (metadata or {}).get(ACTUAL_COST_CONTEXT_METADATA)
        if isinstance(context_index, Mapping):
            for case_key, entries in context_index.items():
                if not isinstance(entries, (list, tuple)):
                    continue
                for entry in entries:
                    if not isinstance(entry, ActualCostContextEntry):
                        continue
                    actual_costs_by_case[str(case_key)][entry.cost_key] = (
                        entry.total_actual_cost,
                        list(entry.source_refs),
                        entry.evidence_scope,
                    )
        for run in runs:
            actual_result = next(
                (result for result in run.results if result.analyzer == "actual_cost"),
                None,
            )
            if actual_result is None:
                continue
            cost_key = actual_result.derived.get("cost_key")
            case_key = actual_result.derived.get("case_key")
            actual = actual_result.derived.get("total_actual_cost")
            if not cost_key or not case_key:
                continue
            normalized_actual = (
                max(0, int(actual))
                if isinstance(actual, (int, float)) and not isinstance(actual, bool)
                else None
            )
            record = run.change.after or run.change.before
            refs = list(record.source_refs) if record else []
            evidence_result = next(
                (
                    result
                    for result in run.results
                    if result.analyzer == self.manifest.name
                ),
                None,
            )
            evidence = (
                evidence_result.derived.get("evidence") if evidence_result else None
            )
            evidence_scope = (
                str(evidence["key"])
                if isinstance(evidence, dict) and evidence.get("key")
                else None
            )
            actual_costs_by_case[str(case_key)][str(cost_key)] = (
                normalized_actual,
                refs,
                evidence_scope,
            )

        # Row-local evidence changes and relationship changes use incomplete scope.
        # Replace all of them with one complete package per program/type below.
        for run in runs:
            for result in run.results:
                result.proposed_changes = [
                    change
                    for change in result.proposed_changes
                    if change.target_database != "정부지원사업 증빙"
                ]
                if result.analyzer == self.manifest.name:
                    result.review_items = [
                        item
                        for item in result.review_items
                        if item.title
                        not in {
                            "증빙 건수와 대상 사건 수 불일치",
                            "증빙액이 실제 사건 비용을 초과",
                        }
                    ]
                    result.warnings = [
                        warning
                        for warning in result.warnings
                        if not warning.startswith("증빙액과 비교할 실제 사건 비용")
                    ]

        case_memberships: dict[str, set[str]] = defaultdict(set)
        if full_evidence_by_key:
            for evidence_key, entries in full_evidence_by_key.items():
                for entry in entries:
                    for case_key in entry.evidence.get("case_keys") or []:
                        case_memberships[str(case_key)].add(evidence_key)
        else:
            for evidence_key, members in grouped.items():
                for _, _, evidence in members:
                    for case_key in evidence.get("case_keys") or []:
                        case_memberships[str(case_key)].add(evidence_key)

        for evidence_key, members in sorted(grouped.items()):
            self._aggregate_group(
                evidence_key,
                members,
                evidence_context=full_evidence_by_key.get(evidence_key),
                case_memberships=case_memberships,
                actual_costs_by_case=actual_costs_by_case,
                current_values=current_values,
            )

    def _aggregate_group(
        self,
        evidence_key: str,
        members: list[tuple["AnalysisRun", AnalyzerResult, dict[str, JsonValue]]],
        *,
        evidence_context: list[EvidenceContextEntry] | None,
        case_memberships: Mapping[str, set[str]],
        actual_costs_by_case: Mapping[
            str, Mapping[str, tuple[int | None, list[SourceRef], str | None]]
        ],
        current_values: Mapping[ValueKey, JsonValue],
    ) -> None:
        first_run, target_result, first = members[0]
        refs = self._combined_refs(members)
        evidence_rows: list[tuple[Mapping[str, JsonValue], list[SourceRef]]]
        if evidence_context:
            evidence_rows = [
                (entry.evidence, list(entry.source_refs))
                for entry in evidence_context
            ]
            refs = self._deduplicate_refs(
                [refs, *[source_refs for _, source_refs in evidence_rows]]
            )
        else:
            evidence_rows = [
                (evidence, list((run.change.after or run.change.before).source_refs))
                for run, _, evidence in members
                if run.change.after or run.change.before
            ]
        context = AnalysisContext(
            first_run.change,
            current_values=current_values,
            knowledge_refs=first_run.knowledge_refs_by_analyzer.get(
                self.manifest.name,
                (),
            ),
        )

        case_occurrences: Counter[str] = Counter()
        group_keys: set[str] = set()
        cost_totals: dict[str, int] = {}
        all_cost_keys: set[str] = set()
        cost_source_refs: list[SourceRef] = []
        program_names: set[str] = set()
        evidence_types: set[str] = set()
        recognized_counts: list[int] = []
        eligible_values: list[int] = []
        subsidy_values: list[int] = []
        self_payment_values: list[int] = []
        vat_treatments: set[str] = set()
        explicit_methods: set[str] = set()

        for evidence, _ in evidence_rows:
            for case_key in evidence.get("case_keys") or []:
                case_occurrences[str(case_key)] += 1
            if evidence.get("group_key"):
                group_keys.add(str(evidence["group_key"]))
            if evidence.get("program"):
                program_names.add(str(evidence["program"]))
            if evidence.get("evidence_type"):
                evidence_types.add(str(evidence["evidence_type"]))
            count = evidence.get("recognized_count")
            if isinstance(count, int) and not isinstance(count, bool):
                recognized_counts.append(count)
            for key, destination in (
                ("eligible_amount", eligible_values),
                ("subsidy_amount", subsidy_values),
                ("self_payment_amount", self_payment_values),
            ):
                value = evidence.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    destination.append(int(value))
            if evidence.get("vat_treatment"):
                vat_treatments.add(str(evidence["vat_treatment"]))
            if evidence.get("calculation_method"):
                explicit_methods.add(str(evidence["calculation_method"]))
            cost_key = evidence.get("actual_cost_key")
            actual = evidence.get("actual_cost_reference")
            if cost_key and isinstance(actual, (int, float)) and not isinstance(actual, bool):
                cost_totals[str(cost_key)] = max(0, int(actual))
                all_cost_keys.add(str(cost_key))

        cases = sorted(case_occurrences)
        for case_key in cases:
            for cost_key, (amount, source_refs, evidence_scope) in actual_costs_by_case.get(
                case_key, {}
            ).items():
                if evidence_scope is not None and evidence_scope != evidence_key:
                    continue
                all_cost_keys.add(cost_key)
                if amount is not None:
                    cost_totals[cost_key] = amount
                cost_source_refs.extend(source_refs)
        refs = self._deduplicate_refs([refs, cost_source_refs])
        cost_keys = sorted(all_cost_keys)
        actual_total = sum(cost_totals.values())
        recognized_count = self._program_value(recognized_counts)
        if recognized_count is None:
            recognized_count = len(cases)
        requested_eligible, eligible_conflict = self._aggregate_money_values(eligible_values)
        subsidy, subsidy_conflict = self._aggregate_money_values(subsidy_values)
        self_payment, self_payment_conflict = self._aggregate_money_values(self_payment_values)

        # The evidence amount is bounded by actual case costs. The shortage records the
        # unsupported portion instead of silently claiming it as substantiated.
        component_target = (
            subsidy + self_payment
            if subsidy is not None and self_payment is not None
            else None
        )
        allocation_target = (
            requested_eligible
            if requested_eligible is not None
            else component_target if component_target is not None else actual_total
        )
        recognized_amount = min(allocation_target, actual_total)
        shortage = max(0, allocation_target - actual_total)
        excess = max(0, actual_total - allocation_target)
        duplicate = any(count > 1 for count in case_occurrences.values()) or any(
            len(case_memberships.get(case_key, set())) > 1 for case_key in cases
        )
        count_mismatch = recognized_count != len(cases)
        amount_conflict = eligible_conflict or subsidy_conflict or self_payment_conflict
        funding_mismatch = (
            subsidy is not None
            and self_payment is not None
            and abs(subsidy + self_payment - allocation_target) > 1
        )
        allocated_subsidy, allocated_self_payment = self._allocate_funding(
            recognized_amount,
            subsidy,
            self_payment,
            funding_mismatch=funding_mismatch,
        )
        needs_review = any(
            (shortage > 0, duplicate, count_mismatch, amount_conflict, funding_mismatch)
        )

        program = sorted(program_names)[0] if program_names else str(first.get("program") or "")
        evidence_type = (
            sorted(evidence_types)[0] if len(evidence_types) == 1 else "복합 증빙"
        )
        title = f"{program or '정부지원사업'} - {evidence_type} {recognized_count}건"
        method = (
            f"사건 {len(cases)}건의 실제 비용 {actual_total:,}원 중 "
            f"{recognized_amount:,}원을 인정대상으로 배분"
        )
        basis = (
            f"요청 {allocation_target:,}원 / 실제비용 {actual_total:,}원 / "
            f"인정 {recognized_amount:,}원 / 부족 {shortage:,}원 / 초과 {excess:,}원"
        )
        if explicit_methods:
            basis = f"{basis}; 원본 산정방식: {' | '.join(sorted(explicit_methods))}"

        fields: tuple[tuple[str, JsonValue, float], ...] = (
            ("증빙명", title, 0.99),
            ("증빙유형", evidence_type, 0.92),
            ("인정 건수", recognized_count, 0.98),
            ("산정방식", method, 0.96),
            ("실제 비용 합계", actual_total, 0.99),
            ("인정대상금액", recognized_amount, 0.98),
            ("정부지원금", allocated_subsidy, 0.96),
            ("자부담", allocated_self_payment, 0.96),
            (
                "부가세 처리",
                sorted(vat_treatments)[0] if len(vat_treatments) == 1 else None,
                0.92,
            ),
            ("증빙상태", "검토필요" if needs_review else "증빙가능", 0.98),
            ("부족액", shortage, 0.99),
            ("초과액", excess, 0.99),
            ("중복증빙", duplicate, 0.99),
            ("산정근거", basis, 0.99),
            ("대상 사건", cases, 0.99),
            ("정부지원사업 그룹", sorted(group_keys), 0.98),
            ("관련 실제 비용", cost_keys, 0.99),
        )
        for property_name, value, confidence in fields:
            if value is None or value == []:
                continue
            proposal = self.proposed_change(
                context,
                database="정부지원사업 증빙",
                entity_key=evidence_key,
                property_name=property_name,
                proposed_value=value,
                reason="여러 사건의 실제 비용을 정부지원사업 증빙 한도에 배분했습니다.",
                confidence=confidence,
                source_refs=refs,
            )
            if proposal:
                target_result.proposed_changes.append(proposal)

        target_result.derived["evidence"] = {
            **first,
            "key": evidence_key,
            "case_keys": cases,
            "group_keys": sorted(group_keys),
            "actual_cost_keys": cost_keys,
            "recognized_count": recognized_count,
            "actual_cost_total": actual_total,
            "requested_eligible_amount": requested_eligible,
            "eligible_amount": recognized_amount,
            "allocated_subsidy_amount": allocated_subsidy,
            "allocated_self_payment_amount": allocated_self_payment,
            "shortage_amount": shortage,
            "excess_amount": excess,
            "duplicate_evidence": duplicate,
        }

        reviews = (
            (
                count_mismatch,
                "증빙 건수와 대상 사건 수 불일치",
                "인정건수와 고유 대상 사건 수가 달라 최종 배분 확인이 필요합니다.",
                {"인정건수": recognized_count, "대상사건수": len(cases)},
            ),
            (
                shortage > 0,
                "정부지원사업 증빙 부족",
                "배분 가능한 실제 사건 비용이 요청 인정대상금액보다 작습니다.",
                {"요청액": allocation_target, "실제비용": actual_total, "부족액": shortage},
            ),
            (
                duplicate,
                "중복 증빙 대상",
                "같은 사건이 같은 증빙안에 반복되거나 둘 이상의 지원사업에 배정되었습니다.",
                cases,
            ),
            (
                amount_conflict,
                "행별 지원금액 기준 불일치",
                "같은 증빙안의 행마다 서로 다른 금액이 있어 합산하되 확인 대상으로 표시했습니다.",
                {
                    "인정대상금액": sorted(set(eligible_values)),
                    "정부지원금": sorted(set(subsidy_values)),
                    "자부담": sorted(set(self_payment_values)),
                },
            ),
            (
                funding_mismatch,
                "지원금·자부담 합계 불일치",
                "정부지원금과 자부담 합계가 요청 인정대상금액과 다릅니다.",
                {
                    "인정대상금액": allocation_target,
                    "정부지원금": subsidy,
                    "자부담": self_payment,
                },
            ),
        )
        for condition, title_text, reason, value in reviews:
            if condition:
                target_result.review_items.append(
                    self.review_item(
                        context,
                        review_type="정부지원사업 증빙",
                        title=title_text,
                        reason=reason,
                        current_value=value,
                        confidence=0.99,
                        source_refs=refs,
                    )
                )

    @staticmethod
    def _program_value(values: list[int]) -> int | None:
        unique = sorted(set(values))
        return unique[0] if len(unique) == 1 else (max(unique) if unique else None)

    @staticmethod
    def _aggregate_money_values(values: list[int]) -> tuple[int | None, bool]:
        unique = sorted(set(values))
        if not unique:
            return None, False
        if len(unique) == 1:
            return unique[0], False
        return sum(values), True

    @staticmethod
    def _allocate_funding(
        recognized_amount: int,
        subsidy: int | None,
        self_payment: int | None,
        *,
        funding_mismatch: bool,
    ) -> tuple[int | None, int | None]:
        if funding_mismatch:
            return None, None
        if subsidy is not None and self_payment is not None:
            total = subsidy + self_payment
            if total <= 0:
                return 0, 0
            allocated_subsidy = round(recognized_amount * subsidy / total)
            return allocated_subsidy, recognized_amount - allocated_subsidy
        if subsidy is not None:
            return min(recognized_amount, subsidy), None
        if self_payment is not None:
            return None, min(recognized_amount, self_payment)
        return None, None

    @staticmethod
    def _deduplicate_refs(groups: list[list[SourceRef]]) -> list[SourceRef]:
        refs: list[SourceRef] = []
        seen: set[tuple[object, ...]] = set()
        for group in groups:
            for ref in group:
                marker = (
                    ref.drive_id,
                    ref.item_id,
                    ref.version_id,
                    ref.file_hash,
                    ref.sheet,
                    ref.row,
                    ref.cells,
                )
                if marker not in seen:
                    seen.add(marker)
                    refs.append(ref)
        return refs

    @staticmethod
    def _combined_refs(
        members: list[tuple["AnalysisRun", AnalyzerResult, dict[str, JsonValue]]],
    ) -> list[SourceRef]:
        groups = []
        for run, _, _ in members:
            record = run.change.after or run.change.before
            if record:
                groups.append(list(record.source_refs))
        return GovernmentSupportEvidenceAnalyzer._deduplicate_refs(groups)
