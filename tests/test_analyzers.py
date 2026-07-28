from notion_excel_sync.domain import (
    AnalysisContext,
    AnalysisPipeline,
    AnalyzerContractError,
    AnalyzerManifest,
    DomainAnalyzer,
    SafeDraftAnalyzerGenerator,
    SourceProfiler,
    build_default_registry,
)
from notion_excel_sync.models import (
    AnalyzerResult,
    ChangeKind,
    ProposedChange,
    RecordChange,
    SourceRecord,
    SourceRef,
)
from notion_excel_sync.workflow.notion_writer import NOTION_DEFINITIONS


def source_ref() -> SourceRef:
    return SourceRef(
        drive_id="drive",
        item_id="item",
        version_id="2.0",
        file_hash="abc",
        sheet="2026",
        row=2,
        cells="A2:Z2",
    )


def test_pipeline_routes_domain_analyzers_and_keeps_actual_and_evidence_cost_separate():
    record = SourceRecord(
        key="row-2",
        sheet="2026",
        row=2,
        values={
            "당소 사건번호": "SS-2026-001",
            "의뢰인": "상상 주식회사",
            "정부지원사업": "2026 수출바우처",
            "협약번호": "VB-2026-01",
            "공급가": "1,000,000원",
            "부가세": "100,000원",
            "약정총액": "1,100,000원",
            "청구액": "1,100,000원",
            "입금액": "500,000원",
            "증빙유형": "가출원 1건",
            "증빙대상사건": "SS-2026-001",
            "인정대상금액": "900,000원",
            "정부지원금": "720,000원",
            "자부담": "180,000원",
        },
        source_refs=[source_ref()],
    )
    change = RecordChange(ChangeKind.CREATE, record.key, None, record)

    run = AnalysisPipeline(build_default_registry()).analyze_change(change)

    names = list(run.routing.analyzer_names)
    assert names.index("case_identity") < names.index("actual_cost")
    assert names.index("actual_cost") < names.index("government_support_evidence")
    assert names[-1] == "anomaly"
    assert all(change.source_refs for change in run.proposed_changes)

    actual = [
        change
        for change in run.proposed_changes
        if change.analyzer == "actual_cost" and change.property_name == "약정총액"
    ]
    evidence = [
        change
        for change in run.proposed_changes
        if change.analyzer == "government_support_evidence"
        and change.property_name == "인정대상금액"
    ]
    assert actual[0].target_database == "비용·청구"
    assert actual[0].proposed_value == 1_100_000
    assert evidence[0].target_database == "정부지원사업 증빙"
    assert evidence[0].proposed_value == 900_000


def test_party_analyzer_preserves_english_legal_name_commas_and_classifies_company():
    record = SourceRecord(
        key="foreign-company",
        sheet="2026",
        row=258,
        values={
            "당소 사건번호": "SS-2026-258",
            "의뢰인": "NPTI GLOBAL CO., LTD.",
        },
        source_refs=[source_ref()],
    )
    run = AnalysisPipeline(build_default_registry()).analyze_change(
        RecordChange(ChangeKind.CREATE, record.key, None, record)
    )
    party_changes = [
        change
        for change in run.proposed_changes
        if change.target_database == "당사자"
    ]
    by_entity: dict[str, dict[str, object]] = {}
    for change in party_changes:
        by_entity.setdefault(change.entity_key, {})[change.property_name] = (
            change.proposed_value
        )

    assert by_entity == {
        "party:npti global co., ltd.": {
            "당사자명": "NPTI GLOBAL CO., LTD.",
            "구분": "법인",
        }
    }


def test_party_analyzer_blocks_composite_or_temporary_names_from_notion():
    for row, name in enumerate(
        (
            "(주)씨에프씨/김성일_서울로미래로",
            "(주)신규법인_유준혁",
            "LTD.",
        ),
        start=2,
    ):
        record = SourceRecord(
            key=f"ambiguous-party-{row}",
            sheet="2026",
            row=row,
            values={"당소 사건번호": f"SS-{row}", "의뢰인": name},
            source_refs=[source_ref()],
        )
        run = AnalysisPipeline(build_default_registry()).analyze_change(
            RecordChange(ChangeKind.CREATE, record.key, None, record)
        )

        assert not any(
            change.target_database == "당사자"
            for change in run.proposed_changes
        )
        assert any(
            item.title == f"{name} 정식 당사자명 확인"
            for item in run.review_items
        )


def test_party_analyzer_classifies_clear_korean_person_name_as_individual():
    record = SourceRecord(
        key="korean-person",
        sheet="2026",
        row=214,
        values={"당소 사건번호": "SS-214", "의뢰인": "강나영"},
        source_refs=[source_ref()],
    )
    run = AnalysisPipeline(build_default_registry()).analyze_change(
        RecordChange(ChangeKind.CREATE, record.key, None, record)
    )
    values = {
        change.property_name: change.proposed_value
        for change in run.proposed_changes
        if change.target_database == "당사자"
    }

    assert values == {"당사자명": "강나영", "구분": "개인"}


def test_source_profiler_and_generator_create_inert_draft_for_unknown_column():
    records = [
        SourceRecord(
            key="1",
            sheet="신규업무",
            row=2,
            values={"당소 사건번호": "SS-1", "외주업체등급": "A"},
            source_refs=[source_ref()],
        )
    ]

    catalog = SourceProfiler().profile(records)
    drafts = SafeDraftAnalyzerGenerator().create_drafts(catalog)

    assert ("신규업무", "외주업체등급") in catalog.unclassified_columns
    assert len(drafts) == 1
    assert drafts[0].manifest.lifecycle.value == "draft"
    assert drafts[0].activation_allowed is False
    assert "eval" not in drafts[0].to_json().lower()


def test_analyzer_contract_rejects_proposal_without_provenance():
    class UnsafeAnalyzer(DomainAnalyzer):
        manifest = AnalyzerManifest(
            name="unsafe",
            version="1",
            description="test",
            target_databases=("한국 특허 사건",),
        )

        def analyze(self, context: AnalysisContext) -> AnalyzerResult:
            return AnalyzerResult(
                analyzer="unsafe",
                version="1",
                proposed_changes=[
                    ProposedChange(
                        target_database="한국 특허 사건",
                        entity_key="x",
                        property_name="x",
                        kind=ChangeKind.CREATE,
                        current_value=None,
                        proposed_value="x",
                        analyzer="unsafe",
                        analyzer_version="1",
                        confidence=1,
                        reason="test",
                        source_refs=[],
                    )
                ],
            )

    analyzer = UnsafeAnalyzer()
    record = SourceRecord("x", "s", 1, {"x": "x"}, [source_ref()])
    context = AnalysisContext(RecordChange(ChangeKind.CREATE, "x", None, record))
    try:
        analyzer.validate_result(analyzer.analyze(context))
    except AnalyzerContractError:
        pass
    else:
        raise AssertionError("unsafe analyzer output was not rejected")


def test_all_emitted_changes_match_notion_schema_types_and_create_titles():
    record = SourceRecord(
        key="all-domains",
        sheet="통합",
        row=2,
        values={
            "당소 사건번호": "SS-1 / SS-2",
            "사건종류": "특허출원",
            "수임상품": "표준",
            "의뢰일": "2026-07-16",
            "의뢰인": "예시 주식회사",
            "출원인": "예시 주식회사",
            "발명자": "홍길동",
            "정부지원사업": "2026 수출바우처",
            "협약번호": "VB-2026-01",
            "사업연도": "2026",
            "지원한도": "2,000,000원",
            "업무명": "가출원 접수",
            "업무종류": "출원",
            "접수일": "2026-07-16",
            "완료일": "2026-07-17",
            "현재상태": "진행중",
            "법정기일": "2026-08-31",
            "공급가": "1,000,000원",
            "부가세": "100,000원",
            "관납료예상": "50,000원",
            "약정총액": "1,150,000원",
            "청구액": "1,150,000원",
            "입금액": "500,000원",
            "선수금차감액": "100,000원",
            "증빙유형": "가출원 2건",
            "인정건수": "2건",
            "인정대상금액": "900,000원",
            "정부지원금": "720,000원",
            "자부담": "180,000원",
            "부가세처리": "별도",
            "등록결정일": "2026-07-15",
            "등록마감일": "2026-08-15",
            "후속상태": "안내예정",
            "연락일": "2026-07-16",
            "연락방법": "전화",
            "연락대상": "담당자",
            "연락내용": "등록 안내",
            "연락결과": "회신대기",
            "발명자료": "수령",
            "명세서상태": "작성중",
            "도면상태": "대기",
        },
        source_refs=[source_ref()],
    )
    runs = AnalysisPipeline(build_default_registry()).analyze_changes(
        [RecordChange(ChangeKind.CREATE, record.key, None, record)]
    )
    routed = {name for run in runs for name in run.routing.analyzer_names}
    assert {
        "case_identity",
        "party",
        "group",
        "workflow_status",
        "deadline",
        "case_history",
        "actual_cost",
        "government_support_evidence",
        "registration_followup",
        "contact",
        "document_material",
        "relationship",
        "anomaly",
    } <= routed
    changes = [change for run in runs for change in run.proposed_changes]
    assert changes

    for change in changes:
        definition = NOTION_DEFINITIONS[change.target_database]
        assert change.property_name in definition.properties, (
            change.analyzer,
            change.target_database,
            change.property_name,
        )
        property_type = definition.properties[change.property_name]
        value = change.proposed_value
        if property_type == "number":
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
        elif property_type == "checkbox":
            assert isinstance(value, bool)
        elif property_type == "relation":
            assert isinstance(value, list) and all(isinstance(item, str) for item in value)
        elif property_type == "date":
            assert isinstance(value, str) and len(value) >= 10
        else:
            assert isinstance(value, str)

    cost_properties = {
        change.property_name
        for change in changes
        if change.target_database == "비용·청구"
    }
    assert "관납료예상" in cost_properties
    assert "관납료확정" not in cost_properties

    by_entity: dict[tuple[str, str], set[str]] = {}
    for change in changes:
        by_entity.setdefault((change.target_database, change.entity_key), set()).add(
            change.property_name
        )
    for (database, _), properties in by_entity.items():
        assert NOTION_DEFINITIONS[database].title_property in properties


def test_case_history_builds_a_human_event_with_stable_source_identity():
    before = SourceRecord(
        key="stable-work-row",
        sheet="2026",
        row=17,
        values={
            "당소 사건번호": "SS-2026-017",
            "업무명": "가출원 명세서 제출",
            "접수일": "2026-07-10",
            "완료일": "2026-07-17",
            "현재상태": "완료",
            "법정기일": "2026-08-01",
        },
        source_refs=[source_ref()],
    )
    after = SourceRecord(
        key=before.key,
        sheet=before.sheet,
        row=before.row,
        values={**before.values, "완료일": "2026-07-18"},
        source_refs=[source_ref()],
    )

    create_run = AnalysisPipeline(build_default_registry()).analyze_change(
        RecordChange(ChangeKind.CREATE, before.key, None, before),
        metadata={"captured_at": "2026-07-18T09:30:00+09:00"},
    )
    update_run = AnalysisPipeline(build_default_registry()).analyze_change(
        RecordChange(ChangeKind.UPDATE, after.key, before, after),
        metadata={"captured_at": "2026-07-19T09:30:00+09:00"},
    )
    created = [
        change
        for change in create_run.proposed_changes
        if change.target_database == "사건 히스토리"
    ]
    updated = [
        change
        for change in update_run.proposed_changes
        if change.target_database == "사건 히스토리"
    ]
    created_values = {change.property_name: change.proposed_value for change in created}
    updated_values = {change.property_name: change.proposed_value for change in updated}

    assert created_values["히스토리명"] == "SS-2026-017 · 2026-07-17 · 업무완료"
    assert created_values["발생일"] == "2026-07-17"
    assert created_values["이벤트유형"] == "업무완료"
    assert created_values["요약"] == "가출원 명세서 제출 · 업무완료"
    assert created_values["다음기한"] == "2026-08-01"
    assert created_values["출처위치"] == "Excel 2026 시트 2행"
    assert created_values["사건"] == ["SS-2026-017"]
    assert created_values["사람확인"] is True
    assert created_values["검토상태"] == "승인완료"
    assert created_values["수집일시"] == "2026-07-18T09:30:00+09:00"
    assert {change.entity_key for change in created} == {
        change.entity_key for change in updated
    }
    assert updated_values["발생일"] == "2026-07-18"


def test_case_history_uses_received_date_and_does_not_invent_missing_dates():
    received = SourceRecord(
        key="received-work",
        sheet="2026",
        row=20,
        values={
            "당소 사건번호": "SS-2026-020",
            "작업설명": "발명자료 수령",
            "받은날짜": "2026.07.20",
        },
        source_refs=[source_ref()],
    )
    undated = SourceRecord(
        key="undated-work",
        sheet="2026",
        row=21,
        values={
            "당소 사건번호": "SS-2026-021",
            "작업설명": "발명자료 검토",
        },
        source_refs=[source_ref()],
    )
    runs = AnalysisPipeline(build_default_registry()).analyze_changes(
        [
            RecordChange(ChangeKind.CREATE, received.key, None, received),
            RecordChange(ChangeKind.CREATE, undated.key, None, undated),
        ]
    )
    history = [
        change
        for run in runs
        for change in run.proposed_changes
        if change.target_database == "사건 히스토리"
    ]

    assert {change.entity_key for change in history}
    assert {
        change.proposed_value
        for change in history
        if change.property_name == "이벤트유형"
    } == {"업무접수"}
    assert not any(
        change.entity_key.endswith("undated-work")
        for change in history
    )


def test_case_history_rejects_internal_source_keys_as_case_numbers():
    record = SourceRecord(
        key="source:sha256:abc:2026:10",
        sheet="2026",
        row=10,
        values={"완료일": "2026-07-20", "작업설명": "내부 식별자만 존재"},
        source_refs=[source_ref()],
    )
    run = AnalysisPipeline(build_default_registry()).analyze_change(
        RecordChange(ChangeKind.CREATE, record.key, None, record)
    )

    assert not any(
        change.target_database == "사건 히스토리"
        for change in run.proposed_changes
    )
    assert any(item.title == "사건번호 확인" for item in run.review_items)


def test_batch_evidence_aggregates_three_cases_and_allocates_actual_cost_capacity():
    records = []
    for row, case_number in enumerate(
        ("SS-1", "SS-2", "SS-3"),
        start=2,
    ):
        records.append(
            SourceRecord(
                key=f"row-{row}",
                sheet="지원사업",
                row=row,
                values={
                    "당소 사건번호": case_number,
                    "정부지원사업": "2026 수출바우처",
                    "협약번호": "VB-2026-01",
                    "증빙유형": "가출원 3건",
                    "인정건수": "3건",
                    "인정대상금액": "900,000원",
                    "정부지원금": "720,000원",
                    "자부담": "180,000원",
                },
                source_refs=[
                    SourceRef(
                        drive_id="drive",
                        item_id="item",
                        version_id="2.0",
                        file_hash="abc",
                        sheet="지원사업",
                        row=row,
                        cells=f"A{row}:Z{row}",
                    )
                ],
            )
        )
    for row, (case_number, actual_cost) in enumerate(
        (
            ("SS-1", 200_000),
            ("SS-1", 200_000),
            ("SS-2", 500_000),
            ("SS-3", 300_000),
        ),
        start=5,
    ):
        records.append(
            SourceRecord(
                key=f"cost-{row}",
                sheet="실제비용",
                row=row,
                values={"당소 사건번호": case_number, "약정총액": actual_cost},
                source_refs=[
                    SourceRef(
                        drive_id="drive",
                        item_id="item",
                        version_id="2.0",
                        file_hash="abc",
                        sheet="실제비용",
                        row=row,
                        cells=f"A{row}:Z{row}",
                    )
                ],
            )
        )
    changes = [
        RecordChange(ChangeKind.CREATE, record.key, None, record) for record in records
    ]
    runs = AnalysisPipeline(build_default_registry()).analyze_changes(changes)
    evidence_changes = [
        change
        for run in runs
        for change in run.proposed_changes
        if change.target_database == "정부지원사업 증빙"
    ]
    assert len({change.entity_key for change in evidence_changes}) == 1
    values = {change.property_name: change.proposed_value for change in evidence_changes}
    assert values["인정 건수"] == 3
    assert values["실제 비용 합계"] == 1_200_000
    assert values["인정대상금액"] == 900_000
    assert values["부족액"] == 0
    assert values["초과액"] == 300_000
    assert values["중복증빙"] is False
    assert values["증빙상태"] == "증빙가능"
    assert values["대상 사건"] == ["SS-1", "SS-2", "SS-3"]
    assert len(values["관련 실제 비용"]) == 4
    assert values["정부지원사업 그룹"] == ["group:VB-2026-01"]
    assert values["증빙유형"] == "가출원"
    assert len(next(iter(evidence_changes)).source_refs) == 7


def test_evidence_select_values_are_normalized_to_the_fixed_notion_contract():
    record = SourceRecord(
        key="normalized-selects",
        sheet="지원사업",
        row=2,
        values={
            "당소 사건번호": "SS-NORMALIZED",
            "정부지원사업": "지원사업 A",
            "협약번호": "AG-NORMALIZED",
            "증빙유형": "가출원 3건",
            "인정건수": "3건",
            "부가세처리": "부가세는 별도 부담",
            "약정총액": "300,000원",
        },
        source_refs=[source_ref()],
    )
    runs = AnalysisPipeline(build_default_registry()).analyze_changes(
        [RecordChange(ChangeKind.CREATE, record.key, None, record)]
    )
    values = {
        change.property_name: change.proposed_value
        for run in runs
        for change in run.proposed_changes
        if change.target_database == "정부지원사업 증빙"
    }
    assert values["증빙유형"] == "가출원"
    assert values["부가세 처리"] == "별도"


def test_unknown_evidence_select_values_fail_closed_to_reviewable_options():
    record = SourceRecord(
        key="unknown-selects",
        sheet="지원사업",
        row=2,
        values={
            "당소 사건번호": "SS-UNKNOWN",
            "정부지원사업": "지원사업 B",
            "협약번호": "AG-UNKNOWN",
            "증빙유형": "상담 및 번역 혼합",
            "부가세처리": "담당자 협의",
            "약정총액": "100,000원",
        },
        source_refs=[source_ref()],
    )
    runs = AnalysisPipeline(build_default_registry()).analyze_changes(
        [RecordChange(ChangeKind.CREATE, record.key, None, record)]
    )
    values = {
        change.property_name: change.proposed_value
        for run in runs
        for change in run.proposed_changes
        if change.target_database == "정부지원사업 증빙"
    }
    reviews = [item.title for run in runs for item in run.review_items]
    assert values["증빙유형"] == "기타"
    assert values["부가세 처리"] == "확인필요"
    assert "증빙유형 확인 필요" in reviews
    assert "부가세 처리 확인 필요" in reviews


def test_batch_evidence_marks_shortage_and_cross_program_duplicate_case():
    records = []
    for row, program in enumerate(("지원사업 A", "지원사업 B"), start=2):
        records.append(
            SourceRecord(
                key=f"duplicate-{row}",
                sheet="지원사업",
                row=row,
                values={
                    "당소 사건번호": "SS-DUP",
                    "정부지원사업": program,
                    "협약번호": f"AG-{row}",
                    "약정총액": "300,000원",
                    "증빙유형": "가출원 1건",
                    "인정건수": "1건",
                    "인정대상금액": "500,000원",
                    "정부지원금": "400,000원",
                    "자부담": "100,000원",
                },
                source_refs=[source_ref()],
            )
        )
    runs = AnalysisPipeline(build_default_registry()).analyze_changes(
        [RecordChange(ChangeKind.CREATE, record.key, None, record) for record in records]
    )
    packages: dict[str, dict[str, object]] = {}
    for run in runs:
        for change in run.proposed_changes:
            if change.target_database == "정부지원사업 증빙":
                packages.setdefault(change.entity_key, {})[change.property_name] = (
                    change.proposed_value
                )
    assert len(packages) == 2
    for values in packages.values():
        assert values["실제 비용 합계"] == 300_000
        assert values["인정대상금액"] == 300_000
        assert values["부족액"] == 200_000
        assert values["정부지원금"] == 240_000
        assert values["자부담"] == 60_000
        assert values["중복증빙"] is True
        assert values["증빙상태"] == "검토필요"


def test_evidence_without_any_actual_cost_is_entirely_short_and_needs_review():
    record = SourceRecord(
        key="no-cost",
        sheet="지원사업",
        row=2,
        values={
            "당소 사건번호": "SS-NO-COST",
            "정부지원사업": "지원사업 C",
            "협약번호": "AG-NO-COST",
            "증빙유형": "가출원 1건",
            "인정건수": "1건",
            "인정대상금액": "500,000원",
        },
        source_refs=[source_ref()],
    )
    runs = AnalysisPipeline(build_default_registry()).analyze_changes(
        [RecordChange(ChangeKind.CREATE, record.key, None, record)]
    )
    values = {
        change.property_name: change.proposed_value
        for run in runs
        for change in run.proposed_changes
        if change.target_database == "정부지원사업 증빙"
    }
    assert values["실제 비용 합계"] == 0
    assert values["인정대상금액"] == 0
    assert values["부족액"] == 500_000
    assert values["증빙상태"] == "검토필요"
    assert "관련 실제 비용" not in values


def test_plain_official_fee_is_preserved_as_reviewable_confirmed_cost_candidate():
    record = SourceRecord(
        key="generic-official-fee",
        sheet="비용",
        row=2,
        values={"당소 사건번호": "SS-FEE", "관납료": "120,000원"},
        source_refs=[source_ref()],
    )
    run = AnalysisPipeline(build_default_registry()).analyze_change(
        RecordChange(ChangeKind.CREATE, record.key, None, record)
    )
    fee = next(
        change
        for change in run.proposed_changes
        if change.target_database == "비용·청구"
        and change.property_name == "관납료확정"
    )
    assert fee.proposed_value == 120_000
    assert fee.confidence == 0.72
    assert any(item.title == "관납료 확정·예상 구분 확인" for item in run.review_items)


def test_group_relationship_is_emitted_for_every_case_in_a_multi_case_row():
    record = SourceRecord(
        key="multi-case",
        sheet="지원사업",
        row=2,
        values={
            "당소 사건번호": "SS-1 / SS-2 / SS-3",
            "정부지원사업": "2026 수출바우처",
            "협약번호": "VB-2026-01",
        },
        source_refs=[source_ref()],
    )
    run = AnalysisPipeline(build_default_registry()).analyze_change(
        RecordChange(ChangeKind.CREATE, record.key, None, record)
    )
    group_relations = {
        change.entity_key: change.proposed_value
        for change in run.proposed_changes
        if change.target_database == "한국 특허 사건"
        and change.property_name == "그룹"
    }
    assert group_relations == {
        "SS-1": ["group:VB-2026-01"],
        "SS-2": ["group:VB-2026-01"],
        "SS-3": ["group:VB-2026-01"],
    }


def test_semantic_but_unconsumed_column_generates_inert_draft():
    record = SourceRecord(
        key="semantic-gap",
        sheet="고객관리",
        row=2,
        values={
            "당소 사건번호": "SS-1",
            "고객 진행상태 분류": "VIP",
            "전화번호": "02-1234-5678",
        },
        source_refs=[source_ref()],
    )
    catalog = SourceProfiler().profile([record])
    column = next(
        column
        for sheet in catalog.sheets
        for column in sheet.columns
        if column.header == "고객 진행상태 분류"
    )
    assert "status" in column.semantic_candidates
    assert column.consumer_candidates == []
    assert ("고객관리", "고객 진행상태 분류") in catalog.unconsumed_columns
    telephone = next(
        column
        for sheet in catalog.sheets
        for column in sheet.columns
        if column.header == "전화번호"
    )
    assert "contact" in telephone.semantic_candidates
    assert telephone.consumer_candidates == []
    assert ("고객관리", "전화번호") in catalog.unconsumed_columns
    drafts = SafeDraftAnalyzerGenerator().create_drafts(catalog)
    assert len(drafts) == 2
    assert {column for draft in drafts for column in draft.input_columns} == {
        "고객 진행상태 분류",
        "전화번호",
    }
    assert len({draft.manifest.name for draft in drafts}) == 2
    assert all(draft.activation_allowed is False for draft in drafts)
