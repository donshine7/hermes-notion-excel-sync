from __future__ import annotations

import json

import pytest

import notion_excel_sync.persistence.database as database_module
from notion_excel_sync.models import (
    ChangeKind,
    ProposalAction,
    ProposalOperation,
    ProposalPurpose,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    SourceRef,
    source_basis_digest,
)
from notion_excel_sync.persistence.database import (
    MutationScopeConflict,
    StateDatabase,
)
from notion_excel_sync.workflow.proposals import ProposalError, ProposalService
from notion_excel_sync.workflow.review_cards import HISTORY_DATABASE


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


class _CountingOperationList(list[ProposalOperation]):
    def __init__(self, values: list[ProposalOperation]) -> None:
        super().__init__(values)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def test_large_bulk_override_lookup_indexes_one_owner_once(
    tmp_path,
    monkeypatch,
) -> None:
    database, _proposals = _state(tmp_path)
    owner_operations: list[ProposalOperation] = []
    for index in range(1480):
        operation = ProposalOperation(
            _change(
                f"owner-override-operation-{index}",
                entity_key=f"case:SS-OVERRIDE-{index:05d}",
            )
        )
        operation.action = ProposalAction.EDIT
        operation.approved_value = f"approved-{index}"
        operation.user_override = True
        owner_operations.append(operation)
    owner = ProposalRevision(
        proposal_id="P-LARGE-OVERRIDE-OWNER",
        revision=1,
        source_version_id="v1",
        source_file_hash="a" * 64,
        requested_by="synthetic-user",
        chat_id="synthetic-chat",
        operations=owner_operations,
        status=ProposalStatus.COMMITTED,
    )
    database.save_proposal(owner)
    now = "2026-07-23T00:00:00+00:00"
    with database.session() as connection:
        connection.executemany(
            """
            INSERT INTO approved_override(
                scope_digest, case_number, database_name, entity_key,
                property_name, approved_value, source_basis_digest,
                observed_basis_digest, source_file_hash, proposal_id,
                revision, operation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    database_module._mutation_scope_digest(
                        DATABASE,
                        PROPERTY,
                        (
                            "notion_page",
                            f"synthetic-page-{index:05d}",
                        ),
                    ),
                    f"SS-OVERRIDE-{index:05d}",
                    DATABASE,
                    f"case:SS-OVERRIDE-{index:05d}",
                    PROPERTY,
                    json.dumps(f"approved-{index}"),
                    f"basis-{index}",
                    f"basis-{index}",
                    "a" * 64,
                    owner.proposal_id,
                    owner.revision,
                    f"owner-override-operation-{index}",
                    now,
                    now,
                )
                for index in range(1480)
            ],
        )

    current_operations = [
        ProposalOperation(
            _change(
                f"current-override-operation-{index}",
                entity_key=f"SS-OVERRIDE-{index:05d}",
            )
        )
        for index in range(1480)
    ]
    current = ProposalRevision(
        proposal_id="P-LARGE-OVERRIDE-LOOKUP",
        revision=1,
        source_version_id="v2",
        source_file_hash="b" * 64,
        requested_by="synthetic-user",
        chat_id="synthetic-chat",
        operations=current_operations,
    )
    parse_calls = 0
    parsed_operation_lists: list[_CountingOperationList] = []
    original_parser = database_module._proposal_from_dict

    def counted_parser(payload):
        nonlocal parse_calls
        parse_calls += 1
        proposal = original_parser(payload)
        operations = _CountingOperationList(list(proposal.operations))
        proposal.operations = operations
        parsed_operation_lists.append(operations)
        return proposal

    monkeypatch.setattr(
        database_module,
        "_proposal_from_dict",
        counted_parser,
    )
    candidate_visits = 0
    original_find = database._find_approved_override

    def counted_find(connection, proposal, operation, **kwargs):
        nonlocal candidate_visits
        candidate_visits += len(kwargs.get("candidate_rows") or [])
        return original_find(
            connection,
            proposal,
            operation,
            **kwargs,
        )

    monkeypatch.setattr(
        database,
        "_find_approved_override",
        counted_find,
    )
    statements: list[str] = []
    original_connect = database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "connect", traced_connect)
    results = database.get_approved_overrides_for_operations(
        current,
        current.operations,
    )

    assert len(results) == 1480
    assert all(result is not None for result in results)
    assert results[0]["approved_value"] == "approved-0"
    assert results[-1]["approved_value"] == "approved-1479"
    assert candidate_visits == 1480
    assert parse_calls == 1
    assert len(parsed_operation_lists) == 1
    assert parsed_operation_lists[0].iterations <= 2
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert sum(
        "FROM APPROVED_OVERRIDE" in statement.upper()
        for statement in selects
    ) == 1
    assert sum(
        "FROM PROPOSAL_REVISION" in statement.upper()
        for statement in selects
    ) == 1
    assert sum(
        "FROM ENTITY_MAPPING" in statement.upper()
        for statement in selects
    ) <= 3


def test_bulk_override_conflicting_signatures_still_fail_closed(
    tmp_path,
) -> None:
    database, _proposals = _state(tmp_path)
    owner = ProposalRevision(
        proposal_id="P-CONFLICTING-OVERRIDE-OWNER",
        revision=1,
        source_version_id="v1",
        source_file_hash="a" * 64,
        requested_by="synthetic-user",
        chat_id="synthetic-chat",
        operations=[
            ProposalOperation(
                _change(
                    "conflicting-override-one",
                    entity_key=f"case:{ENTITY}",
                )
            ),
            ProposalOperation(
                _change(
                    "conflicting-override-two",
                    entity_key=f"alias:{ENTITY}",
                )
            ),
        ],
        status=ProposalStatus.COMMITTED,
    )
    database.save_proposal(owner)
    now = "2026-07-23T00:00:00+00:00"
    with database.session() as connection:
        connection.executemany(
            """
            INSERT INTO approved_override(
                scope_digest, case_number, database_name, entity_key,
                property_name, approved_value, source_basis_digest,
                observed_basis_digest, source_file_hash, proposal_id,
                revision, operation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    database_module._mutation_scope_digest(
                        DATABASE,
                        PROPERTY,
                        ("notion_page", f"synthetic-page-{index}"),
                    ),
                    ENTITY,
                    DATABASE,
                    entity_key,
                    PROPERTY,
                    json.dumps(f"approved-{index}"),
                    f"basis-{index}",
                    f"basis-{index}",
                    "a" * 64,
                    owner.proposal_id,
                    owner.revision,
                    operation_id,
                    now,
                    now,
                )
                for index, (entity_key, operation_id) in enumerate(
                    (
                        (f"case:{ENTITY}", "conflicting-override-one"),
                        (f"alias:{ENTITY}", "conflicting-override-two"),
                    )
                )
            ],
        )
    current_operation = ProposalOperation(
        _change("conflicting-override-current")
    )
    current = ProposalRevision(
        proposal_id="P-CONFLICTING-OVERRIDE-LOOKUP",
        revision=1,
        source_version_id="v2",
        source_file_hash="b" * 64,
        requested_by="synthetic-user",
        chat_id="synthetic-chat",
        operations=[current_operation],
    )

    with pytest.raises(
        MutationScopeConflict,
        match="Conflicting approved overrides",
    ):
        database.get_approved_override_for_operation(
            current,
            current_operation,
        )


def test_large_create_uses_one_bulk_override_session_and_skips_history(
    tmp_path,
    monkeypatch,
) -> None:
    database, proposals = _state(tmp_path)
    regular_changes = [
        _change(
            f"bulk-operation-{index}",
            entity_key=f"SS-BULK-{index:05d}",
        )
        for index in range(1480)
    ]
    history_changes = [
        ProposedChange(
            target_database=HISTORY_DATABASE,
            entity_key=f"history:bulk-operation-{index}",
            property_name="Synthetic history value",
            kind=ChangeKind.CREATE,
            current_value=None,
            proposed_value=f"value-{index}",
            analyzer="synthetic_history",
            analyzer_version="1",
            confidence=1.0,
            reason="Synthetic generated history",
            source_refs=[],
            operation_id=f"history-operation-{index}",
        )
        for index in range(20)
    ]
    original_bulk_lookup = (
        database.get_approved_overrides_for_operations
    )
    bulk_calls: list[list[str]] = []

    def tracked_bulk_lookup(proposal, operations):
        operation_list = list(operations)
        bulk_calls.append(
            [
                operation.change.target_database
                for operation in operation_list
            ]
        )
        return original_bulk_lookup(proposal, operation_list)

    monkeypatch.setattr(
        database,
        "get_approved_overrides_for_operations",
        tracked_bulk_lookup,
    )
    monkeypatch.setattr(
        database,
        "get_approved_override_for_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("per-operation override lookup was used")
        ),
    )
    connect_count = 0
    statements: list[str] = []
    original_connect = database.connect

    def traced_connect():
        nonlocal connect_count
        connect_count += 1
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "connect", traced_connect)
    proposal = proposals.create(
        "v-large",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [*regular_changes, *history_changes],
    )

    assert len(proposal.operations) == 1500
    assert len(bulk_calls) == 1
    assert len(bulk_calls[0]) == 1480
    assert set(bulk_calls[0]) == {DATABASE}
    assert connect_count == 3
    assert sum(
        "FROM APPROVED_OVERRIDE" in statement.upper()
        for statement in statements
    ) == 1


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
