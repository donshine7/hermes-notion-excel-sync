from __future__ import annotations

import pytest

from notion_excel_sync.models import (
    ChangeKind,
    ProposalAction,
    ProposalPurpose,
    ProposalStatus,
    ProposedChange,
    SourceRef,
    source_basis_digest,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.workflow.proposals import ProposalError, ProposalService


DATABASE = "한국 특허 사건"
PROPERTY = "현재상태"
ENTITY = "SS-SYNTHETIC-001"


def _source_ref(
    *,
    raw_value: object = "Excel 값",
    version_id: str = "v1",
    file_hash: str = "a" * 64,
    drive_id: str = "synthetic-drive",
    item_id: str = "synthetic-item",
    sheet: str = "2026",
    row: int = 2,
    cells: str = "H2",
) -> SourceRef:
    return SourceRef(
        drive_id=drive_id,
        item_id=item_id,
        version_id=version_id,
        file_hash=file_hash,
        sheet=sheet,
        row=row,
        cells=cells,
        raw_value=raw_value,
    )


def _change(
    operation_id: str,
    *,
    entity_key: str = ENTITY,
    current_value: object = "현재 Notion 값",
    proposed_value: object = "Excel 제안값",
    raw_value: object = "Excel 값",
    version_id: str = "v1",
    file_hash: str = "a" * 64,
    source_refs: list[SourceRef] | None = None,
) -> ProposedChange:
    return ProposedChange(
        target_database=DATABASE,
        entity_key=entity_key,
        property_name=PROPERTY,
        kind=ChangeKind.UPDATE,
        current_value=current_value,
        proposed_value=proposed_value,
        analyzer="synthetic",
        analyzer_version="1",
        confidence=1.0,
        reason="합성 Excel 변경",
        source_refs=(
            source_refs
            if source_refs is not None
            else [
                _source_ref(
                    raw_value=raw_value,
                    version_id=version_id,
                    file_hash=file_hash,
                )
            ]
        ),
        operation_id=operation_id,
    )


def _state(tmp_path) -> tuple[StateDatabase, ProposalService]:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    return database, ProposalService(database)


def _commit(
    database: StateDatabase,
    proposal,
    *,
    version_id: str = "v1",
    file_hash: str = "a" * 64,
) -> None:
    proposal.status = ProposalStatus.COMMITTED
    database.finalize_success(
        proposal,
        drive_id="synthetic-drive",
        item_id="synthetic-item",
        version_id=version_id,
        file_hash=file_hash,
    )


def _seed_override(
    database: StateDatabase,
    proposals: ProposalService,
    *,
    approved_value: object = "사용자 승인값",
):
    proposal = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [_change("seed-operation")],
    )
    operation = proposal.operations[0]
    operation.action = ProposalAction.EDIT
    operation.approved_value = approved_value
    operation.user_override = True
    _commit(database, proposal)
    return database.get_approved_override(DATABASE, ENTITY, PROPERTY)


def test_source_basis_ignores_workbook_identity_but_detects_cited_cell_change() -> None:
    first = _source_ref()
    unrelated_workbook_revision = _source_ref(
        drive_id="another-drive",
        item_id="another-item",
        version_id="v99",
        file_hash="f" * 64,
    )

    assert source_basis_digest([first]) == source_basis_digest(
        [unrelated_workbook_revision]
    )
    assert source_basis_digest([first]) != source_basis_digest(
        [_source_ref(raw_value="변경된 Excel 값")]
    )
    assert source_basis_digest([first]) != source_basis_digest(
        [_source_ref(cells="I2")]
    )
    assert source_basis_digest([first]) != source_basis_digest(
        [_source_ref(row=3)]
    )


def test_successful_user_override_is_persisted_and_reused_for_same_basis(
    tmp_path,
) -> None:
    database, proposals = _state(tmp_path)
    seeded = _seed_override(database, proposals)
    assert seeded is not None
    original_basis = seeded["source_basis_digest"]

    later = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "later-operation",
                version_id="v2",
                file_hash="b" * 64,
            )
        ],
    )
    operation = later.operations[0]

    assert operation.action is ProposalAction.APPLY
    assert operation.user_override is False
    assert operation.approved_value == "사용자 승인값"
    assert operation.change.proposed_value == "Excel 제안값"
    assert "[approved_override_retained]" in operation.change.reason
    assert (
        source_basis_digest(operation.change.source_refs)
        == original_basis
    )


def test_same_basis_with_current_override_value_is_auto_excluded(tmp_path) -> None:
    database, proposals = _state(tmp_path)
    _seed_override(database, proposals)

    later = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "already-current-operation",
                current_value="사용자 승인값",
                version_id="v2",
                file_hash="b" * 64,
            )
        ],
    )

    operation = later.operations[0]
    assert operation.action is ProposalAction.EXCLUDE
    assert operation.user_override is False
    assert operation.approved_value == "사용자 승인값"
    _commit(
        database,
        later,
        version_id="v2",
        file_hash="b" * 64,
    )
    assert (
        database.get_approved_override(DATABASE, ENTITY, PROPERTY)[
            "approved_value"
        ]
        == "사용자 승인값"
    )


def test_changed_source_basis_is_visible_and_approved_excel_apply_clears_override(
    tmp_path,
) -> None:
    database, proposals = _state(tmp_path)
    _seed_override(database, proposals)

    changed = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "changed-source-operation",
                raw_value="원본에서 변경된 값",
                proposed_value="변경된 Excel 제안값",
                version_id="v2",
                file_hash="b" * 64,
            )
        ],
    )
    operation = changed.operations[0]

    assert operation.action is ProposalAction.APPLY
    assert operation.user_override is False
    assert operation.approved_value == "변경된 Excel 제안값"
    assert "[approved_override_source_conflict]" in operation.change.reason

    _commit(
        database,
        changed,
        version_id="v2",
        file_hash="b" * 64,
    )
    assert database.get_approved_override(DATABASE, ENTITY, PROPERTY) is None


def test_exclude_and_defer_do_not_clear_a_source_conflicted_override(
    tmp_path,
) -> None:
    for action in (ProposalAction.EXCLUDE, ProposalAction.DEFER):
        database, proposals = _state(tmp_path / action.value)
        _seed_override(database, proposals)
        proposal = proposals.create(
            "v2",
            "b" * 64,
            "synthetic-user",
            "synthetic-chat",
            [
                _change(
                    f"{action.value}-operation",
                    raw_value="원본에서 변경된 값",
                    version_id="v2",
                    file_hash="b" * 64,
                )
            ],
        )
        proposal.operations[0].action = action
        proposal.operations[0].user_override = False

        _commit(
            database,
            proposal,
            version_id="v2",
            file_hash="b" * 64,
        )
        assert (
            database.get_approved_override(DATABASE, ENTITY, PROPERTY)[
                "approved_value"
            ]
            == "사용자 승인값"
        )


def test_independent_correction_preserves_existing_stable_excel_basis(
    tmp_path,
) -> None:
    database, proposals = _state(tmp_path)
    seeded = _seed_override(database, proposals)
    assert seeded is not None

    correction = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "correction-operation",
                proposed_value="독립 수정값",
                source_refs=[],
            )
        ],
        purpose=ProposalPurpose.USER_CORRECTION,
        correction_binding={
            "case_number": ENTITY,
            "mutation_scope": [DATABASE, ENTITY, PROPERTY],
        },
    )
    _commit(
        database,
        correction,
        version_id="v2",
        file_hash="b" * 64,
    )

    stored = database.get_approved_override(DATABASE, ENTITY, PROPERTY)
    assert stored is not None
    assert stored["approved_value"] == "독립 수정값"
    assert stored["source_basis_digest"] == seeded["source_basis_digest"]
    assert stored["observed_basis_digest"] == ""


def test_unbound_independent_correction_requires_conflict_review_on_next_excel_sync(
    tmp_path,
) -> None:
    database, proposals = _state(tmp_path)
    correction = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "unbound-correction-operation",
                proposed_value="독립 수정값",
                source_refs=[],
            )
        ],
        purpose=ProposalPurpose.USER_CORRECTION,
    )
    _commit(database, correction)
    stored = database.get_approved_override(DATABASE, ENTITY, PROPERTY)
    assert stored is not None
    assert stored["source_basis_digest"] == ""

    excel = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "excel-after-unbound-correction",
                version_id="v2",
                file_hash="b" * 64,
            )
        ],
    )
    operation = excel.operations[0]
    assert operation.user_override is False
    assert operation.approved_value == "Excel 제안값"
    assert "[approved_override_source_conflict]" in operation.change.reason


def test_recovery_exclusion_persists_override_that_was_already_written(
    tmp_path,
) -> None:
    database, proposals = _state(tmp_path)
    proposal = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [_change("recovered-operation")],
    )
    operation = proposal.operations[0]
    operation.action = ProposalAction.EDIT
    operation.approved_value = "복구된 승인값"
    operation.user_override = True
    database.enqueue_operations(proposal)
    database.mark_outbox(operation.change.operation_id, "done")
    operation.action = ProposalAction.EXCLUDE

    _commit(database, proposal)

    stored = database.get_approved_override(DATABASE, ENTITY, PROPERTY)
    assert stored is not None
    assert stored["approved_value"] == "복구된 승인값"


def test_suffix_alias_reuses_same_basis_override(tmp_path) -> None:
    database, proposals = _state(tmp_path)
    original = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "alias-seed-operation",
                entity_key=f"case:{ENTITY}",
            )
        ],
    )
    original_operation = original.operations[0]
    original_operation.action = ProposalAction.EDIT
    original_operation.approved_value = "별칭 승인값"
    original_operation.user_override = True
    _commit(database, original)

    excel_alias = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "alias-excel-operation",
                entity_key=ENTITY,
                version_id="v2",
                file_hash="b" * 64,
            )
        ],
    )

    operation = excel_alias.operations[0]
    assert operation.approved_value == "별칭 승인값"
    assert "[approved_override_retained]" in operation.change.reason


def test_page_bound_correction_alias_is_found_by_suffix_excel_alias(
    tmp_path,
) -> None:
    database, proposals = _state(tmp_path)
    correction_entity = f"case:{ENTITY}"
    correction = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "page-bound-correction",
                entity_key=correction_entity,
                proposed_value="페이지 승인값",
                source_refs=[],
            )
        ],
        purpose=ProposalPurpose.USER_CORRECTION,
        correction_binding={
            "case_number": ENTITY,
            "target": {
                "version": 1,
                "database": DATABASE,
                "data_source_id": "synthetic-data-source",
                "entity_key": correction_entity,
                "property": PROPERTY,
                "page_id": "synthetic-page-id",
                "parent": {
                    "type": "data_source_id",
                    "id": "synthetic-data-source",
                },
            },
        },
    )
    _commit(database, correction)

    excel_alias = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "excel-after-page-correction",
                entity_key=ENTITY,
                version_id="v2",
                file_hash="b" * 64,
            )
        ],
    )
    operation = excel_alias.operations[0]

    assert operation.approved_value == "Excel 제안값"
    assert "[approved_override_source_conflict]" in operation.change.reason


def test_mapped_alias_resolves_page_bound_correction_override(
    tmp_path,
) -> None:
    database, proposals = _state(tmp_path)
    correction_entity = f"case:{ENTITY}"
    database.save_entity_mapping(
        DATABASE,
        correction_entity,
        "synthetic-page-id",
        ENTITY,
    )
    correction = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "mapped-page-correction",
                entity_key=correction_entity,
                proposed_value="매핑 승인값",
                source_refs=[],
            )
        ],
        purpose=ProposalPurpose.USER_CORRECTION,
        correction_binding={
            "case_number": ENTITY,
            "target": {
                "version": 1,
                "database": DATABASE,
                "data_source_id": "synthetic-data-source",
                "entity_key": correction_entity,
                "property": PROPERTY,
                "page_id": "synthetic-page-id",
                "parent": {
                    "type": "data_source_id",
                    "id": "synthetic-data-source",
                },
            },
        },
    )
    _commit(database, correction)

    excel_alias = proposals.create(
        "v2",
        "b" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                "mapped-excel-alias",
                entity_key=ENTITY,
                version_id="v2",
                file_hash="b" * 64,
            )
        ],
    )

    assert (
        "[approved_override_source_conflict]"
        in excel_alias.operations[0].change.reason
    )


@pytest.mark.parametrize(
    "action",
    [ProposalAction.APPLY, ProposalAction.DEFER],
)
def test_user_correction_rejects_apply_and_defer_edits(
    tmp_path,
    action: ProposalAction,
) -> None:
    _database_state, proposals = _state(tmp_path / action.value)
    proposal = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                f"correction-{action.value}",
                proposed_value="독립 수정값",
                source_refs=[],
            )
        ],
        purpose=ProposalPurpose.USER_CORRECTION,
    )

    with pytest.raises(
        ProposalError,
        match="may only be edited or excluded",
    ):
        proposals.edit(
            proposal.proposal_id,
            proposal.operations[0].change.operation_id,
            action,
        )


@pytest.mark.parametrize(
    ("action", "value"),
    [
        (ProposalAction.EDIT, "수정된 독립 수정값"),
        (ProposalAction.EXCLUDE, None),
    ],
)
def test_user_correction_allows_edit_or_exclude(
    tmp_path,
    action: ProposalAction,
    value: object,
) -> None:
    _database_state, proposals = _state(tmp_path / action.value)
    proposal = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [
            _change(
                f"allowed-correction-{action.value}",
                proposed_value="독립 수정값",
                source_refs=[],
            )
        ],
        purpose=ProposalPurpose.USER_CORRECTION,
    )
    operation_id = proposal.operations[0].change.operation_id

    if action is ProposalAction.EDIT:
        revised = proposals.edit(
            proposal.proposal_id,
            operation_id,
            action,
            value,
        )
        assert revised.operations[0].approved_value == value
        assert revised.operations[0].user_override is True
    else:
        revised = proposals.edit(
            proposal.proposal_id,
            operation_id,
            action,
        )
        assert revised.operations[0].action is ProposalAction.EXCLUDE
