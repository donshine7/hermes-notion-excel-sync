from copy import deepcopy

import pytest

from notion_excel_sync.models import (
    ChangeKind,
    KnowledgeRef,
    ProposalAction,
    ProposalOperation,
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
    assert 'Notion: "접수" → "진행"' in first
    assert "Excel 근거: 2026 행 3 [B3] (v2)" in first
    assert "Wiki 근거: 증빙 기준.md (v4, section:2, #cccccccccccc)" in first
    assert "Operation ID: op-1" in first
    assert f"/nx_approve P-SYNTHETIC 2 {digest}" in first
    assert "Notion과 Wiki는 아직 변경되지 않았습니다." in first


def test_orphan_history_is_reported_but_never_consumes_a_card() -> None:
    revision = proposal([history_operation("missing-operation", 1)])

    page = review_card_page(revision)

    assert page.total_cards == 0
    assert page.total_pages == 1
    assert page.cards == ()
    assert page.automatic_history_operations == 1
    assert page.unmerged_history_operations == 1
    assert "검토할 논리 변경 카드가 없습니다." in render_review_card_page(revision)


@pytest.mark.parametrize("page", [0, 2])
def test_page_bounds_are_rejected(page: int) -> None:
    with pytest.raises(ReviewCardPageError, match="valid pages are 1-1"):
        review_card_page(proposal([operation("op-1")]), page)
