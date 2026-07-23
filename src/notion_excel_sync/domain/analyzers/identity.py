from __future__ import annotations

from collections import defaultdict

from notion_excel_sync.domain.analyzer import AnalysisContext, AnalyzerManifest, DomainAnalyzer
from notion_excel_sync.domain.analyzers.common import (
    find_before,
    find_value,
    non_empty,
    normalized_text,
    parse_date,
    parse_count,
    parse_money,
    record_case_numbers,
    split_values,
)
from notion_excel_sync.models import AnalyzerResult


CASE_NUMBER = ("당소 사건번호", "사건번호", "관리번호", "케이스번호")


class CaseIdentityAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="case_identity",
        version="1.0.0",
        description="Identifies patent cases and normalizes their stable identity fields.",
        header_signals=(
            "당소 사건번호",
            "사건번호",
            "출원번호",
            "사건종류",
            "수임상품",
            "의뢰일",
        ),
        keyword_signals=("가출원", "특허출원", "분할출원", "상표", "디자인"),
        outputs=("case",),
        target_databases=("한국 특허 사건",),
        knowledge_topics=("case-numbering", "procedure-rule"),
        priority=10,
    )

    _FIELDS = (
        ("당소 사건번호", CASE_NUMBER, normalized_text, 1.0),
        ("사건종류", ("사건종류", "권리구분", "출원종류"), normalized_text, 0.96),
        ("수임상품", ("수임상품", "상품", "수임유형"), normalized_text, 0.95),
        ("의뢰일", ("의뢰일", "수임일"), parse_date, 0.96),
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        record = context.record
        case_numbers = record_case_numbers(record)
        if not case_numbers:
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="사건 식별",
                    title="사건번호 미확정",
                    reason="사건 분석 신호가 있지만 안정적인 사건번호를 찾지 못했습니다.",
                    confidence=0.2,
                )
            )
            return result

        if len(case_numbers) > 1:
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="복수 사건",
                    title="한 원본 행의 복수 사건번호",
                    reason="각 사건 페이지로 분리하되 같은 원본 행을 공유하도록 제안했습니다.",
                    proposed_value=case_numbers,
                    confidence=0.9,
                )
            )

        for case_number in case_numbers:
            for property_name, aliases, parser, confidence in self._FIELDS:
                raw = case_number if property_name == "당소 사건번호" else find_value(record, aliases)
                if not non_empty(raw):
                    continue
                parsed = parser(raw)
                if parsed is None:
                    result.review_items.append(
                        self.review_item(
                            context,
                            review_type="값 형식",
                            title=f"{property_name} 형식 확인",
                            reason=f"{property_name} 값을 결정론적으로 정규화하지 못했습니다.",
                            current_value=raw,
                            confidence=0.25,
                        )
                    )
                    continue
                before = (
                    None
                    if len(case_numbers) > 1
                    else find_before(context.change.before, aliases)
                )
                if parser is parse_date:
                    before = parse_date(before)
                elif parser is normalized_text:
                    before = normalized_text(before)
                proposal = self.proposed_change(
                    context,
                    database="한국 특허 사건",
                    entity_key=case_number,
                    property_name=property_name,
                    proposed_value=parsed,
                    current_value=before,
                    confidence=confidence,
                    reason=f"Excel 원본의 {property_name} 필드를 정규화했습니다.",
                )
                if proposal:
                    result.proposed_changes.append(proposal)

        result.derived.update(
            {
                "case_keys": case_numbers,
                "primary_case_key": case_numbers[0],
                "case_type": normalized_text(find_value(record, ("사건종류", "권리구분", "출원종류"))),
                "product": normalized_text(find_value(record, ("수임상품", "상품", "수임유형"))),
                # Current case DB has no such properties; preserve for a future specialized
                # application analyzer/schema review instead of proposing unknown properties.
                "application_number": normalized_text(
                    find_value(record, ("출원번호", "출원 No", "출원no"))
                ),
                "application_date": parse_date(find_value(record, ("출원일", "출원일자"))),
            }
        )
        return result


class PartyAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="party",
        version="1.0.0",
        description="Extracts clients, applicants, inventors, and cost bearers.",
        header_signals=("의뢰인", "출원인", "발명자", "당사자", "비용부담자", "대표자"),
        dependencies=("case_identity",),
        outputs=("parties",),
        target_databases=("당사자",),
        knowledge_topics=("office-sop",),
        priority=20,
    )

    _ROLE_FIELDS = {
        "의뢰인": ("의뢰인", "고객", "고객명"),
        "출원인": ("출원인", "권리자"),
        "발명자": ("발명자",),
        "비용부담자": ("비용부담자", "청구처"),
        "담당자": ("고객담당자", "담당자명"),
    }

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        parties: dict[str, set[str]] = defaultdict(set)
        for role, aliases in self._ROLE_FIELDS.items():
            for name in split_values(find_value(context.record, aliases)):
                parties[name].add(role)

        derived_parties: list[dict[str, object]] = []
        for name in sorted(parties):
            roles = sorted(parties[name])
            entity_key = f"party:{name.casefold()}"
            explicit_type = normalized_text(
                find_value(context.record, ("당사자구분", "당사자유형", "법인구분"))
            )
            allowed_types = {"개인", "법인", "기관", "정부지원기관", "기타"}
            if explicit_type in allowed_types:
                party_type = explicit_type
                type_confidence = 0.97
            elif any(marker in name for marker in ("주식회사", "(주)", "㈜", "법인")):
                party_type = "법인"
                type_confidence = 0.93
            elif any(marker in name for marker in ("공단", "진흥원", "협회", "대학교", "연구원")):
                party_type = "기관"
                type_confidence = 0.84
            elif roles == ["발명자"]:
                party_type = "개인"
                type_confidence = 0.82
            else:
                party_type = "기타"
                type_confidence = 0.55
            for property_name, value, confidence in (
                ("당사자명", name, 0.98),
                ("구분", party_type, type_confidence),
            ):
                proposal = self.proposed_change(
                    context,
                    database="당사자",
                    entity_key=entity_key,
                    property_name=property_name,
                    proposed_value=value,
                    reason=f"원본의 {', '.join(roles)} 필드에서 당사자를 식별했습니다.",
                    confidence=confidence,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
            derived_parties.append({"key": entity_key, "name": name, "roles": roles})
            if party_type == "기타" and explicit_type not in allowed_types:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="당사자 구분",
                        title=f"{name} 당사자 구분 확인",
                        reason="이름과 역할만으로 개인·법인·기관을 확정할 수 없어 기타로 제안했습니다.",
                        proposed_value="기타",
                        confidence=type_confidence,
                    )
                )
        result.derived["parties"] = derived_parties
        return result


class GroupAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="group",
        version="1.1.0",
        description="Classifies government support, bulk-retainer, and portfolio groups.",
        header_signals=(
            "그룹",
            "그룹명",
            "정부지원사업",
            "지원사업명",
            "협약번호",
            "관리기관",
            "사업연도",
            "지원한도",
        ),
        keyword_signals=("정부지원", "바우처", "지원사업", "일괄수임", "패키지", "포트폴리오"),
        dependencies=("case_identity",),
        outputs=("group",),
        target_databases=("그룹",),
        knowledge_topics=("grouping-policy", "government-support-evidence"),
        priority=20,
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        record = context.record
        name = normalized_text(
            find_value(record, ("그룹명", "정부지원사업", "지원사업명", "사업명", "그룹"))
        )
        agreement = normalized_text(find_value(record, ("협약번호", "사업협약번호")))
        if not name and agreement:
            name = f"정부지원사업 {agreement}"
        if not name:
            return result

        group_type = normalized_text(find_value(record, ("그룹유형", "사업유형")))
        text = f"{name} {group_type or ''}"
        if not group_type:
            if agreement or any(word in text for word in ("지원", "바우처", "사업")):
                group_type = "정부지원사업"
            elif any(word in text for word in ("일괄", "패키지")):
                group_type = "일괄수임"
            else:
                group_type = "기타"
        group_key = agreement or name

        values = (
            ("그룹명", name, ("그룹명", "정부지원사업", "지원사업명", "사업명", "그룹"), 0.96),
            ("그룹유형", group_type, ("그룹유형", "사업유형"), 0.88),
            ("협약번호", agreement, ("협약번호", "사업협약번호"), 0.98),
            (
                "사업연도",
                parse_count(find_value(record, ("사업연도", "지원연도"))),
                ("사업연도", "지원연도"),
                0.95,
            ),
            (
                "관리기관",
                normalized_text(find_value(record, ("관리기관", "주관기관", "지원기관"))),
                ("관리기관", "주관기관", "지원기관"),
                0.94,
            ),
            (
                "지원한도",
                parse_money(find_value(record, ("지원한도", "사업한도"))),
                ("지원한도", "사업한도"),
                0.92,
            ),
        )
        for property_name, value, aliases, confidence in values:
            if value is None:
                continue
            current = find_before(context.change.before, aliases)
            if property_name == "지원한도":
                current = parse_money(current)
            elif property_name == "사업연도":
                current = parse_count(current)
            elif current is not None:
                current = normalized_text(current)
            proposal = self.proposed_change(
                context,
                database="그룹",
                entity_key=f"group:{group_key}",
                property_name=property_name,
                proposed_value=value,
                current_value=current,
                confidence=confidence,
                reason="정부지원사업·일괄수임·사건 묶음 신호를 기준으로 그룹을 분석했습니다.",
            )
            if proposal:
                result.proposed_changes.append(proposal)
        result.derived["group"] = {
            "key": f"group:{group_key}",
            "name": name,
            "type": group_type,
            "agreement_number": agreement,
        }
        return result


class RelationshipAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="relationship",
        version="1.1.0",
        description="Builds explicit relationships from already-derived domain entities.",
        dependencies=("case_identity",),
        outputs=("relationships",),
        target_databases=(
            "한국 특허 사건",
            "업무·절차",
            "비용·청구",
            "정부지원사업 증빙",
        ),
        knowledge_topics=("grouping-policy", "office-sop"),
        priority=90,
        always_run=True,
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        case_keys = list(context.derived("case_identity").get("case_keys", []))
        if not case_keys:
            return result
        relationships: list[dict[str, object]] = []

        group = context.derived("group").get("group")
        if isinstance(group, dict) and group.get("key"):
            for case_key in case_keys:
                proposal = self.proposed_change(
                    context,
                    database="한국 특허 사건",
                    entity_key=str(case_key),
                    property_name="그룹",
                    proposed_value=[str(group["key"])],
                    reason="그룹 분석 결과를 모든 대상 사건 관계로 연결했습니다.",
                    confidence=0.92,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
                relationships.append(
                    {"from": case_key, "to": group["key"], "type": "case_group"}
                )

        parties = context.derived("party").get("parties", [])
        if isinstance(parties, list) and parties:
            by_role: dict[str, list[str]] = defaultdict(list)
            for party in parties:
                if not isinstance(party, dict) or not party.get("key"):
                    continue
                for role in party.get("roles", []):
                    if role in {"의뢰인", "출원인", "발명자", "비용부담자"}:
                        by_role[str(role)].append(str(party["key"]))
            for role, party_keys in sorted(by_role.items()):
                for case_key in case_keys:
                    proposal = self.proposed_change(
                        context,
                        database="한국 특허 사건",
                        entity_key=str(case_key),
                        property_name=role,
                        proposed_value=party_keys,
                        reason=f"당사자 분석 결과를 사건의 {role} 관계로 연결했습니다.",
                        confidence=0.9,
                    )
                    if proposal:
                        result.proposed_changes.append(proposal)
                    relationships.extend(
                        {"from": case_key, "to": party_key, "type": f"case_{role}"}
                        for party_key in party_keys
                    )

        for analyzer_name, database, derived_key in (
            ("workflow_status", "업무·절차", "workflow_key"),
            ("actual_cost", "비용·청구", "cost_key"),
        ):
            entity = context.derived(analyzer_name).get(derived_key)
            if entity:
                proposal = self.proposed_change(
                    context,
                    database=database,
                    entity_key=str(entity),
                    property_name="사건",
                    proposed_value=case_keys,
                    reason="분석된 레코드를 원본 사건에 연결했습니다.",
                    confidence=0.95,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
                relationships.extend(
                    {"from": entity, "to": case_key, "type": "entity_case"}
                    for case_key in case_keys
                )

        evidence = context.derived("government_support_evidence").get("evidence")
        if isinstance(evidence, dict) and evidence.get("key"):
            evidence_key = str(evidence["key"])
            for property_name, value in (
                ("대상 사건", evidence.get("case_keys") or case_keys),
                ("정부지원사업 그룹", [evidence.get("group_key")] if evidence.get("group_key") else None),
            ):
                if not value:
                    continue
                proposal = self.proposed_change(
                    context,
                    database="정부지원사업 증빙",
                    entity_key=evidence_key,
                    property_name=property_name,
                    proposed_value=value,
                    reason="증빙안을 대상 사건 및 정부지원사업 그룹에 연결했습니다.",
                    confidence=0.94,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
        result.derived["relationships"] = relationships
        return result
