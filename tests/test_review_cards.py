from copy import deepcopy

import pytest

from notion_excel_sync.models import (
    ChangeKind,
    CORRECTION_OVERLAY_RECOVERY_MODE,
    KnowledgeRef,
    ProposalAction,
    ProposalOperation,
    ProposalPurpose,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    SourceRef,
)
from notion_excel_sync.workflow.review_cards import (
    DEFAULT_CARDS_PER_PAGE,
    ReviewCardPageError,
    build_review_cards,
    render_review_card_page,
    review_card_page,
)


def source_ref(
    *,
    sheet: str = "2026",
    row: int = 3,
    cells: str = "B3",
) -> SourceRef:
    return SourceRef(
        drive_id="local-drive",
        item_id="synthetic-workbook",
        version_id="v2",
        file_hash="a" * 64,
        sheet=sheet,
        row=row,
        cells=cells,
    )


def knowledge_ref() -> KnowledgeRef:
    return KnowledgeRef(
        drive_id="wiki-drive",
        item_id="policy-1",
        version_id="v4",
        content_hash="b" * 64,
        chunk_locator="section:2",
        chunk_hash="c" * 64,
        security_classification="internal",
        display_name="증빙 기준.md",
    )


def operation(
    operation_id: str,
    *,
    entity_key: str = "SS-2026-001",
    database: str = "한국 특허 사건",
    property_name: str = "현재상태",
    current: object = "접수",
    proposed: object = "진행",
    action: ProposalAction = ProposalAction.APPLY,
    refs: list[SourceRef] | None = None,
    wiki_refs: list[KnowledgeRef] | None = None,
) -> ProposalOperation:
    return ProposalOperation(
        change=ProposedChange(
            target_database=database,
            entity_key=entity_key,
            property_name=property_name,
            kind=ChangeKind.UPDATE,
            current_value=current,
            proposed_value=proposed,
            analyzer="synthetic_analyzer",
            analyzer_version="1.0.0",
            confidence=0.9,
            reason="Excel 원본의 상태를\n정규화했습니다.",
            source_refs=list(refs or [source_ref()]),
            knowledge_refs=list(wiki_refs or []),
            operation_id=operation_id,
        ),
        action=action,
        approved_value=proposed,
        user_override=action is ProposalAction.EDIT,
    )


def proposal(operations: list[ProposalOperation]) -> ProposalRevision:
    return ProposalRevision(
        proposal_id="P-SYNTHETIC",
        revision=2,
        source_version_id="v2",
        source_file_hash="d" * 64,
        requested_by="telegram-user",
        chat_id="telegram-chat",
        operations=operations,
        status=ProposalStatus.PENDING_APPROVAL,
        knowledge_binding=(
            {"generation_digest": "e" * 64}
            if any(item.change.knowledge_refs for item in operations)
            else {}
        ),
    )


def history_operation(source_operation_id: str, index: int) -> ProposalOperation:
    return operation(
        f"history-{source_operation_id}-{index}",
        entity_key=f"history:{source_operation_id}",
        database="변경이력",
        property_name=f"필드-{index}",
        current=None,
        proposed=f"값-{index}",
    )


def test_default_pages_contain_five_logical_cards_and_exclude_history() -> None:
    main = [
        operation(
            f"op-{index}",
            entity_key=f"SS-{index:03}",
            refs=[source_ref(row=index + 1, cells=f"B{index + 1}")],
        )
        for index in range(1, 12)
    ]
    histories = [
        history_operation(item.change.operation_id, index)
        for item in main
        for index in (1, 2)
    ]
    revision = proposal([*main, *histories])

    first = review_card_page(revision)
    second = review_card_page(revision, 2)
    third = review_card_page(revision, 3)

    assert DEFAULT_CARDS_PER_PAGE == 5
    assert first.total_cards == 11
    assert first.total_pages == 3
    assert [len(first.cards), len(second.cards), len(third.cards)] == [5, 5, 1]
    assert first.automatic_history_operations == 22
    assert first.unmerged_history_operations == 0
    assert first.cards[0].automatic_operation_ids == (
        "history-op-1-1",
        "history-op-1-2",
    )
    assert first.cards[0].automatic_notion_impact == "변경이력 2개 operation 자동 동반"


def test_card_contains_exact_review_and_command_data() -> None:
    changed = operation(
        "op-edit",
        current={"상태": "접수"},
        proposed={"상태": "보완"},
        action=ProposalAction.EDIT,
        refs=[
            source_ref(cells="C3"),
            source_ref(cells="A3"),
            source_ref(cells="A3"),
        ],
        wiki_refs=[knowledge_ref(), knowledge_ref()],
    )
    changed.approved_value = {"상태": "사용자 수정"}
    revision = proposal([changed, history_operation("op-edit", 1)])

    card = build_review_cards(revision)[0]

    assert card.case_number == "SS-2026-001"
    assert card.target_database == "한국 특허 사건"
    assert card.property_name == "현재상태"
    assert card.current_value == {"상태": "접수"}
    assert card.approved_value == {"상태": "사용자 수정"}
    assert card.excel_source_refs == ("2026 행 3 [A3, C3] (v2)",)
    assert card.knowledge_refs == (
        "증빙 기준.md (v4, section:2, #cccccccccccc)",
    )
    assert (
        card.change_basis
        == "Excel 원본의 상태를 정규화했습니다. | Excel 1개 위치 | Wiki 1개 인용"
    )
    assert "사용자 수정값" in card.wiki_impact
    assert card.commands.apply == "/nx_set P-SYNTHETIC 2 op-edit apply"
    assert card.commands.exclude == "/nx_set P-SYNTHETIC 2 op-edit exclude"
    assert card.commands.defer == "/nx_set P-SYNTHETIC 2 op-edit defer"
    assert card.commands.edit == "/nx_set P-SYNTHETIC 2 op-edit edit <JSON>"


def test_case_number_prefers_entity_key_then_uses_same_row_reverse_lookup() -> None:
    case_identity = operation(
        "op-case",
        entity_key="SS-ROW-9",
        property_name="당소 사건번호",
        current=None,
        proposed="SS-ROW-9",
        refs=[source_ref(sheet="등록결정", row=9, cells="A9")],
    )
    own_key = operation(
        "op-own",
        entity_key="ENTITY-FIRST",
        database="업무·절차",
        property_name="업무명",
        refs=[source_ref(sheet="등록결정", row=9, cells="D9")],
    )
    empty_key = operation(
        "op-empty",
        entity_key="",
        database="업무·절차",
        property_name="완료일",
        refs=[source_ref(sheet="등록결정", row=9, cells="E9")],
    )

    cards = build_review_cards(proposal([case_identity, own_key, empty_key]))

    assert cards[1].case_number == "ENTITY-FIRST"
    assert cards[2].case_number == "SS-ROW-9"


def test_render_is_deterministic_and_does_not_change_digest_or_revision() -> None:
    revision = proposal(
        [
            operation("op-1", wiki_refs=[knowledge_ref()]),
            history_operation("op-1", 1),
        ]
    )
    before = deepcopy(revision)
    digest = revision.digest

    first = render_review_card_page(revision)
    second = render_review_card_page(revision)

    assert first == second
    assert revision == before
    assert revision.digest == digest
    assert "사건번호: SS-2026-001" in first
    assert '- 현재상태: "접수" → "진행" [반영]' in first
    assert "원본 위치: Excel 2026 시트 3행" in first
    assert "Wiki 참고: 증빙 기준.md (v4, section:2, #cccccccccccc)" in first
    assert "/nx_show P-SYNTHETIC 2 1 detail 1" in first
    assert "Operation ID: op-1" not in first
    detail = render_review_card_page(revision, detail_item=1)
    assert "Operation ID: op-1" in detail
    assert f"/nx_approve P-SYNTHETIC 2 {digest}" in first
    assert "Notion과 Wiki는 아직 변경되지 않았습니다." in first


def test_overlay_only_recovery_shows_completed_notion_write_and_only_approval() -> None:
    recovered = operation(
        "op-overlay-recovery",
        current="접수",
        proposed="진행",
        action=ProposalAction.EXCLUDE,
        refs=[],
    )
    recovered.approved_value = "진행"
    recovered.user_override = True
    revision = proposal([recovered])
    revision.purpose = ProposalPurpose.USER_CORRECTION
    revision.correction_binding = {
        "recovery": {
            "mode": CORRECTION_OVERLAY_RECOVERY_MODE,
            "notion_writes": "already_done",
        }
    }

    rendered = render_review_card_page(revision)

    assert '- 현재상태: "진행" (이전 승인에서 반영 완료)' in rendered
    assert "승인 후 Wiki: Wiki 승인 오버레이만 복구" in rendered
    assert "이번 승인은 Notion을 다시 쓰지 않고 Wiki 승인 오버레이와 " in rendered
    assert "최종화만 복구합니다." in rendered
    assert '"접수" → "진행"' not in rendered
    assert "/nx_set" not in rendered
    assert "/nx_reject" not in rendered
    assert f"/nx_approve P-SYNTHETIC 2 {revision.digest}" in rendered
    assert "Notion과 Wiki는 아직 변경되지 않았습니다." not in rendered


def test_internal_source_key_is_replaced_with_human_evidence_title() -> None:
    entity_key = f"source:sha256:{'f' * 64}:2026:10"
    title = operation(
        "op-title",
        entity_key=entity_key,
        database="근거자료",
        property_name="근거명",
        current=None,
        proposed=f"Excel 2026 10행 (vsha256:{'f' * 64})",
        refs=[source_ref(row=10, cells="A10")],
    )
    digest = operation(
        "op-digest",
        entity_key=entity_key,
        database="근거자료",
        property_name="SHA256",
        current=None,
        proposed="f" * 64,
        refs=[source_ref(row=10, cells="A10:L10")],
    )

    rendered = render_review_card_page(proposal([title, digest]))

    assert "페이지: 1/1 (Notion 페이지 1개, 화면당 5개)" in rendered
    assert "1. 근거자료 새 페이지 만들기" in rendered
    assert "자료: Excel 2026 10행" in rendered
    assert "원본 위치: Excel 2026 시트 10행" in rendered
    assert "파일 식별값(SHA256)" in rendered
    assert "Operation ID" not in rendered
    assert entity_key not in rendered


def test_party_page_uses_human_party_label_instead_of_case_number() -> None:
    party_name = operation(
        "op-party-name",
        entity_key="party:amc",
        database="당사자",
        property_name="당사자명",
        current=None,
        proposed="AMC",
        refs=[source_ref(row=107, cells="C107")],
    )
    party_type = operation(
        "op-party-type",
        entity_key="party:amc",
        database="당사자",
        property_name="구분",
        current=None,
        proposed="기타",
        refs=[source_ref(row=107, cells="C107")],
    )

    rendered = render_review_card_page(proposal([party_name, party_type]))

    assert "당사자: AMC" in rendered
    assert "사건번호: party:amc" not in rendered
    assert "party:amc" not in rendered


def test_render_paginates_by_notion_page_instead_of_property_operation() -> None:
    operations = [
        operation(
            f"op-{entity}-{field}",
            entity_key=f"ENTITY-{entity}",
            property_name=f"필드-{field}",
        )
        for entity in range(25)
        for field in range(9)
    ]
    revision = proposal(operations)

    first = render_review_card_page(revision)
    fifth = render_review_card_page(revision, 5)

    assert "페이지: 1/5 (Notion 페이지 25개, 화면당 5개)" in first
    assert "속성 변경: 225건" in first
    assert f"/nx_show {revision.proposal_id} {revision.revision} 2" in first
    assert "페이지: 5/5 (Notion 페이지 25개, 화면당 5개)" in fifth


def test_orphan_history_is_reported_but_never_consumes_a_card() -> None:
    revision = proposal([history_operation("missing-operation", 1)])

    page = review_card_page(revision)
    rendered = render_review_card_page(revision)

    assert page.total_cards == 0
    assert page.total_pages == 1
    assert page.cards == ()
    assert page.automatic_history_operations == 1
    assert page.unmerged_history_operations == 1
    assert page.approve_command is None
    assert "검토할 Notion 페이지 변경이 없습니다." in rendered
    assert "승인 불가" in rendered
    assert "/nx_approve" not in rendered


@pytest.mark.parametrize("page", [0, 2])
def test_page_bounds_are_rejected(page: int) -> None:
    with pytest.raises(ReviewCardPageError, match="valid pages are 1-1"):
        review_card_page(proposal([operation("op-1")]), page)
