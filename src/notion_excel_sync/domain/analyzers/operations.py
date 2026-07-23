from __future__ import annotations

from notion_excel_sync.domain.analyzer import (
    AnalysisContext,
    AnalyzerManifest,
    DomainAnalyzer,
)
from notion_excel_sync.domain.analyzers.common import (
    find_before,
    find_value,
    non_empty,
    normalize_header,
    normalized_text,
    parse_date,
    primary_case_key,
)
from notion_excel_sync.models import AnalyzerResult


def _case_key(context: AnalysisContext) -> str:
    derived = context.derived("case_identity").get("primary_case_key")
    return str(derived or primary_case_key(context.record))


class WorkflowStatusAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="workflow_status",
        version="1.1.0",
        description="Analyzes work/procedure records and case/workflow statuses.",
        header_signals=(
            "업무명",
            "업무종류",
            "절차대분류",
            "접수일",
            "완료일",
            "현재상태",
            "진행상태",
            "원본비고",
        ),
        dependencies=("case_identity",),
        outputs=("workflow", "status"),
        target_databases=("업무·절차", "한국 특허 사건"),
        knowledge_topics=("office-sop", "procedure-rule"),
        priority=30,
    )

    _WORKFLOW_FIELDS = (
        ("업무명", ("업무명", "업무", "절차명"), normalized_text, 0.94),
        ("업무종류", ("업무종류", "업무구분", "절차종류"), normalized_text, 0.92),
        ("절차대분류", ("절차대분류", "대분류"), normalized_text, 0.9),
        ("접수일", ("접수일", "접수일자"), parse_date, 0.96),
        ("완료일", ("완료일", "처리일", "종결일"), parse_date, 0.96),
        ("내부상태", ("내부상태", "진행상태", "업무상태"), normalized_text, 0.9),
        ("원본비고", ("원본비고", "비고", "메모"), normalized_text, 0.98),
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        case_key = _case_key(context)
        workflow_key = f"{case_key}:workflow:{context.record.key}"
        emitted_title = False
        found_workflow_value = False
        for property_name, aliases, parser, confidence in self._WORKFLOW_FIELDS:
            raw = find_value(context.record, aliases)
            if not non_empty(raw):
                continue
            found_workflow_value = True
            value = parser(raw)
            if value is None:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="업무 값 형식",
                        title=f"{property_name} 확인",
                        reason=f"{property_name} 값을 안전하게 정규화하지 못했습니다.",
                        current_value=raw,
                        confidence=0.25,
                    )
                )
                continue
            if property_name == "업무명":
                value = f"{case_key} {value} [{context.record.key}]"
            emitted_title = emitted_title or property_name == "업무명"
            before = find_before(context.change.before, aliases)
            before = parser(before) if before is not None else None
            if property_name == "업무명" and before:
                before = f"{case_key} {before} [{context.record.key}]"
            proposal = self.proposed_change(
                context,
                database="업무·절차",
                entity_key=workflow_key,
                property_name=property_name,
                proposed_value=value,
                current_value=before,
                reason="Excel 행을 사건에 연결되는 업무·절차 레코드로 분석했습니다.",
                confidence=confidence,
            )
            if proposal:
                result.proposed_changes.append(proposal)

        if found_workflow_value and not emitted_title:
            proposal = self.proposed_change(
                context,
                database="업무·절차",
                entity_key=workflow_key,
                property_name="업무명",
                proposed_value=f"{case_key} 업무·절차 [{context.record.key}]",
                reason="업무 레코드 생성에 필요한 제목을 안전하게 보완했습니다.",
                confidence=0.88,
            )
            if proposal:
                result.proposed_changes.insert(0, proposal)

        status = normalized_text(find_value(context.record, ("현재상태", "사건상태", "공식상태")))
        if status:
            proposal = self.proposed_change(
                context,
                database="한국 특허 사건",
                entity_key=case_key,
                property_name="현재상태",
                proposed_value=status,
                current_value=normalized_text(
                    find_before(context.change.before, ("현재상태", "사건상태", "공식상태"))
                ),
                reason="원본의 명시적 사건 상태를 보존했습니다.",
                confidence=0.92,
            )
            if proposal:
                result.proposed_changes.append(proposal)

        received = parse_date(find_value(context.record, ("접수일", "접수일자")))
        completed = parse_date(find_value(context.record, ("완료일", "처리일", "종결일")))
        if received and completed and completed < received:
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="날짜 이상",
                    title="완료일이 접수일보다 빠름",
                    reason="업무 날짜의 선후관계가 맞지 않아 자동 보정하지 않았습니다.",
                    current_value={"접수일": received, "완료일": completed},
                    confidence=0.99,
                )
            )
        result.derived.update(
            {
                "workflow_key": workflow_key,
                "received_date": received,
                "completed_date": completed,
                "status": status,
            }
        )
        return result


class DeadlineAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="deadline",
        version="1.1.0",
        description="Extracts legal, internal, registration, and client-response deadlines.",
        header_signals=("법정기일", "기일", "마감일", "기한", "회신일", "등록마감"),
        dependencies=("case_identity",),
        outputs=("deadlines",),
        target_databases=("기일",),
        knowledge_topics=("legal-deadline", "procedure-rule"),
        priority=40,
    )

    _TOKENS = ("기일", "기한", "마감", "회신일", "제출일", "납부일")

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        case_key = _case_key(context)
        deadlines: list[dict[str, str]] = []
        for header, raw in context.record.values.items():
            normalized = normalize_header(header)
            if not any(token in normalized for token in self._TOKENS) or not non_empty(raw):
                continue
            parsed = parse_date(raw)
            if parsed is None:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="기일 형식",
                        title=f"{header} 날짜 확인",
                        reason="기일 신호가 있으나 유효한 날짜로 해석되지 않았습니다.",
                        current_value=raw,
                        confidence=0.2,
                    )
                )
                continue
            if "법정" in normalized or "등록마감" in normalized:
                deadline_type = "법정기일"
            elif "회신" in normalized:
                deadline_type = "고객회신"
            else:
                deadline_type = "내부기일"
            deadline_key = f"{case_key}:deadline:{normalized}"
            current = parse_date(find_before(context.change.before, (header,)))
            for property_name, value, confidence in (
                ("기일명", f"{case_key} {header}", 0.96),
                ("기일유형", deadline_type, 0.93),
                ("기일", parsed, 0.98),
                ("사건", [case_key], 0.98),
            ):
                proposal = self.proposed_change(
                    context,
                    database="기일",
                    entity_key=deadline_key,
                    property_name=property_name,
                    proposed_value=value,
                    current_value=current if property_name == "기일" else None,
                    reason=f"{header} 열을 {deadline_type}로 분석했습니다.",
                    confidence=confidence,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
            deadlines.append(
                {
                    "key": deadline_key,
                    "name": header,
                    "type": deadline_type,
                    "date": parsed,
                }
            )
        result.derived["deadlines"] = deadlines
        return result


class RegistrationFollowupAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="registration_followup",
        version="1.1.0",
        description="Analyzes actions required after a registration decision.",
        header_signals=(
            "등록결정",
            "등록마감일",
            "등록후속상태",
            "분할결정",
            "등록료",
            "다음연락일",
        ),
        sheet_signals=("등록결정",),
        dependencies=("case_identity",),
        outputs=("registration_followup",),
        target_databases=("등록결정 후속관리",),
        knowledge_topics=("registration-followup", "procedure-rule"),
        priority=40,
    )

    _FIELDS = (
        ("등록마감일", ("등록마감일", "등록마감", "등록료납부기한"), parse_date, 0.98),
        ("후속상태", ("등록후속상태", "후속상태"), normalized_text, 0.92),
        ("분할결정", ("분할결정", "분할출원결정"), normalized_text, 0.9),
        ("다음연락일", ("다음연락일", "연락예정일"), parse_date, 0.95),
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        case_key = _case_key(context)
        entity_key = f"{case_key}:registration-followup"
        found = False
        decision_date = parse_date(find_value(context.record, ("등록결정일", "결정일")))
        if decision_date:
            found = True
            result.warnings.append(
                "등록결정일은 현재 후속관리 스키마에 별도 필드가 없어 분석 근거로만 보존합니다."
            )
        for property_name, aliases, parser, confidence in self._FIELDS:
            raw = find_value(context.record, aliases)
            if not non_empty(raw):
                continue
            found = True
            value = parser(raw)
            if value is None:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="등록 후속",
                        title=f"{property_name} 확인",
                        reason="등록결정 후속관리 값을 정규화하지 못했습니다.",
                        current_value=raw,
                        confidence=0.25,
                    )
                )
                continue
            before = find_before(context.change.before, aliases)
            before = parser(before) if before is not None else None
            proposal = self.proposed_change(
                context,
                database="등록결정 후속관리",
                entity_key=entity_key,
                property_name=property_name,
                proposed_value=value,
                current_value=before,
                reason="등록결정 이후의 명시적 후속관리 값을 분석했습니다.",
                confidence=confidence,
            )
            if proposal:
                result.proposed_changes.append(proposal)
        if found:
            proposal = self.proposed_change(
                context,
                database="등록결정 후속관리",
                entity_key=entity_key,
                property_name="후속관리명",
                proposed_value=f"{case_key} 등록결정 후속관리",
                reason="등록결정 후속관리 레코드의 제목을 생성했습니다.",
                confidence=0.99,
            )
            if proposal:
                result.proposed_changes.insert(0, proposal)
            proposal = self.proposed_change(
                context,
                database="등록결정 후속관리",
                entity_key=entity_key,
                property_name="사건",
                proposed_value=[case_key],
                reason="후속관리 항목을 사건과 연결했습니다.",
                confidence=0.98,
            )
            if proposal:
                result.proposed_changes.append(proposal)
            result.derived["registration_followup_key"] = entity_key
            result.derived["registration_decision_date"] = decision_date
        return result


class ContactAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="contact",
        version="1.1.0",
        description="Extracts explicit phone, email, text, and other contact records.",
        header_signals=("연락일", "연락방법", "연락내용", "연락대상", "통화", "이메일", "문자"),
        dependencies=("case_identity",),
        outputs=("contact",),
        target_databases=("연락이력",),
        knowledge_topics=("office-sop",),
        priority=40,
    )

    _FIELDS = (
        ("연락일시", ("연락일", "연락일시", "통화일", "메일일", "문자일"), parse_date, 0.96),
        ("채널", ("연락방법", "연락수단", "채널"), normalized_text, 0.9),
        ("상대방명", ("연락대상", "수신자", "연락처명", "상대방명"), normalized_text, 0.9),
        ("메모", ("연락내용", "통화내용", "메일내용", "문자내용", "메모"), normalized_text, 0.95),
        ("결과", ("연락결과", "회신결과", "결과"), normalized_text, 0.9),
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        case_key = _case_key(context)
        entity_key = f"{case_key}:contact:{context.record.key}"
        found = False
        for property_name, aliases, parser, confidence in self._FIELDS:
            raw = find_value(context.record, aliases)
            if not non_empty(raw):
                continue
            found = True
            value = parser(raw)
            if value is None:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="연락이력",
                        title=f"{property_name} 확인",
                        reason="연락이력 값을 정규화하지 못했습니다.",
                        current_value=raw,
                        confidence=0.25,
                    )
                )
                continue
            before = find_before(context.change.before, aliases)
            before = parser(before) if before is not None else None
            proposal = self.proposed_change(
                context,
                database="연락이력",
                entity_key=entity_key,
                property_name=property_name,
                proposed_value=value,
                current_value=before,
                reason="명시적인 연락 기록을 연락이력으로 분석했습니다.",
                confidence=confidence,
            )
            if proposal:
                result.proposed_changes.append(proposal)
        if found:
            proposal = self.proposed_change(
                context,
                database="연락이력",
                entity_key=entity_key,
                property_name="연락제목",
                proposed_value=f"{case_key} 연락이력 {context.record.key}",
                reason="연락이력 레코드의 제목을 생성했습니다.",
                confidence=0.99,
            )
            if proposal:
                result.proposed_changes.insert(0, proposal)
            proposal = self.proposed_change(
                context,
                database="연락이력",
                entity_key=entity_key,
                property_name="사건",
                proposed_value=[case_key],
                reason="연락이력을 사건과 연결했습니다.",
                confidence=0.98,
            )
            if proposal:
                result.proposed_changes.append(proposal)
            result.derived["contact_key"] = entity_key
        return result


class DocumentMaterialAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="document_material",
        version="1.1.0",
        description="Analyzes invention materials and document preparation states.",
        header_signals=("발명자료", "명세서", "도면", "자료요청", "자료확정", "문서상태", "거절이유"),
        dependencies=("case_identity",),
        outputs=("document_material",),
        target_databases=("업무·절차",),
        knowledge_topics=("document-checklist", "template"),
        priority=40,
    )

    _FIELDS = (
        ("발명자료 상태", ("발명자료", "발명자료상태")),
        ("명세서 상태", ("명세서", "명세서상태")),
        ("도면 상태", ("도면", "도면상태")),
        ("자료요청 상태", ("자료요청", "요청자료")),
        ("문서 상태", ("문서상태", "자료상태")),
        ("거절이유", ("거절이유", "OA요지")),
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        result = self.empty_result()
        case_key = _case_key(context)
        entity_key = f"{case_key}:materials:{context.record.key}"
        material: dict[str, str] = {}
        for property_name, aliases in self._FIELDS:
            value = normalized_text(find_value(context.record, aliases))
            if not value:
                continue
            material[property_name] = value
        if material:
            summary = " | ".join(f"{name}: {value}" for name, value in sorted(material.items()))
            for property_name, value, confidence in (
                ("업무명", f"{case_key} 문서·자료 관리 [{context.record.key}]", 0.99),
                ("업무종류", "문서·자료 관리", 0.96),
                ("원본비고", summary, 0.99),
                ("사건", [case_key], 0.99),
            ):
                proposal = self.proposed_change(
                    context,
                    database="업무·절차",
                    entity_key=entity_key,
                    property_name=property_name,
                    proposed_value=value,
                    reason="문서·자료 상태를 현재 업무 스키마의 제목·유형·원문 필드로 보존했습니다.",
                    confidence=confidence,
                )
                if proposal:
                    result.proposed_changes.append(proposal)
            result.derived.update({"document_material_key": entity_key, "material": material})
        return result
