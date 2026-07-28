from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from notion_excel_sync.adapters.notion import (
    ApprovalRequiredError,
    InMemoryNotionGateway,
    NotionMatch,
    NotionPrecondition,
    NotionPropertyMutation,
    NotionError,
)
from notion_excel_sync.models import (
    ApprovalReceipt,
    ChangeKind,
    ProposalOperation,
    ProposalStatus,
    ProposedChange,
    SourceRef,
    WritePreconditionState,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.approval import ApprovalError, ApprovalService
from notion_excel_sync.workflow.apply import ApprovalGatedApplyService
from notion_excel_sync.workflow.notion_writer import (
    NOTION_DEFINITIONS,
    ApprovedNotionWriter,
    ExactNotionMutationVerifier,
    NotionSchemaError,
    build_notion_binding,
)
from notion_excel_sync.workflow.proposals import ProposalService


def _source_ref(raw_value: object = "synthetic") -> SourceRef:
    return SourceRef(
        drive_id="synthetic-drive",
        item_id="synthetic-item",
        version_id="v2",
        file_hash="f" * 64,
        sheet="2026",
        row=2,
        cells="A2",
        raw_value=raw_value,
    )


def _change(
    property_name: str,
    current_value: object,
    proposed_value: object,
    *,
    database_name: str = "한국 특허 사건",
    entity_key: str = "CASE-001",
    kind: ChangeKind = ChangeKind.UPDATE,
) -> ProposedChange:
    return ProposedChange(
        target_database=database_name,
        entity_key=entity_key,
        property_name=property_name,
        kind=kind,
        current_value=current_value,
        proposed_value=proposed_value,
        analyzer="synthetic_analyzer",
        analyzer_version="1.0.0",
        confidence=1.0,
        reason="synthetic exact change",
        source_refs=[_source_ref(proposed_value)],
    )


def _workflow(
    tmp_path: Path,
    *,
    data_sources: dict[str, str] | None = None,
) -> tuple[StateDatabase, ApprovalService, ProposalService]:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    approval = ApprovalService(
        secret="synthetic-approval-secret-at-least-24-characters",
        allowed_users={"100"},
        nonce_used=database.nonce_used,
        mark_nonce_used=database.mark_nonce_used,
    )
    proposals = ProposalService(
        database,
        approval,
        notion_data_sources=data_sources or {"한국 특허 사건": "cases"},
    )
    return database, approval, proposals


def _approved(
    proposals: ProposalService,
    changes: list[ProposedChange],
    *,
    knowledge_binding: dict[str, object] | None = None,
):
    proposal = proposals.create(
        "v2",
        "f" * 64,
        "100",
        "synthetic-chat",
        changes,
        knowledge_binding=knowledge_binding,
    )
    proposal = proposals.request_approval(proposal.proposal_id)
    receipt = proposals.approve(
        proposal.proposal_id,
        proposal.revision,
        "100",
        "synthetic-chat",
        proposal.digest,
    )
    return proposal, receipt


def _receipt() -> ApprovalReceipt:
    now = datetime.now(UTC)
    return ApprovalReceipt(
        proposal_id="P-SYNTHETIC",
        revision=1,
        proposal_digest="d" * 64,
        source_version_id="v2",
        source_file_hash="f" * 64,
        telegram_user_id="100",
        chat_id="synthetic-chat",
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
        nonce="synthetic-nonce",
        signature="s" * 64,
        notion_binding_digest="b" * 64,
    )


def test_gateway_batch_rejects_mixed_duplicate_and_unapproved_scope() -> None:
    verified: list[str] = []

    def verifier(_receipt, operation_id, *_args):
        verified.append(operation_id)
        return operation_id in {"title-op", "status-op"}

    gateway = InMemoryNotionGateway(verifier)
    match = NotionMatch("당소 사건번호", "title")
    title = NotionPropertyMutation(
        "title-op",
        "당소 사건번호",
        {"title": [{"text": {"content": "CASE-001"}}]},
        NotionPrecondition(None, None, "당소 사건번호", None),
    )
    status = NotionPropertyMutation(
        "status-op",
        "현재상태",
        {"select": {"name": "출원"}},
        NotionPrecondition(None, None, "현재상태", None),
    )
    mixed = NotionPropertyMutation(
        "status-op",
        "현재상태",
        {"select": {"name": "출원"}},
        NotionPrecondition("another-page", None, "현재상태", None),
    )
    with pytest.raises(ApprovalRequiredError, match="mixed page"):
        gateway.upsert_page_batch(
            "cases",
            match,
            "CASE-001",
            [title, mixed],
            approval=_receipt(),
        )
    assert verified == []
    with pytest.raises(ApprovalRequiredError, match="duplicate property"):
        gateway.upsert_page_batch(
            "cases",
            match,
            "CASE-001",
            [title, NotionPropertyMutation(
                "other-op",
                "당소 사건번호",
                title.property_value,
                title.precondition,
            )],
            approval=_receipt(),
        )
    assert verified == []
    with pytest.raises(ApprovalRequiredError, match="not covered"):
        gateway.upsert_page_batch(
            "cases",
            match,
            "CASE-001",
            [
                title,
                NotionPropertyMutation(
                    "unapproved-op",
                    "현재상태",
                    status.property_value,
                    status.precondition,
                ),
            ],
            approval=_receipt(),
        )
    assert gateway.find_page("cases", match, "CASE-001") is None


def test_one_stale_property_blocks_entire_page_batch_without_write() -> None:
    gateway = InMemoryNotionGateway(
        lambda *_: True,
        initial_pages={
            "cases": [
                {
                    "id": "page-1",
                    "properties": {
                        "당소 사건번호": {
                            "title": [{"plain_text": "CASE-001"}]
                        },
                        "현재상태": {"select": {"name": "외부변경"}},
                        "내부상태": {"select": {"name": "대기"}},
                    },
                }
            ]
        },
    )
    match = NotionMatch("당소 사건번호", "title")
    mutations = [
        NotionPropertyMutation(
            "status-op",
            "현재상태",
            {"select": {"name": "출원"}},
            NotionPrecondition(
                "page-1",
                None,
                "현재상태",
                {"select": {"name": "착수"}},
            ),
        ),
        NotionPropertyMutation(
            "internal-op",
            "내부상태",
            {"select": {"name": "진행"}},
            NotionPrecondition(
                "page-1",
                None,
                "내부상태",
                {"select": {"name": "대기"}},
            ),
        ),
    ]
    with pytest.raises(NotionError, match="property changed"):
        gateway.upsert_page_batch(
            "cases",
            match,
            "CASE-001",
            mutations,
            approval=_receipt(),
        )
    page = gateway.get_page("page-1")
    assert page["properties"]["현재상태"]["select"]["name"] == "외부변경"
    assert page["properties"]["내부상태"]["select"]["name"] == "대기"


def test_current_title_drift_blocks_batch_before_mutation() -> None:
    gateway = InMemoryNotionGateway(
        lambda *_: True,
        initial_pages={
            "cases": [
                {
                    "id": "page-1",
                    "properties": {
                        "당소 사건번호": {
                            "title": [{"plain_text": "DRIFTED"}]
                        },
                        "현재상태": {"select": {"name": "착수"}},
                    },
                }
            ]
        },
    )
    with pytest.raises(NotionError, match="match value changed"):
        gateway.upsert_page_batch(
            "cases",
            NotionMatch("당소 사건번호", "title"),
            "CASE-001",
            [
                NotionPropertyMutation(
                    "status-op",
                    "현재상태",
                    {"select": {"name": "출원"}},
                    NotionPrecondition(
                        "page-1",
                        None,
                        "현재상태",
                        {"select": {"name": "착수"}},
                    ),
                )
            ],
            approval=_receipt(),
        )
    assert (
        gateway.get_page("page-1")["properties"]["현재상태"]["select"]["name"]
        == "착수"
    )


@pytest.mark.parametrize(
    ("resuming", "expected_status"),
    [
        (False, ProposalStatus.STALE),
        (True, ProposalStatus.FAILED),
    ],
)
def test_global_binding_exception_closes_scope_without_notion_write(
    tmp_path: Path,
    resuming: bool,
    expected_status: ProposalStatus,
) -> None:
    case_database = "한국 특허 사건"
    definition = NOTION_DEFINITIONS[case_database]
    property_name = next(
        name
        for name, property_type in definition.properties.items()
        if property_type == "select"
    )
    database, approval, proposals = _workflow(tmp_path)
    database.save_entity_mapping(
        case_database,
        "CASE-001",
        "page-1",
        "CASE-001",
    )
    proposal, receipt = _approved(
        proposals,
        [
            _change(
                property_name,
                "before",
                "after",
                database_name=case_database,
            )
        ],
    )
    if resuming:
        applying = database.load_proposal(proposal.proposal_id)
        applying.status = ProposalStatus.APPLYING
        database.begin_apply(applying, receipt.nonce)
    gateway = InMemoryNotionGateway(
        lambda *_: True,
        initial_pages={
            "cases": [
                {
                    "id": "page-1",
                    "properties": {
                        definition.title_property: {
                            "title": [{"plain_text": "DRIFTED"}]
                        },
                        property_name: {"select": {"name": "before"}},
                    },
                }
            ]
        },
    )
    writer = ApprovedNotionWriter(
        gateway,
        database,
        database.load_proposal(proposal.proposal_id),
        {case_database: "cases"},
    )

    expected_error = ApprovalError if resuming else NotionSchemaError
    expected_message = (
        "global preflight"
        if resuming
        else "title no longer matches"
    )
    with pytest.raises(expected_error, match=expected_message):
        ApprovalGatedApplyService(
            database,
            approval,
            writer,
        ).apply(
            receipt,
            "synthetic-drive",
            "synthetic-item",
            "v2",
            "f" * 64,
        )

    assert (
        database.load_proposal(proposal.proposal_id).status
        is expected_status
    )
    operation_id = proposal.operations[0].change.operation_id
    outbox = database.operation_outbox_state(operation_id)
    assert (outbox is not None) is resuming
    if outbox is not None:
        assert outbox["status"] == "pending"
    assert database.nonce_used(receipt.nonce) is resuming
    assert (
        gateway.get_page("page-1")["properties"][property_name]["select"]["name"]
        == "before"
    )


class _PreflightSetupFailingWriter:
    def begin_global_preflight(self) -> None:
        raise NotionSchemaError("synthetic preflight setup failure")

    def inspect_batch(
        self,
        _operations: list[ProposalOperation],
    ) -> dict[str, WritePreconditionState]:
        pytest.fail("inspection must not run after setup failure")


@pytest.mark.parametrize(
    ("resuming", "expected_status", "expected_error"),
    [
        (False, ProposalStatus.STALE, NotionSchemaError),
        (True, ProposalStatus.FAILED, ApprovalError),
    ],
)
def test_preflight_setup_exception_closes_unstarted_or_resumable_scope(
    tmp_path: Path,
    resuming: bool,
    expected_status: ProposalStatus,
    expected_error: type[Exception],
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    proposal, receipt = _approved(
        proposals,
        [_change("현재상태", "착수", "출원")],
    )
    if resuming:
        applying = database.load_proposal(proposal.proposal_id)
        applying.status = ProposalStatus.APPLYING
        database.begin_apply(applying, receipt.nonce)

    with pytest.raises(expected_error):
        ApprovalGatedApplyService(
            database,
            approval,
            _PreflightSetupFailingWriter(),
        ).apply(
            receipt,
            "synthetic-drive",
            "synthetic-item",
            "v2",
            "f" * 64,
        )

    assert (
        database.load_proposal(proposal.proposal_id).status
        is expected_status
    )
    operation_id = proposal.operations[0].change.operation_id
    outbox = database.operation_outbox_state(operation_id)
    assert (outbox is not None) is resuming
    if outbox is not None:
        assert outbox["status"] == "pending"


class _CountingBatchWriter:
    def __init__(self) -> None:
        self.batch_calls: list[list[str]] = []

    def inspect_batch(
        self,
        operations: list[ProposalOperation],
    ) -> dict[str, WritePreconditionState]:
        return {
            item.change.operation_id: WritePreconditionState.PENDING
            for item in operations
        }

    def apply_batch(
        self,
        operations: list[ProposalOperation],
        _receipt: ApprovalReceipt,
    ) -> str:
        self.batch_calls.append(
            [item.change.operation_id for item in operations]
        )
        return "page-1"


def test_source_and_wiki_guards_run_once_per_page_mutation(
    tmp_path: Path,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    proposal, receipt = _approved(
        proposals,
        [
            _change("현재상태", "착수", "출원"),
            _change("내부상태", "대기", "진행"),
        ],
        knowledge_binding={"generation": "synthetic-wiki-v1"},
    )
    writer = _CountingBatchWriter()
    source_checks: list[str] = []
    wiki_checks: list[str] = []

    def source_guard() -> str:
        source_checks.append("checked")
        return "v2"

    def wiki_guard() -> dict[str, object]:
        wiki_checks.append("checked")
        return {"generation": "synthetic-wiki-v1"}

    report = ApprovalGatedApplyService(
        database,
        approval,
        writer,
    ).apply(
        receipt,
        "synthetic-drive",
        "synthetic-item",
        "v2",
        "f" * 64,
        source_version_guard=source_guard,
        knowledge_binding_guard=wiki_guard,
    )
    assert report.committed
    assert writer.batch_calls == [
        [item.change.operation_id for item in proposal.operations]
    ]
    assert len(source_checks) == 3
    assert len(wiki_checks) == 3


class _PrepareApplyFailingWriter(_CountingBatchWriter):
    def prepare_apply(
        self,
        _operations: list[ProposalOperation],
    ) -> None:
        raise NotionSchemaError("synthetic schema binding failure")


def test_started_source_guard_exception_becomes_recoverable_failure(
    tmp_path: Path,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    proposal, receipt = _approved(
        proposals,
        [_change("현재상태", "착수", "출원")],
    )
    applying = database.load_proposal(proposal.proposal_id)
    applying.status = ProposalStatus.APPLYING
    database.begin_apply(applying, receipt.nonce)
    writer = _CountingBatchWriter()

    def failing_source_guard() -> str:
        raise RuntimeError("synthetic private source failure")

    with pytest.raises(
        ApprovalError,
        match="source could not be verified",
    ):
        ApprovalGatedApplyService(
            database,
            approval,
            writer,
        ).apply(
            receipt,
            "synthetic-drive",
            "synthetic-item",
            "v2",
            "f" * 64,
            source_version_guard=failing_source_guard,
        )

    assert writer.batch_calls == []
    assert (
        database.load_proposal(proposal.proposal_id).status
        is ProposalStatus.FAILED
    )
    operation_id = proposal.operations[0].change.operation_id
    assert database.operation_outbox_state(operation_id)["status"] == "pending"


def test_started_prepare_apply_exception_becomes_recoverable_failure(
    tmp_path: Path,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    proposal, receipt = _approved(
        proposals,
        [_change("현재상태", "착수", "출원")],
    )
    applying = database.load_proposal(proposal.proposal_id)
    applying.status = ProposalStatus.APPLYING
    database.begin_apply(applying, receipt.nonce)
    writer = _PrepareApplyFailingWriter()

    with pytest.raises(
        ApprovalError,
        match="apply preparation could not be verified",
    ):
        ApprovalGatedApplyService(
            database,
            approval,
            writer,
        ).apply(
            receipt,
            "synthetic-drive",
            "synthetic-item",
            "v2",
            "f" * 64,
        )

    assert writer.batch_calls == []
    assert (
        database.load_proposal(proposal.proposal_id).status
        is ProposalStatus.FAILED
    )
    operation_id = proposal.operations[0].change.operation_id
    assert database.operation_outbox_state(operation_id)["status"] == "pending"


class _FailingBatchWriter(_CountingBatchWriter):
    def apply_batch(
        self,
        operations: list[ProposalOperation],
        _receipt: ApprovalReceipt,
    ) -> str:
        self.batch_calls.append(
            [item.change.operation_id for item in operations]
        )
        raise RuntimeError("synthetic acknowledgement loss")


def test_failed_status_is_durable_before_outbox_failure_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    proposal, receipt = _approved(
        proposals,
        [_change("현재상태", "착수", "출원")],
    )
    observed_statuses: list[ProposalStatus] = []

    def crash_before_outbox_transition(*_args, **_kwargs) -> None:
        observed_statuses.append(
            database.load_proposal(proposal.proposal_id).status
        )
        raise KeyboardInterrupt("synthetic process stop")

    monkeypatch.setattr(
        database,
        "mark_outbox_batch_failed",
        crash_before_outbox_transition,
    )

    with pytest.raises(KeyboardInterrupt, match="synthetic process stop"):
        ApprovalGatedApplyService(
            database,
            approval,
            _FailingBatchWriter(),
        ).apply(
            receipt,
            "synthetic-drive",
            "synthetic-item",
            "v2",
            "f" * 64,
        )

    assert observed_statuses == [ProposalStatus.FAILED]
    assert (
        database.load_proposal(proposal.proposal_id).status
        is ProposalStatus.FAILED
    )
    operation_id = proposal.operations[0].change.operation_id
    assert database.operation_outbox_state(operation_id)["status"] == "pending"
    assert database.nonce_used(receipt.nonce)


class _CrashBeforeLocalCommitWriter(ApprovedNotionWriter):
    def complete_batch(self, operations, page_id=None) -> None:
        del operations, page_id
        raise KeyboardInterrupt("synthetic process crash")


def test_remote_success_before_local_done_reconciles_on_resume(
    tmp_path: Path,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    database.save_entity_mapping(
        "한국 특허 사건",
        "CASE-001",
        "page-1",
        "CASE-001",
    )
    proposal, receipt = _approved(
        proposals,
        [_change("현재상태", "착수", "출원")],
    )
    pages = {
        "cases": [
            {
                "id": "page-1",
                "properties": {
                    "당소 사건번호": {
                        "title": [{"plain_text": "CASE-001"}]
                    },
                    "현재상태": {"select": {"name": "착수"}},
                },
            }
        ]
    }
    first_gateway = InMemoryNotionGateway(
        ExactNotionMutationVerifier(
            approval,
            database,
            {"한국 특허 사건": "cases"},
        ),
        initial_pages=pages,
    )
    first_writer = _CrashBeforeLocalCommitWriter(
        first_gateway,
        database,
        database.load_proposal(proposal.proposal_id),
        {"한국 특허 사건": "cases"},
    )
    with pytest.raises(KeyboardInterrupt):
        ApprovalGatedApplyService(
            database,
            approval,
            first_writer,
        ).apply(
            receipt,
            "synthetic-drive",
            "synthetic-item",
            "v2",
            "f" * 64,
        )
    assert (
        database.load_proposal(proposal.proposal_id).status
        is ProposalStatus.APPLYING
    )
    operation_id = proposal.operations[0].change.operation_id
    assert database.operation_outbox_state(operation_id)["status"] == "pending"

    resumed_gateway = InMemoryNotionGateway(
        ExactNotionMutationVerifier(
            approval,
            database,
            {"한국 특허 사건": "cases"},
        ),
        initial_pages={"cases": [first_gateway.get_page("page-1")]},
    )
    resumed_writer = ApprovedNotionWriter(
        resumed_gateway,
        database,
        database.load_proposal(proposal.proposal_id),
        {"한국 특허 사건": "cases"},
    )
    report = ApprovalGatedApplyService(
        database,
        approval,
        resumed_writer,
    ).apply(
        receipt,
        "synthetic-drive",
        "synthetic-item",
        "v2",
        "f" * 64,
    )
    assert report.committed
    assert report.already_applied == [operation_id]
    assert database.operation_outbox_state(operation_id)["status"] == "done"


def test_title_rename_crash_recovery_atomically_updates_mapping(
    tmp_path: Path,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    database.save_entity_mapping(
        "한국 특허 사건",
        "case:stable-key",
        "page-1",
        "CASE-OLD",
    )
    proposal, receipt = _approved(
        proposals,
        [
            _change(
                "당소 사건번호",
                "CASE-OLD",
                "CASE-NEW",
                entity_key="case:stable-key",
            )
        ],
    )
    pages = {
        "cases": [
            {
                "id": "page-1",
                "properties": {
                    "당소 사건번호": {
                        "title": [{"plain_text": "CASE-OLD"}]
                    }
                },
            }
        ]
    }
    first_gateway = InMemoryNotionGateway(
        ExactNotionMutationVerifier(
            approval,
            database,
            {"한국 특허 사건": "cases"},
        ),
        initial_pages=pages,
    )
    with pytest.raises(KeyboardInterrupt):
        ApprovalGatedApplyService(
            database,
            approval,
            _CrashBeforeLocalCommitWriter(
                first_gateway,
                database,
                database.load_proposal(proposal.proposal_id),
                {"한국 특허 사건": "cases"},
            ),
        ).apply(
            receipt,
            "synthetic-drive",
            "synthetic-item",
            "v2",
            "f" * 64,
        )
    assert database.get_entity_mapping(
        "한국 특허 사건",
        "case:stable-key",
    )["match_value"] == "CASE-OLD"

    resumed_gateway = InMemoryNotionGateway(
        ExactNotionMutationVerifier(
            approval,
            database,
            {"한국 특허 사건": "cases"},
        ),
        initial_pages={"cases": [first_gateway.get_page("page-1")]},
    )
    report = ApprovalGatedApplyService(
        database,
        approval,
        ApprovedNotionWriter(
            resumed_gateway,
            database,
            database.load_proposal(proposal.proposal_id),
            {"한국 특허 사건": "cases"},
        ),
    ).apply(
        receipt,
        "synthetic-drive",
        "synthetic-item",
        "v2",
        "f" * 64,
    )
    assert report.committed
    mapping = database.get_entity_mapping(
        "한국 특허 사건",
        "case:stable-key",
    )
    assert mapping == {
        "database_name": "한국 특허 사건",
        "entity_key": "case:stable-key",
        "notion_page_id": "page-1",
        "match_value": "CASE-NEW",
        "updated_at": mapping["updated_at"],
    }


def test_created_relation_target_is_available_to_later_page_batch(
    tmp_path: Path,
) -> None:
    data_sources = {
        "한국 특허 사건": "cases",
        "그룹": "groups",
    }
    database, approval, proposals = _workflow(
        tmp_path,
        data_sources=data_sources,
    )
    database.save_entity_mapping(
        "한국 특허 사건",
        "CASE-001",
        "case-page",
        "CASE-001",
    )
    group_title = _change(
        "그룹명",
        None,
        "지원사업 A",
        database_name="그룹",
        entity_key="group:support-a",
        kind=ChangeKind.CREATE,
    )
    case_relation = _change(
        "그룹",
        [],
        ["group:support-a"],
    )
    proposal, receipt = _approved(
        proposals,
        [group_title, case_relation],
    )
    gateway = InMemoryNotionGateway(
        ExactNotionMutationVerifier(
            approval,
            database,
            data_sources,
        ),
        initial_pages={
            "cases": [
                {
                    "id": "case-page",
                    "properties": {
                        "당소 사건번호": {
                            "title": [{"plain_text": "CASE-001"}]
                        },
                        "그룹": {"relation": []},
                    },
                }
            ]
        },
    )
    report = ApprovalGatedApplyService(
        database,
        approval,
        ApprovedNotionWriter(
            gateway,
            database,
            database.load_proposal(proposal.proposal_id),
            data_sources,
        ),
    ).apply(
        receipt,
        "synthetic-drive",
        "synthetic-item",
        "v2",
        "f" * 64,
    )
    assert report.committed
    group_page_id = database.get_entity_mapping(
        "그룹",
        "group:support-a",
    )["notion_page_id"]
    relation = gateway.get_page("case-page")["properties"]["그룹"]["relation"]
    assert relation == [{"id": group_page_id}]


class _DriftRelationTargetGateway(InMemoryNotionGateway):
    def __init__(self, *args, target_page_id: str, target_title: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_page_id = target_page_id
        self.target_title = target_title
        self.mutation_count = 0

    def upsert_page_batch(self, *args, **kwargs):
        result = super().upsert_page_batch(*args, **kwargs)
        self.mutation_count += 1
        if self.mutation_count == 1:
            with self._lock:
                self._pages[self.target_page_id]["properties"][
                    self.target_title
                ] = {"title": [{"plain_text": "DRIFTED"}]}
        return result


def test_relation_target_is_revalidated_for_every_page_mutation(
    tmp_path: Path,
) -> None:
    data_sources = {
        "한국 특허 사건": "cases",
        "그룹": "groups",
    }
    database, approval, proposals = _workflow(
        tmp_path,
        data_sources=data_sources,
    )
    database.save_entity_mapping(
        "그룹",
        "group:support-a",
        "group-page",
        "지원사업 A",
    )
    for entity_key, page_id, match_value in (
        ("case:a", "case-a-page", "CASE-A"),
        ("case:b", "case-b-page", "CASE-B"),
    ):
        database.save_entity_mapping(
            "한국 특허 사건",
            entity_key,
            page_id,
            match_value,
        )
    first_relation = _change(
        "그룹",
        [],
        ["group:support-a"],
        entity_key="case:a",
    )
    second_relation = _change(
        "그룹",
        [],
        ["group:support-a"],
        entity_key="case:b",
    )
    proposal, receipt = _approved(
        proposals,
        [first_relation, second_relation],
    )
    gateway = _DriftRelationTargetGateway(
        ExactNotionMutationVerifier(
            approval,
            database,
            data_sources,
        ),
        target_page_id="group-page",
        target_title="그룹명",
        initial_pages={
            "cases": [
                {
                    "id": "case-a-page",
                    "properties": {
                        "당소 사건번호": {
                            "title": [{"plain_text": "CASE-A"}]
                        },
                        "그룹": {"relation": []},
                    },
                },
                {
                    "id": "case-b-page",
                    "properties": {
                        "당소 사건번호": {
                            "title": [{"plain_text": "CASE-B"}]
                        },
                        "그룹": {"relation": []},
                    },
                },
            ],
            "groups": [
                {
                    "id": "group-page",
                    "properties": {
                        "그룹명": {
                            "title": [{"plain_text": "지원사업 A"}]
                        }
                    },
                }
            ],
        },
    )

    report = ApprovalGatedApplyService(
        database,
        approval,
        ApprovedNotionWriter(
            gateway,
            database,
            database.load_proposal(proposal.proposal_id),
            data_sources,
        ),
    ).apply(
        receipt,
        "synthetic-drive",
        "synthetic-item",
        "v2",
        "f" * 64,
    )

    assert not report.committed
    assert report.applied == [first_relation.operation_id]
    assert report.failed == {
        second_relation.operation_id: "notion_write_failed"
    }
    assert gateway.mutation_count == 1
    assert gateway.get_page("case-a-page")["properties"]["그룹"]["relation"] == [
        {"id": "group-page"}
    ]
    assert gateway.get_page("case-b-page")["properties"]["그룹"]["relation"] == []
    assert (
        database.load_proposal(proposal.proposal_id).status
        is ProposalStatus.FAILED
    )


def test_title_and_non_title_batches_rebind_only_after_exact_creation(
    tmp_path: Path,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    title = _change(
        "당소 사건번호",
        None,
        "CASE-NEW",
        entity_key="case:new",
        kind=ChangeKind.CREATE,
    )
    separator = _change(
        "당소 사건번호",
        None,
        "CASE-OTHER",
        entity_key="case:other",
        kind=ChangeKind.CREATE,
    )
    status = _change(
        "현재상태",
        None,
        "출원",
        entity_key="case:new",
        kind=ChangeKind.CREATE,
    )
    proposal, receipt = _approved(
        proposals,
        [title, separator, status],
    )
    gateway = InMemoryNotionGateway(
        ExactNotionMutationVerifier(
            approval,
            database,
            {"한국 특허 사건": "cases"},
        )
    )
    report = ApprovalGatedApplyService(
        database,
        approval,
        ApprovedNotionWriter(
            gateway,
            database,
            database.load_proposal(proposal.proposal_id),
            {"한국 특허 사건": "cases"},
        ),
    ).apply(
        receipt,
        "synthetic-drive",
        "synthetic-item",
        "v2",
        "f" * 64,
    )
    assert report.committed
    page = gateway.find_page(
        "cases",
        NotionMatch("당소 사건번호", "title"),
        "CASE-NEW",
    )
    assert page is not None
    assert page["properties"]["현재상태"]["select"]["name"] == "출원"


class _CountingSchemaGateway(InMemoryNotionGateway):
    def __init__(self, schema):
        super().__init__(lambda *_: True)
        self.schema = schema
        self.schema_gets = 0
        self.batch_mutations = 0

    def get_data_source(self, _data_source_id: str):
        self.schema_gets += 1
        return self.schema

    def upsert_page_batch(self, *args, **kwargs):
        self.batch_mutations += 1
        return super().upsert_page_batch(*args, **kwargs)


class _CountingExactVerifier(ExactNotionMutationVerifier):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.successes = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        verified = super().__call__(*args, **kwargs)
        if verified:
            self.successes += 1
        return verified


def test_large_proposal_mutations_scale_with_page_batches(
    tmp_path: Path,
) -> None:
    database, approval, proposals = _workflow(tmp_path)
    schema = {
        "properties": {
            "당소 사건번호": {"type": "title", "title": {}},
            "현재상태": {
                "type": "select",
                "select": {"options": [{"name": "출원"}]},
            },
            "내부상태": {
                "type": "select",
                "select": {"options": [{"name": "진행"}]},
            },
            "공식절차": {
                "type": "select",
                "select": {"options": [{"name": "국내출원"}]},
            },
            "비고": {"type": "rich_text", "rich_text": {}},
            "로컬ID": {"type": "rich_text", "rich_text": {}},
        }
    }
    gateway = _CountingSchemaGateway(schema)
    changes: list[ProposedChange] = []
    for index in range(295):
        changes.append(
            _change(
                "당소 사건번호",
                None,
                f"CASE-{index:03d}",
                entity_key=f"case:{index:03d}",
                kind=ChangeKind.CREATE,
            )
        )
    for index in range(295):
        entity_key = f"case:{index:03d}"
        changes.extend(
            [
                _change(
                    "현재상태",
                    None,
                    "출원",
                    entity_key=entity_key,
                    kind=ChangeKind.CREATE,
                ),
                _change(
                    "내부상태",
                    None,
                    "진행",
                    entity_key=entity_key,
                    kind=ChangeKind.CREATE,
                ),
                _change(
                    "공식절차",
                    None,
                    "국내출원",
                    entity_key=entity_key,
                    kind=ChangeKind.CREATE,
                ),
                _change(
                    "비고",
                    None,
                    f"합성 메모 {index}",
                    entity_key=entity_key,
                    kind=ChangeKind.CREATE,
                ),
            ]
        )
        if index < 5:
            changes.append(
                _change(
                    "로컬ID",
                    None,
                    f"synthetic-{index}",
                    entity_key=entity_key,
                    kind=ChangeKind.CREATE,
                )
            )
    assert len(changes) == 1480
    notion_binding = build_notion_binding(
        changes,
        {"한국 특허 사건": "cases"},
        schema_provider=gateway.get_data_source,
    )
    gateway.schema_gets = 0
    proposal = proposals.create(
        "v2",
        "f" * 64,
        "100",
        "synthetic-chat",
        changes,
        notion_binding=notion_binding,
    )
    proposal = proposals.request_approval(proposal.proposal_id)
    receipt = proposals.approve(
        proposal.proposal_id,
        proposal.revision,
        "100",
        "synthetic-chat",
        proposal.digest,
    )
    verifier = _CountingExactVerifier(
        approval,
        database,
        {"한국 특허 사건": "cases"},
    )
    gateway._approval_verifier = verifier
    report = ApprovalGatedApplyService(
        database,
        approval,
        ApprovedNotionWriter(
            gateway,
            database,
            database.load_proposal(proposal.proposal_id),
            {"한국 특허 사건": "cases"},
            require_exact_schema=True,
        ),
    ).apply(
        receipt,
        "synthetic-drive",
        "synthetic-item",
        "v2",
        "f" * 64,
    )
    assert report.committed, (
        next(iter(report.failed.items()), None),
        verifier.calls,
        verifier.successes,
        gateway.batch_mutations,
    )
    assert len(report.applied) == 1480
    assert verifier.calls == 1480
    assert verifier.successes == 1480
    assert gateway.batch_mutations == 590
    assert gateway.schema_gets == 1
