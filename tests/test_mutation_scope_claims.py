from __future__ import annotations

import pytest

from notion_excel_sync.models import (
    ChangeKind,
    ProposalAction,
    ProposalPurpose,
    ProposalStatus,
    ProposedChange,
)
from notion_excel_sync.persistence.database import (
    MutationScopeConflict,
    StateDatabase,
)
from notion_excel_sync.security.approval import ApprovalService
from notion_excel_sync.workflow.proposals import ProposalService


def _change(
    operation_id: str,
    *,
    property_name: str = "현재상태",
    entity_key: str = "SS-SYNTHETIC-001",
    current_value: object = "접수",
    proposed_value: object = "진행",
) -> ProposedChange:
    return ProposedChange(
        target_database="한국 특허 사건",
        entity_key=entity_key,
        property_name=property_name,
        kind=ChangeKind.UPDATE,
        current_value=current_value,
        proposed_value=proposed_value,
        analyzer="synthetic",
        analyzer_version="1",
        confidence=1.0,
        reason="합성 변경",
        source_refs=[],
        operation_id=operation_id,
    )


def _database(tmp_path) -> tuple[StateDatabase, ProposalService]:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    return database, ProposalService(database)


def _correction_binding(
    page_id: str,
    *,
    entity_key: str = "case:SS-SYNTHETIC-001",
) -> dict[str, object]:
    return {
        "case_number": "SS-SYNTHETIC-001",
        "target": {
            "page_id": page_id,
            "database": "한국 특허 사건",
            "entity_key": entity_key,
            "property": "현재상태",
        },
    }


def test_excel_and_correction_proposals_block_the_same_scope_both_ways(
    tmp_path,
) -> None:
    for first_purpose, second_purpose in (
        (
            ProposalPurpose.EXCEL_SYNC,
            ProposalPurpose.USER_CORRECTION,
        ),
        (
            ProposalPurpose.USER_CORRECTION,
            ProposalPurpose.EXCEL_SYNC,
        ),
    ):
        case_root = tmp_path / first_purpose.name
        database = StateDatabase(case_root / "state.db")
        database.initialize()
        proposals = ProposalService(database)
        first = proposals.create(
            "v1",
            "a" * 64,
            "user",
            "chat",
            [_change("operation-first")],
            purpose=first_purpose,
        )

        try:
            proposals.create(
                "v1",
                "a" * 64,
                "user",
                "chat",
                [_change("operation-second")],
                purpose=second_purpose,
            )
        except MutationScopeConflict as exc:
            assert "already owns" in str(exc)
        else:
            raise AssertionError("conflicting property proposal was accepted")

        with database.session() as connection:
            proposals_count = connection.execute(
                "SELECT COUNT(*) FROM proposal"
            ).fetchone()[0]
            owner = connection.execute(
                "SELECT proposal_id FROM mutation_scope_claim"
            ).fetchone()[0]
        assert proposals_count == 1
        assert owner == first.proposal_id


@pytest.mark.parametrize(
    ("first_purpose", "second_purpose"),
    [
        (ProposalPurpose.USER_CORRECTION, ProposalPurpose.EXCEL_SYNC),
        (ProposalPurpose.EXCEL_SYNC, ProposalPurpose.USER_CORRECTION),
    ],
)
def test_page_bound_correction_blocks_unmapped_excel_alias_both_ways(
    tmp_path,
    first_purpose: ProposalPurpose,
    second_purpose: ProposalPurpose,
) -> None:
    database, proposals = _database(tmp_path)

    def create(purpose: ProposalPurpose, operation_id: str) -> None:
        is_correction = purpose is ProposalPurpose.USER_CORRECTION
        entity_key = (
            "case:SS-SYNTHETIC-001"
            if is_correction
            else "SS-SYNTHETIC-001"
        )
        proposals.create(
            "v1",
            "a" * 64,
            "user",
            "chat",
            [_change(operation_id, entity_key=entity_key)],
            purpose=purpose,
            correction_binding=(
                _correction_binding("synthetic-page-a")
                if is_correction
                else None
            ),
        )

    create(first_purpose, "cross-identity-first")

    with pytest.raises(MutationScopeConflict, match="already owns"):
        create(second_purpose, "cross-identity-second")

    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM mutation_scope_claim"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0]
            == 1
        )


def test_distinct_page_bound_scopes_with_same_match_value_do_not_collide(
    tmp_path,
) -> None:
    database, proposals = _database(tmp_path)
    database.save_entity_mapping(
        "한국 특허 사건",
        "SS-SYNTHETIC-001",
        "synthetic-page-b",
        "SS-SYNTHETIC-001",
    )
    correction = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [
            _change(
                "strong-page-a",
                entity_key="case:SS-SYNTHETIC-001",
            )
        ],
        purpose=ProposalPurpose.USER_CORRECTION,
        correction_binding=_correction_binding("synthetic-page-a"),
    )
    excel = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("strong-page-b")],
        purpose=ProposalPurpose.EXCEL_SYNC,
    )

    with database.session() as connection:
        owners = {
            str(row["proposal_id"])
            for row in connection.execute(
                "SELECT proposal_id FROM mutation_scope_claim"
            ).fetchall()
        }
    assert owners == {correction.proposal_id, excel.proposal_id}


def test_different_properties_can_be_reviewed_independently(tmp_path) -> None:
    database, proposals = _database(tmp_path)

    first = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("operation-status")],
    )
    second = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("operation-note", property_name="비고")],
    )

    assert first.proposal_id != second.proposal_id
    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM mutation_scope_claim"
            ).fetchone()[0]
            == 2
        )


def test_rejection_and_success_release_the_scope(tmp_path) -> None:
    database, proposals = _database(tmp_path)
    rejected = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("operation-rejected")],
    )
    proposals.reject(rejected.proposal_id, "user", "chat")

    committed = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("operation-committed")],
    )
    committed.status = ProposalStatus.COMMITTED
    database.finalize_success(
        committed,
        drive_id="drive",
        item_id="item",
        version_id="v1",
        file_hash="a" * 64,
    )

    replacement = proposals.create(
        "v2",
        "b" * 64,
        "user",
        "chat",
        [_change("operation-replacement")],
    )
    assert replacement.proposal_id


def test_failed_proposal_retains_scope_for_recovery(tmp_path) -> None:
    _database_state, proposals = _database(tmp_path)
    failed = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("operation-failed")],
    )
    failed.status = ProposalStatus.FAILED
    proposals.database.save_proposal(failed)

    try:
        proposals.create(
            "v1",
            "a" * 64,
            "user",
            "chat",
            [_change("operation-conflict")],
        )
    except MutationScopeConflict:
        pass
    else:
        raise AssertionError("failed proposal released its recovery scope")


def test_deferred_operation_claim_transfers_by_stable_operation_id(
    tmp_path,
) -> None:
    database, proposals = _database(tmp_path)
    deferred = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("operation-stable")],
    )
    deferred.operations[0].action = ProposalAction.DEFER
    deferred.status = ProposalStatus.COMMITTED
    database.finalize_success(
        deferred,
        drive_id="drive",
        item_id="item",
        version_id="v1",
        file_hash="a" * 64,
    )

    next_proposal = proposals.create(
        "v2",
        "b" * 64,
        "user",
        "chat",
        [_change("operation-stable")],
    )

    with database.session() as connection:
        claim = connection.execute(
            "SELECT proposal_id, claim_state FROM mutation_scope_claim"
        ).fetchone()
    assert claim["proposal_id"] == next_proposal.proposal_id
    assert claim["claim_state"] == ProposalStatus.DRAFT.value


def test_initialize_backfills_an_active_proposal_head(tmp_path) -> None:
    database, proposals = _database(tmp_path)
    proposal = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("legacy-active-operation")],
    )
    with database.session() as connection:
        connection.execute("DELETE FROM mutation_scope_claim")

    database.initialize()

    with database.session() as connection:
        claim = connection.execute(
            "SELECT proposal_id, revision, claim_state "
            "FROM mutation_scope_claim"
        ).fetchone()
    assert claim is not None
    assert claim["proposal_id"] == proposal.proposal_id
    assert claim["revision"] == proposal.revision
    assert claim["claim_state"] == ProposalStatus.DRAFT.value


def test_initialize_fails_closed_when_legacy_active_heads_collide(
    tmp_path,
) -> None:
    database, proposals = _database(tmp_path)
    proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("legacy-first", entity_key="SS-SYNTHETIC-001")],
        proposal_id="P-LEGACY-FIRST",
    )
    with database.session() as connection:
        connection.execute("DELETE FROM mutation_scope_claim")
    proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("legacy-second", entity_key="case:SS-SYNTHETIC-001")],
        proposal_id="P-LEGACY-SECOND",
    )
    with database.session() as connection:
        connection.execute("DELETE FROM mutation_scope_claim")

    with pytest.raises(MutationScopeConflict, match="already owns"):
        database.initialize()

    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM mutation_scope_claim"
            ).fetchone()[0]
            == 0
        )


def test_begin_apply_refuses_missing_claim_before_consuming_nonce(
    tmp_path,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    approval = ApprovalService(
        "a-secure-synthetic-secret-at-least-24-characters",
        {"synthetic-user"},
        nonce_used=database.nonce_used,
        mark_nonce_used=database.mark_nonce_used,
    )
    proposals = ProposalService(database, approval)
    proposal = proposals.create(
        "v1",
        "a" * 64,
        "synthetic-user",
        "synthetic-chat",
        [_change("claim-loss-operation")],
    )
    proposal = proposals.request_approval(proposal.proposal_id)
    receipt = proposals.approve(
        proposal.proposal_id,
        proposal.revision,
        "synthetic-user",
        "synthetic-chat",
        proposal.digest,
    )
    with database.session() as connection:
        connection.execute(
            "DELETE FROM mutation_scope_claim WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
    applying = database.load_proposal(proposal.proposal_id)
    applying.status = ProposalStatus.APPLYING

    with pytest.raises(
        MutationScopeConflict,
        match="ownership is incomplete",
    ):
        database.begin_apply(applying, receipt.nonce)

    assert database.nonce_used(receipt.nonce) is False
    assert (
        database.load_proposal(proposal.proposal_id).status
        is ProposalStatus.APPROVED
    )
    assert (
        database.operation_outbox_state(
            proposal.operations[0].change.operation_id
        )
        is None
    )


def test_writer_compatible_entity_key_aliases_share_one_claim(
    tmp_path,
) -> None:
    database, proposals = _database(tmp_path)
    proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("alias-first", entity_key="SS-SYNTHETIC-001")],
    )

    with pytest.raises(MutationScopeConflict, match="already owns"):
        proposals.create(
            "v1",
            "a" * 64,
            "user",
            "chat",
            [_change("alias-second", entity_key="case:SS-SYNTHETIC-001")],
        )


def test_mapping_page_id_canonicalizes_mapped_and_unmapped_aliases(
    tmp_path,
) -> None:
    database, proposals = _database(tmp_path)
    database.save_entity_mapping(
        "한국 특허 사건",
        "case:SS-SYNTHETIC-001",
        "synthetic-page-id",
        "SS-SYNTHETIC-001",
    )
    proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [_change("mapped-first", entity_key="case:SS-SYNTHETIC-001")],
    )

    with pytest.raises(MutationScopeConflict, match="already owns"):
        proposals.create(
            "v1",
            "a" * 64,
            "user",
            "chat",
            [_change("unmapped-second", entity_key="SS-SYNTHETIC-001")],
        )


def test_title_operation_value_is_used_as_canonical_match_identity(
    tmp_path,
) -> None:
    _database_state, proposals = _database(tmp_path)
    first = [
        _change(
            "title-first",
            entity_key="internal:first",
            property_name="당소 사건번호",
            proposed_value="SS-SYNTHETIC-001",
        ),
        _change(
            "status-first",
            entity_key="internal:first",
        ),
    ]
    second = [
        _change(
            "title-second",
            entity_key="internal:second",
            property_name="당소 사건번호",
            proposed_value="SS-SYNTHETIC-001",
        ),
        _change(
            "status-second",
            entity_key="internal:second",
        ),
    ]
    proposals.create("v1", "a" * 64, "user", "chat", first)

    with pytest.raises(MutationScopeConflict, match="already owns"):
        proposals.create("v1", "a" * 64, "user", "chat", second)


def test_non_identical_duplicate_scope_in_one_proposal_is_rejected(
    tmp_path,
) -> None:
    _database_state, proposals = _database(tmp_path)

    with pytest.raises(
        MutationScopeConflict,
        match="multiple non-identical mutations",
    ):
        proposals.create(
            "v1",
            "a" * 64,
            "user",
            "chat",
            [
                _change("duplicate-one", proposed_value="첫 값"),
                _change("duplicate-two", proposed_value="둘째 값"),
            ],
        )


def test_exact_duplicate_with_same_stable_operation_id_shares_claim(
    tmp_path,
) -> None:
    database, proposals = _database(tmp_path)
    proposal = proposals.create(
        "v1",
        "a" * 64,
        "user",
        "chat",
        [
            _change("stable-duplicate"),
            _change("stable-duplicate"),
        ],
    )

    with database.session() as connection:
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM mutation_scope_claim "
            "WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()[0]
    assert claim_count == 1
