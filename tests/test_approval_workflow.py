from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from notion_excel_sync.adapters.notion import (
    ApprovalRequiredError,
    InMemoryNotionGateway,
    NotionMatch,
    NotionPrecondition,
)
from notion_excel_sync.models import (
    CORRECTION_OVERLAY_RECOVERY_MODE,
    ChangeKind,
    ProposalAction,
    ProposalPurpose,
    ProposalStatus,
    ProposedChange,
    SourceRef,
    WritePreconditionState,
)
from notion_excel_sync.persistence.database import ApplyLeaseError, StateDatabase
from notion_excel_sync.security.approval import ApprovalError, ApprovalService
from notion_excel_sync.workflow.apply import ApprovalGatedApplyService
from notion_excel_sync.workflow.notion_writer import (
    ApprovedNotionWriter,
    ExactNotionMutationVerifier,
    NOTION_DEFINITIONS,
    NotionSchemaError,
    build_notion_binding,
    encode_property,
)
from notion_excel_sync.workflow.proposals import ProposalError, ProposalService


class MemoryWriter:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], object] = {}
        self.preflight_ok = True
        self.calls = 0

    def preflight(self, operation) -> bool:
        return self.preflight_ok

    def apply(self, operation, receipt) -> str:
        self.calls += 1
        key = (
            operation.change.target_database,
            operation.change.entity_key,
            operation.change.property_name,
        )
        self.values[key] = operation.approved_value
        return operation.change.entity_key


class FailOnSecondWriter(MemoryWriter):
    def apply(self, operation, receipt) -> str:
        if self.calls == 1:
            self.calls += 1
            raise RuntimeError("simulated second write failure")
        return super().apply(operation, receipt)


class AlwaysFailWriter(MemoryWriter):
    def apply(self, operation, receipt) -> str:
        self.calls += 1
        raise RuntimeError("simulated write failure")


class SensitiveFailWriter(MemoryWriter):
    def apply(self, operation, receipt) -> str:
        self.calls += 1
        raise RuntimeError(
            r"C:\Users\private-user\AppData\Local\secret.txt "
            "gateway-write-token=do-not-persist"
        )


class InspectingWriter(MemoryWriter):
    def inspect(self, operation) -> WritePreconditionState:
        key = (
            operation.change.target_database,
            operation.change.entity_key,
            operation.change.property_name,
        )
        current = self.values.get(key, operation.change.current_value)
        if current == operation.approved_value:
            return WritePreconditionState.ALREADY_APPLIED
        if current == operation.change.current_value:
            return WritePreconditionState.PENDING
        return WritePreconditionState.CONFLICT


class MismatchedSchemaGateway(InMemoryNotionGateway):
    def get_data_source(self, data_source_id: str):
        del data_source_id
        return {
            "properties": {
                "당소 사건번호": {"type": "title", "title": {}},
                "현재상태": {"type": "number", "number": {}},
            }
        }


class FixedSchemaGateway(InMemoryNotionGateway):
    def __init__(self, schema):
        super().__init__(lambda *_: True)
        self.schema = schema

    def get_data_source(self, data_source_id: str):
        del data_source_id
        return self.schema


def sample_change() -> ProposedChange:
    return ProposedChange(
        target_database="한국 특허 사건",
        entity_key="SS-2026-001",
        property_name="현재상태",
        kind=ChangeKind.UPDATE,
        current_value="착수",
        proposed_value="출원",
        analyzer="status_analyzer",
        analyzer_version="1.0.0",
        confidence=1.0,
        reason="Excel status changed",
        source_refs=[
            SourceRef(
                drive_id="drive",
                item_id="item",
                version_id="2.0",
                file_hash="hash",
                sheet="2026",
                row=2,
                cells="H2",
                raw_value="출원",
            )
        ],
    )


class ApprovalWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = StateDatabase(Path(self.temp.name) / "state.db")
        self.database.initialize()
        self.approval = ApprovalService(
            secret="a-secure-test-secret-at-least-24-characters",
            allowed_users={"123"},
            nonce_used=self.database.nonce_used,
            mark_nonce_used=self.database.mark_nonce_used,
        )
        self.proposals = ProposalService(
            self.database,
            self.approval,
            notion_data_sources={
                "한국 특허 사건": "cases",
                "그룹": "groups",
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_pending(self):
        proposal = self.proposals.create("2.0", "hash", "123", "chat", [sample_change()])
        return self.proposals.request_approval(proposal.proposal_id)

    def create_expired_approved(self):
        proposal = self.create_pending()
        receipt = self.approval.issue(
            proposal,
            telegram_user_id="123",
            chat_id="chat",
            confirmed_digest=proposal.digest,
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        self.database.save_receipt(receipt)
        proposal.status = ProposalStatus.APPROVED
        self.database.save_proposal(proposal)
        return proposal, receipt

    def test_exact_approval_applies_and_advances_checkpoint(self) -> None:
        proposal = self.create_pending()
        receipt = self.proposals.approve(
            proposal.proposal_id, proposal.revision, "123", "chat", proposal.digest
        )
        writer = MemoryWriter()
        service = ApprovalGatedApplyService(self.database, self.approval, writer)
        report = service.apply(receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(report.committed)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(self.database.get_checkpoint("drive", "item")["version_id"], "2.0")

    def test_stale_mapping_never_retargets_write_to_title_match(self) -> None:
        self.database.save_entity_mapping(
            "한국 특허 사건",
            "SS-2026-001",
            "page-p",
            "SS-2026-001",
        )
        proposal = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [sample_change()],
        )
        gateway = InMemoryNotionGateway(
            lambda *_: True,
            initial_pages={
                "cases": [
                    {
                        "id": "page-p",
                        "properties": {
                            "당소 사건번호": {
                                "title": [{"plain_text": "DRIFTED"}]
                            },
                            "현재상태": {"select": {"name": "착수"}},
                        },
                    },
                    {
                        "id": "page-q",
                        "properties": {
                            "당소 사건번호": {
                                "title": [{"plain_text": "SS-2026-001"}]
                            },
                            "현재상태": {"select": {"name": "착수"}},
                        },
                    },
                ]
            },
        )
        writer = ApprovedNotionWriter(
            gateway,
            self.database,
            proposal,
            {"한국 특허 사건": "cases"},
        )

        with self.assertRaisesRegex(
            NotionSchemaError,
            "title no longer matches",
        ):
            writer.inspect(proposal.operations[0])

        page_q = gateway.get_page("page-q")
        self.assertEqual(
            page_q["properties"]["현재상태"]["select"]["name"],
            "착수",
        )

    def test_mapped_page_id_is_updated_directly_when_titles_are_duplicated(
        self,
    ) -> None:
        self.database.save_entity_mapping(
            "한국 특허 사건",
            "SS-2026-001",
            "page-p",
            "SS-2026-001",
        )
        proposal = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [sample_change()],
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        verifier = ExactNotionMutationVerifier(
            self.approval,
            self.database,
            {"한국 특허 사건": "cases"},
        )
        gateway = InMemoryNotionGateway(
            verifier,
            initial_pages={
                "cases": [
                    {
                        "id": page_id,
                        "properties": {
                            "당소 사건번호": {
                                "title": [{"plain_text": "SS-2026-001"}]
                            },
                            "현재상태": {"select": {"name": "착수"}},
                        },
                    }
                    for page_id in ("page-p", "page-q")
                ]
            },
        )
        writer = ApprovedNotionWriter(
            gateway,
            self.database,
            self.database.load_proposal(proposal.proposal_id),
            {"한국 특허 사건": "cases"},
        )

        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
        ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(report.committed)
        self.assertEqual(
            gateway.get_page("page-p")["properties"]["현재상태"]["select"][
                "name"
            ],
            "출원",
        )
        self.assertEqual(
            gateway.get_page("page-q")["properties"]["현재상태"]["select"][
                "name"
            ],
            "착수",
        )

    def test_independent_correction_never_advances_excel_checkpoint(self) -> None:
        proposal = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [sample_change()],
            purpose=ProposalPurpose.USER_CORRECTION,
        )
        loaded = self.database.load_proposal(proposal.proposal_id)
        self.assertIs(loaded.purpose, ProposalPurpose.USER_CORRECTION)
        self.assertIs(loaded.operations[0].action, ProposalAction.EDIT)
        self.assertTrue(loaded.operations[0].user_override)
        self.assertNotEqual(loaded.digest, "")
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )

        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            MemoryWriter(),
        ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(report.committed)
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))
        self.assertIs(
            self.database.load_proposal(proposal.proposal_id).status,
            ProposalStatus.COMMITTED,
        )

    def test_hidden_history_cannot_be_edited_or_sent_for_approval(self) -> None:
        main = sample_change()
        hidden_history = ProposedChange(
            target_database="변경이력",
            entity_key=f"history:{main.operation_id}",
            property_name="새값",
            kind=ChangeKind.CREATE,
            current_value=None,
            proposed_value="출원",
            analyzer="history_analyzer",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Synthetic generated history",
            source_refs=list(main.source_refs),
        )
        service = ProposalService(self.database, self.approval)
        proposal = service.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [main, hidden_history],
        )
        proposal = service.request_approval(proposal.proposal_id)

        with self.assertRaisesRegex(
            ProposalError,
            "history operations cannot be edited",
        ):
            service.edit(
                proposal.proposal_id,
                hidden_history.operation_id,
                ProposalAction.EXCLUDE,
            )

        orphan = ProposedChange(
            target_database="변경이력",
            entity_key="history:missing-operation",
            property_name="새값",
            kind=ChangeKind.CREATE,
            current_value=None,
            proposed_value="orphaned",
            analyzer="history_analyzer",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Synthetic orphan history",
            source_refs=[],
        )
        orphan_proposal = service.create(
            "2.0",
            "hash-2",
            "123",
            "chat",
            [orphan],
        )
        with self.assertRaisesRegex(
            ProposalError,
            "not displayed on any review card",
        ):
            service.request_approval(orphan_proposal.proposal_id)
        self.assertIs(
            self.database.load_proposal(orphan_proposal.proposal_id).status,
            ProposalStatus.DRAFT,
        )

    def test_user_edit_publishes_overlay_only_after_notion_write(self) -> None:
        proposal = self.create_pending()
        proposal = self.proposals.edit(
            proposal.proposal_id,
            proposal.operations[0].change.operation_id,
            ProposalAction.EDIT,
            "사용자 승인값",
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        writer = MemoryWriter()
        published: list[tuple[str, str]] = []

        def publish_overlay(revision, approved_receipt) -> None:
            self.assertEqual(writer.calls, 1)
            published.append(
                (revision.proposal_id, approved_receipt.proposal_digest)
            )

        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
            correction_overlay_publisher=publish_overlay,
        ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(report.committed)
        self.assertEqual(
            published,
            [(proposal.proposal_id, proposal.digest)],
        )

    def test_overlay_failure_recovers_without_replaying_notion_write(self) -> None:
        proposal = self.create_pending()
        operation_id = proposal.operations[0].change.operation_id
        proposal = self.proposals.edit(
            proposal.proposal_id,
            operation_id,
            ProposalAction.EDIT,
            "사용자 승인값",
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        writer = MemoryWriter()

        def fail_overlay(_revision, _approved_receipt) -> None:
            raise RuntimeError("simulated Wiki publication failure")

        failed = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
            correction_overlay_publisher=fail_overlay,
        ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertFalse(failed.committed)
        self.assertIn("__wiki_overlay__", failed.failed)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(
            self.database.operation_outbox_state(operation_id)["status"],
            "done",
        )
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))
        with self.assertRaises(ProposalError):
            self.proposals.reject(proposal.proposal_id, "123", "chat")

        recovery = self.proposals.create_recovery_revision(
            proposal.proposal_id
        )
        self.assertIs(recovery.operations[0].action, ProposalAction.EXCLUDE)
        recovery = self.proposals.request_approval(recovery.proposal_id)
        with self.assertRaisesRegex(
            ProposalError,
            "started apply must be recovered and finalized",
        ):
            self.proposals.reject(
                recovery.proposal_id,
                "123",
                "chat",
            )
        recovery_receipt = self.proposals.approve(
            recovery.proposal_id,
            recovery.revision,
            "123",
            "chat",
            recovery.digest,
        )
        recovered_events: list[str] = []

        recovered = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
            correction_overlay_publisher=lambda revision, _: (
                recovered_events.append(revision.proposal_id)
            ),
        ).apply(recovery_receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(recovered.committed)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(recovered_events, [proposal.proposal_id])

    def test_completed_correction_recovery_rebinds_changed_source_for_overlay_only(
        self,
    ) -> None:
        proposal = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [sample_change()],
            purpose=ProposalPurpose.USER_CORRECTION,
            knowledge_binding={"generation_digest": "old-wiki"},
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        writer = MemoryWriter()

        failed = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
            correction_overlay_publisher=lambda *_: (_ for _ in ()).throw(
                RuntimeError("simulated Wiki publication failure")
            ),
        ).apply(
            receipt,
            "drive",
            "item",
            "2.0",
            "hash",
            knowledge_binding_guard=lambda: {
                "generation_digest": "old-wiki"
            },
        )

        self.assertFalse(failed.committed)
        self.assertEqual(writer.calls, 1)
        failed_proposal = self.database.load_proposal(proposal.proposal_id)
        self.assertIs(failed_proposal.status, ProposalStatus.FAILED)
        self.assertTrue(
            self.proposals.correction_remote_writes_complete(
                failed_proposal
            )
        )

        recovery = self.proposals.create_recovery_revision(
            proposal.proposal_id,
            source_rebind=("3.0", "new-source-hash"),
        )
        self.assertEqual(recovery.source_version_id, "3.0")
        self.assertEqual(recovery.source_file_hash, "new-source-hash")
        self.assertEqual(recovery.knowledge_binding, {})
        self.assertEqual(recovery.operations[0].change.source_refs, [])
        self.assertEqual(recovery.operations[0].change.knowledge_refs, [])
        self.assertIs(
            recovery.operations[0].action,
            ProposalAction.EXCLUDE,
        )
        self.assertEqual(
            recovery.correction_binding["recovery"]["mode"],
            CORRECTION_OVERLAY_RECOVERY_MODE,
        )
        recovery = self.proposals.request_approval(recovery.proposal_id)
        with self.assertRaisesRegex(
            ProposalError,
            "overlay-only correction recovery cannot be revised",
        ):
            self.proposals.edit(
                recovery.proposal_id,
                recovery.operations[0].change.operation_id,
                ProposalAction.EDIT,
                "changed-again",
            )
        with self.assertRaisesRegex(
            ProposalError,
            "started apply must be recovered and finalized",
        ):
            self.proposals.reject(
                recovery.proposal_id,
                "123",
                "chat",
            )
        recovery_receipt = self.proposals.approve(
            recovery.proposal_id,
            recovery.revision,
            "123",
            "chat",
            recovery.digest,
        )
        published: list[str] = []

        recovered = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
            correction_overlay_publisher=lambda revision, _: (
                published.append(revision.proposal_id)
            ),
        ).apply(
            recovery_receipt,
            "drive",
            "item",
            "3.0",
            "new-source-hash",
        )

        self.assertTrue(recovered.committed)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(published, [proposal.proposal_id])
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))

    def test_started_apply_resumes_without_replaying_confirmed_remote_write(self) -> None:
        proposal = self.create_pending()
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        applying = self.database.load_proposal(proposal.proposal_id)
        applying.status = ProposalStatus.APPLYING
        self.database.begin_apply(applying, receipt.nonce)
        writer = InspectingWriter()
        operation = applying.operations[0]
        key = (
            operation.change.target_database,
            operation.change.entity_key,
            operation.change.property_name,
        )
        writer.values[key] = operation.approved_value

        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
        ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(report.committed)
        self.assertEqual(report.applied, [])
        self.assertEqual(report.already_applied, [operation.change.operation_id])
        self.assertEqual(writer.calls, 0)

    def test_expired_receipt_cannot_start_a_new_apply(self) -> None:
        proposal, receipt = self.create_expired_approved()
        writer = MemoryWriter()

        with self.assertRaisesRegex(ApprovalError, "expired"):
            ApprovalGatedApplyService(
                self.database,
                self.approval,
                writer,
            ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertEqual(writer.calls, 0)
        self.assertFalse(self.database.nonce_used(receipt.nonce))
        self.assertIs(
            self.database.load_proposal(proposal.proposal_id).status,
            ProposalStatus.APPROVED,
        )

    def test_expired_started_apply_reconciles_through_exact_verifier(self) -> None:
        proposal, receipt = self.create_expired_approved()
        applying = self.database.load_proposal(proposal.proposal_id)
        applying.status = ProposalStatus.APPLYING
        self.database.begin_apply(applying, receipt.nonce)
        operation = applying.operations[0]
        change = operation.change
        definition = NOTION_DEFINITIONS[change.target_database]
        data_source_ids = {change.target_database: "cases"}
        initial_properties = {
            definition.title_property: {
                "title": [{"plain_text": change.entity_key}]
            },
            change.property_name: encode_property(
                change.current_value,
                definition.properties[change.property_name],
            ),
        }
        verifier = ExactNotionMutationVerifier(
            self.approval,
            self.database,
            data_source_ids,
        )
        gateway = InMemoryNotionGateway(
            verifier,
            initial_pages={
                "cases": [{"id": "page-1", "properties": initial_properties}]
            },
        )
        writer = ApprovedNotionWriter(
            gateway,
            self.database,
            applying,
            data_source_ids,
        )

        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
        ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(report.committed)
        self.assertEqual(report.applied, [change.operation_id])
        self.assertIs(
            self.database.load_proposal(proposal.proposal_id).status,
            ProposalStatus.COMMITTED,
        )

    def test_begin_apply_rolls_back_all_local_state_on_failure(self) -> None:
        proposal = self.create_pending()
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        applying = self.database.load_proposal(proposal.proposal_id)
        applying.status = ProposalStatus.APPLYING
        with self.assertRaises(RuntimeError):
            self.database.begin_apply(applying, "missing-nonce")
        loaded = self.database.load_proposal(proposal.proposal_id)
        self.assertIs(loaded.status, ProposalStatus.APPROVED)
        self.assertFalse(self.database.nonce_used(receipt.nonce))
        self.assertIsNone(
            self.database.operation_outbox_state(
                proposal.operations[0].change.operation_id
            )
        )

    def test_source_change_invalidates_approval_without_write(self) -> None:
        proposal = self.create_pending()
        receipt = self.proposals.approve(
            proposal.proposal_id, proposal.revision, "123", "chat", proposal.digest
        )
        writer = MemoryWriter()
        service = ApprovalGatedApplyService(self.database, self.approval, writer)
        with self.assertRaises(ApprovalError):
            service.apply(receipt, "drive", "item", "3.0", "different")
        self.assertEqual(writer.calls, 0)
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))

    def test_source_apply_lease_blocks_a_distinct_approved_proposal(self) -> None:
        first = self.create_pending()
        token = self.database.acquire_apply_lease(
            drive_id="drive",
            item_id="item",
            proposal_id=first.proposal_id,
            revision=first.revision,
        )
        try:
            other_scope = sample_change()
            other_scope.property_name = "내부상태"
            second = self.proposals.create(
                "2.0", "hash", "123", "chat", [other_scope]
            )
            second = self.proposals.request_approval(second.proposal_id)
            receipt = self.proposals.approve(
                second.proposal_id,
                second.revision,
                "123",
                "chat",
                second.digest,
            )
            writer = MemoryWriter()
            with self.assertRaises(ApplyLeaseError):
                ApprovalGatedApplyService(
                    self.database,
                    self.approval,
                    writer,
                ).apply(receipt, "drive", "item", "2.0", "hash")
            self.assertEqual(writer.calls, 0)
        finally:
            self.database.release_apply_lease(token)

    def test_source_apply_lease_is_scoped_by_drive_and_item(self) -> None:
        proposal = self.create_pending()
        first = self.database.acquire_apply_lease(
            drive_id="drive-a",
            item_id="item",
            proposal_id=proposal.proposal_id,
            revision=proposal.revision,
        )
        second = self.database.acquire_apply_lease(
            drive_id="drive-b",
            item_id="item",
            proposal_id=proposal.proposal_id,
            revision=proposal.revision,
        )
        self.database.release_apply_lease(second)
        self.database.release_apply_lease(first)

    def test_crashed_lease_can_only_be_reclaimed_by_same_proposal(self) -> None:
        proposal = self.create_pending()
        self.database.acquire_apply_lease(
            drive_id="drive",
            item_id="item",
            proposal_id=proposal.proposal_id,
            revision=proposal.revision,
        )
        with self.database.session() as connection:
            connection.execute(
                "UPDATE source_apply_lease SET owner_pid = ?",
                (999999,),
            )

        other_scope = sample_change()
        other_scope.property_name = "내부상태"
        other = self.proposals.create(
            "2.0", "hash", "123", "chat", [other_scope]
        )
        other = self.proposals.request_approval(other.proposal_id)
        with self.assertRaises(ApplyLeaseError):
            self.database.acquire_apply_lease(
                drive_id="drive",
                item_id="item",
                proposal_id=other.proposal_id,
                revision=other.revision,
            )

        recovered = self.database.acquire_apply_lease(
            drive_id="drive",
            item_id="item",
            proposal_id=proposal.proposal_id,
            revision=proposal.revision,
        )
        self.database.release_apply_lease(recovered)

    def test_source_change_during_apply_stops_remaining_writes(self) -> None:
        first = sample_change()
        second = sample_change()
        second.property_name = "내부상태"
        proposal = self.proposals.create(
            "2.0", "hash", "123", "chat", [first, second]
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        checks = iter(["2.0", "2.0", "3.0"])
        writer = MemoryWriter()
        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
        ).apply(
            receipt,
            "drive",
            "item",
            "2.0",
            "hash",
            source_version_guard=lambda: next(checks),
        )
        self.assertFalse(report.committed)
        self.assertEqual(writer.calls, 1)
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))

    def test_notion_preflight_failure_prevents_all_writes(self) -> None:
        proposal = self.create_pending()
        receipt = self.proposals.approve(
            proposal.proposal_id, proposal.revision, "123", "chat", proposal.digest
        )
        writer = MemoryWriter()
        writer.preflight_ok = False
        service = ApprovalGatedApplyService(self.database, self.approval, writer)
        with self.assertRaises(ApprovalError):
            service.apply(receipt, "drive", "item", "2.0", "hash")
        self.assertEqual(writer.calls, 0)
        self.assertIs(
            self.database.load_proposal(proposal.proposal_id).status,
            ProposalStatus.STALE,
        )

    def test_resumed_preflight_conflict_is_failed_and_recoverable(self) -> None:
        first_change = sample_change()
        second_change = sample_change()
        second_change.property_name = "내부상태"
        second_change.current_value = "대기"
        second_change.proposed_value = "진행"
        proposal = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [first_change, second_change],
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        applying = self.database.load_proposal(proposal.proposal_id)
        applying.status = ProposalStatus.APPLYING
        self.database.begin_apply(applying, receipt.nonce)
        self.database.mark_outbox(first_change.operation_id, "done")

        writer = InspectingWriter()
        second_key = (
            second_change.target_database,
            second_change.entity_key,
            second_change.property_name,
        )
        writer.values[second_key] = "외부 충돌값"

        with self.assertRaisesRegex(ApprovalError, "Notion values changed"):
            ApprovalGatedApplyService(
                self.database,
                self.approval,
                writer,
            ).apply(receipt, "drive", "item", "2.0", "hash")

        failed = self.database.load_proposal(proposal.proposal_id)
        self.assertIs(failed.status, ProposalStatus.FAILED)
        with self.database.session() as connection:
            claims = connection.execute(
                "SELECT claim_state FROM mutation_scope_claim "
                "WHERE proposal_id = ? ORDER BY property_name",
                (proposal.proposal_id,),
            ).fetchall()
        self.assertEqual([row["claim_state"] for row in claims], ["recovery", "recovery"])

        recovery = self.proposals.create_recovery_revision(
            proposal.proposal_id
        )
        self.assertEqual(recovery.revision, proposal.revision + 1)
        self.assertIs(recovery.operations[0].action, ProposalAction.EXCLUDE)
        self.assertIs(recovery.operations[1].action, ProposalAction.APPLY)

    def test_partial_failure_requires_new_revision_and_can_recover(self) -> None:
        first_change = sample_change()
        second_change = sample_change()
        second_change.property_name = "내부상태"
        second_change.current_value = "대기"
        second_change.proposed_value = "진행"
        proposal = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [first_change, second_change],
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            FailOnSecondWriter(),
        ).apply(receipt, "drive", "item", "2.0", "hash")
        self.assertFalse(report.committed)
        self.assertEqual(report.applied, [first_change.operation_id])
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))
        with self.assertRaises(ApprovalError):
            ApprovalGatedApplyService(
                self.database,
                self.approval,
                MemoryWriter(),
            ).apply(receipt, "drive", "item", "2.0", "hash")

        recovery = self.proposals.create_recovery_revision(proposal.proposal_id)
        self.assertIs(recovery.operations[0].action, ProposalAction.EXCLUDE)
        self.assertIs(recovery.operations[1].action, ProposalAction.APPLY)
        recovery = self.proposals.request_approval(recovery.proposal_id)
        recovery_receipt = self.proposals.approve(
            recovery.proposal_id,
            recovery.revision,
            "123",
            "chat",
            recovery.digest,
        )
        recovered = ApprovalGatedApplyService(
            self.database,
            self.approval,
            MemoryWriter(),
        ).apply(recovery_receipt, "drive", "item", "2.0", "hash")
        self.assertTrue(recovered.committed)
        self.assertEqual(recovered.applied, [second_change.operation_id])

    def test_apply_failure_persists_only_stable_non_sensitive_codes(self) -> None:
        proposal = self.create_pending()
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )

        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            SensitiveFailWriter(),
        ).apply(receipt, "drive", "item", "2.0", "hash")

        operation_id = proposal.operations[0].change.operation_id
        self.assertEqual(
            report.failed,
            {operation_id: "notion_write_failed"},
        )
        outbox = self.database.operation_outbox_state(operation_id)
        assert outbox is not None
        self.assertEqual(outbox["last_error"], "notion_write_failed")
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT details FROM audit_log "
                "WHERE event_type = 'apply_failed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        details = row["details"]
        self.assertNotIn("private-user", details)
        self.assertNotIn("gateway-write-token", details)
        self.assertNotIn("RuntimeError", details)
        self.assertEqual(
            json.loads(details)["failure_codes"],
            {operation_id: "notion_write_failed"},
        )

    def test_edit_creates_new_revision_and_invalidates_old_digest(self) -> None:
        proposal = self.create_pending()
        operation_id = proposal.operations[0].change.operation_id
        revised = self.proposals.edit(
            proposal.proposal_id,
            operation_id,
            ProposalAction.EDIT,
            approved_value="의견통지",
        )
        pending = self.proposals.request_approval(revised.proposal_id)
        self.assertEqual(pending.revision, 2)
        self.assertNotEqual(pending.digest, proposal.digest)
        with self.assertRaises(ApprovalError):
            self.proposals.approve(
                pending.proposal_id, pending.revision, "123", "chat", proposal.digest
            )

    def test_historical_revision_cannot_be_approved(self) -> None:
        first = self.create_pending()
        revised = self.proposals.edit(
            first.proposal_id,
            first.operations[0].change.operation_id,
            ProposalAction.EDIT,
            approved_value="의견통지",
        )
        self.proposals.request_approval(revised.proposal_id)
        with self.assertRaises(ProposalError):
            self.proposals.approve(
                first.proposal_id,
                first.revision,
                "123",
                "chat",
                first.digest,
            )

    def test_json_null_is_a_valid_explicit_edit(self) -> None:
        first = self.create_pending()
        revised = self.proposals.edit(
            first.proposal_id,
            first.operations[0].change.operation_id,
            ProposalAction.EDIT,
            approved_value=None,
        )
        loaded = self.database.load_proposal(first.proposal_id, revised.revision)
        self.assertIsNone(loaded.operations[0].approved_value)
        self.assertIs(loaded.operations[0].action, ProposalAction.EDIT)

    def test_edit_value_must_match_declared_notion_property_type(self) -> None:
        change = sample_change()
        change.target_database = "비용·청구"
        change.entity_key = "cost:1"
        change.property_name = "공급가"
        change.proposed_value = 1000
        proposal = self.proposals.create(
            "2.0", "hash", "123", "chat", [change]
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        with self.assertRaises(ProposalError):
            self.proposals.edit(
                proposal.proposal_id,
                proposal.operations[0].change.operation_id,
                ProposalAction.EDIT,
                approved_value="one thousand",
            )

    def test_edit_cannot_clear_a_notion_title(self) -> None:
        change = sample_change()
        change.target_database = "비용·청구"
        change.entity_key = "cost:1"
        change.property_name = "항목명"
        change.proposed_value = "출원 비용"
        proposal = self.proposals.create(
            "2.0", "hash", "123", "chat", [change]
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        with self.assertRaises(ProposalError):
            self.proposals.edit(
                proposal.proposal_id,
                proposal.operations[0].change.operation_id,
                ProposalAction.EDIT,
                approved_value=None,
            )

    def test_deferred_operation_round_trips_and_is_resolved_later(self) -> None:
        first = self.create_pending()
        deferred = self.proposals.edit(
            first.proposal_id,
            first.operations[0].change.operation_id,
            ProposalAction.DEFER,
        )
        deferred = self.proposals.request_approval(deferred.proposal_id)
        receipt = self.proposals.approve(
            deferred.proposal_id,
            deferred.revision,
            "123",
            "chat",
            deferred.digest,
        )
        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            MemoryWriter(),
        ).apply(receipt, "drive", "item", "2.0", "hash")
        self.assertTrue(report.committed)
        pending = self.database.load_pending_operations()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].change.reason, "Excel status changed")
        self.assertTrue(pending[0].change.source_refs)

        failed_retry = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [pending[0].change],
        )
        failed_retry = self.proposals.request_approval(failed_retry.proposal_id)
        failed_receipt = self.proposals.approve(
            failed_retry.proposal_id,
            failed_retry.revision,
            "123",
            "chat",
            failed_retry.digest,
        )
        failed_report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            AlwaysFailWriter(),
        ).apply(failed_receipt, "drive", "item", "2.0", "hash")
        self.assertFalse(failed_report.committed)
        self.assertEqual(len(self.database.load_pending_operations()), 1)

        retry = self.proposals.create_recovery_revision(
            failed_retry.proposal_id
        )
        retry = self.proposals.request_approval(retry.proposal_id)
        retry_receipt = self.proposals.approve(
            retry.proposal_id,
            retry.revision,
            "123",
            "chat",
            retry.digest,
        )
        ApprovalGatedApplyService(
            self.database,
            self.approval,
            MemoryWriter(),
        ).apply(retry_receipt, "drive", "item", "2.0", "hash")
        self.assertEqual(self.database.load_pending_operations(), [])

    def test_exact_mutation_scope_blocks_retarget_value_change_and_replay(self) -> None:
        pending = self.create_pending()
        receipt = self.proposals.approve(
            pending.proposal_id,
            pending.revision,
            "123",
            "chat",
            pending.digest,
        )
        applying = self.database.load_proposal(pending.proposal_id)
        applying.status = ProposalStatus.APPLYING
        self.database.begin_apply(applying, receipt.nonce)
        verifier = ExactNotionMutationVerifier(
            self.approval,
            self.database,
            {"한국 특허 사건": "cases"},
        )
        gateway = InMemoryNotionGateway(
            verifier,
            initial_pages={
                "cases": [
                    {
                        "id": "page-1",
                        "properties": {
                            "당소 사건번호": {
                                "title": [{"plain_text": "SS-2026-001"}]
                            },
                            "현재상태": {"select": {"name": "착수"}},
                        },
                    }
                ]
            },
        )
        operation_id = applying.operations[0].change.operation_id
        match = NotionMatch("당소 사건번호", "title")
        precondition = NotionPrecondition(
            page_id="page-1",
            last_edited_time=None,
            property_name="현재상태",
            property_value={"select": {"name": "착수"}},
        )
        with self.assertRaises(ApprovalRequiredError):
            gateway.upsert_page(
                "other-data-source",
                match,
                "SS-2026-001",
                {"현재상태": {"select": {"name": "출원"}}},
                operation_id=operation_id,
                approval=receipt,
                precondition=precondition,
            )
        with self.assertRaises(ApprovalRequiredError):
            gateway.upsert_page(
                "cases",
                match,
                "SS-2026-001",
                {"현재상태": {"select": {"name": "임의값"}}},
                operation_id=operation_id,
                approval=receipt,
                precondition=precondition,
            )
        gateway.upsert_page(
            "cases",
            match,
            "SS-2026-001",
            {"현재상태": {"select": {"name": "출원"}}},
            operation_id=operation_id,
            approval=receipt,
            precondition=precondition,
        )
        self.database.mark_outbox(operation_id, "done")
        with self.assertRaises(ApprovalRequiredError):
            gateway.upsert_page(
                "cases",
                match,
                "SS-2026-001",
                {"현재상태": {"select": {"name": "출원"}}},
                operation_id=operation_id,
                approval=receipt,
                precondition=precondition,
            )

    def test_full_exact_gateway_flow_creates_only_approved_page_fields(self) -> None:
        title = sample_change()
        title.entity_key = "case:new"
        title.property_name = "당소 사건번호"
        title.current_value = None
        title.proposed_value = "SS-NEW"
        title.kind = ChangeKind.CREATE
        status = sample_change()
        status.entity_key = "case:new"
        status.current_value = None
        status.kind = ChangeKind.CREATE
        proposal = self.proposals.create(
            "2.0", "hash", "123", "chat", [title, status]
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        verifier = ExactNotionMutationVerifier(
            self.approval,
            self.database,
            {"한국 특허 사건": "cases"},
        )
        gateway = InMemoryNotionGateway(verifier)
        writer = ApprovedNotionWriter(
            gateway,
            self.database,
            self.database.load_proposal(proposal.proposal_id),
            {"한국 특허 사건": "cases"},
        )
        report = ApprovalGatedApplyService(
            self.database,
            self.approval,
            writer,
        ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertTrue(report.committed)
        page = gateway.find_page(
            "cases",
            NotionMatch("당소 사건번호", "title"),
            "SS-NEW",
        )
        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page["properties"]["현재상태"]["select"]["name"], "출원")

    def test_excluded_title_cannot_be_injected_by_another_field(self) -> None:
        title = sample_change()
        title.entity_key = "case:new"
        title.property_name = "당소 사건번호"
        title.current_value = None
        title.proposed_value = "SS-NEW"
        title.kind = ChangeKind.CREATE
        status = sample_change()
        status.entity_key = "case:new"
        status.current_value = None
        status.kind = ChangeKind.CREATE
        proposal = self.proposals.create(
            "2.0", "hash", "123", "chat", [title, status]
        )
        proposal = self.proposals.edit(
            proposal.proposal_id,
            title.operation_id,
            ProposalAction.EXCLUDE,
        )
        writer = ApprovedNotionWriter(
            InMemoryNotionGateway(lambda *_: True),
            self.database,
            proposal,
            {"한국 특허 사건": "cases"},
        )
        with self.assertRaises(NotionSchemaError):
            writer.preflight(proposal.operations[1])

    def test_remote_notion_schema_mismatch_blocks_before_write(self) -> None:
        proposal = self.proposals.create(
            "2.0", "hash", "123", "chat", [sample_change()]
        )
        writer = ApprovedNotionWriter(
            MismatchedSchemaGateway(lambda *_: True),
            self.database,
            proposal,
            {"한국 특허 사건": "cases"},
        )
        with self.assertRaises(NotionSchemaError):
            writer.inspect(proposal.operations[0])

    def test_data_source_mapping_change_after_approval_is_rejected(self) -> None:
        proposal = self.create_pending()
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        writer = ApprovedNotionWriter(
            InMemoryNotionGateway(lambda *_: True),
            self.database,
            proposal,
            {
                "한국 특허 사건": "retargeted-cases",
                "그룹": "groups",
            },
        )

        with self.assertRaises(NotionSchemaError):
            ApprovalGatedApplyService(
                self.database,
                self.approval,
                writer,
            ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertFalse(self.database.nonce_used(receipt.nonce))
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))

    def test_relation_schema_target_change_after_approval_is_rejected(self) -> None:
        relation = sample_change()
        relation.property_name = "그룹"
        relation.current_value = []
        relation.proposed_value = ["group:1"]
        data_sources = {
            "한국 특허 사건": "cases",
            "그룹": "groups",
        }
        approved_schema = {
            "properties": {
                "당소 사건번호": {
                    "id": "title",
                    "type": "title",
                    "title": {},
                },
                "그룹": {
                    "id": "relation",
                    "type": "relation",
                    "relation": {"data_source_id": "groups"},
                },
            }
        }
        binding = build_notion_binding(
            [relation],
            data_sources,
            schema_provider=lambda _: approved_schema,
        )
        proposal = self.proposals.create(
            "2.0",
            "hash",
            "123",
            "chat",
            [relation],
            notion_binding=binding,
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        receipt = self.proposals.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        changed_schema = {
            "properties": {
                **approved_schema["properties"],
                "그룹": {
                    "id": "relation",
                    "type": "relation",
                    "relation": {"data_source_id": "different-groups"},
                },
            }
        }
        writer = ApprovedNotionWriter(
            FixedSchemaGateway(changed_schema),
            self.database,
            proposal,
            data_sources,
            require_exact_schema=True,
        )

        with self.assertRaises(NotionSchemaError):
            ApprovalGatedApplyService(
                self.database,
                self.approval,
                writer,
            ).apply(receipt, "drive", "item", "2.0", "hash")

        self.assertFalse(self.database.nonce_used(receipt.nonce))
        self.assertIsNone(self.database.get_checkpoint("drive", "item"))

    def test_wrong_telegram_user_cannot_approve(self) -> None:
        proposal = self.create_pending()
        with self.assertRaises(ApprovalError):
            self.proposals.approve(
                proposal.proposal_id, proposal.revision, "999", "chat", proposal.digest
            )


if __name__ == "__main__":
    unittest.main()
