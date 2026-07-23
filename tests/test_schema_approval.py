from __future__ import annotations

import hashlib
import hmac
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from notion_excel_sync.models import (
    SCHEMA_APPROVAL_PURPOSE,
    SchemaApprovalReceipt,
    SchemaProposalRevision,
    SchemaProposalStatus,
    canonical_json,
    schema_approval_command_hash,
    sha256_json,
)
from notion_excel_sync.persistence.database import (
    SchemaEventClaimError,
    SchemaStateError,
    StateDatabase,
)
from notion_excel_sync.security.schema_approval import (
    SchemaApprovalError,
    SchemaApprovalService,
)


SECRET = "schema-specific-test-secret-at-least-24-characters"
TEST_PARENT_PAGE_ID = "f0000000000000000000000000000001"


class SchemaApprovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = StateDatabase(Path(self.temp.name) / "state.db")
        self.database.initialize()
        self.now = datetime.now(UTC)
        self.service = SchemaApprovalService(
            SECRET,
            {"123"},
            ttl_seconds=1800,
            nonce_used=self.database.schema_nonce_used,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def proposal(
        self,
        *,
        proposal_id: str = "SP-20260722-test",
        revision: int = 1,
        status: SchemaProposalStatus = SchemaProposalStatus.PENDING_APPROVAL,
        thread_id: str = "topic-7",
        payload_suffix: str = "",
    ) -> SchemaProposalRevision:
        return SchemaProposalRevision(
            schema_proposal_id=proposal_id,
            revision=revision,
            requested_by="123",
            chat_id="chat-1",
            thread_id=thread_id,
            operation_id="schema-op-1",
            logical_database_name="정부지원사업 증빙",
            parent_page_id=TEST_PARENT_PAGE_ID,
            schema_payload={
                "title": "정부지원사업 증빙" + payload_suffix,
                "properties": {
                    "증빙명": {"title": {}},
                    "인정 건수": {"number": {}},
                },
            },
            precondition={
                "parent_page_id": TEST_PARENT_PAGE_ID,
                "matching_databases": [],
            },
            status=status,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=20),
        )

    def issue(
        self,
        proposal: SchemaProposalRevision,
        *,
        message_id: str = "message-9",
        update_id: int = 9001,
    ) -> SchemaApprovalReceipt:
        return self.service.issue(
            proposal,
            telegram_user_id="123",
            chat_id="chat-1",
            thread_id=proposal.thread_id,
            telegram_message_id=message_id,
            telegram_update_id=update_id,
            confirmed_digest=proposal.digest,
            now=self.now,
        )

    def claim_approval_event(
        self,
        proposal: SchemaProposalRevision,
        *,
        message_id: str = "message-9",
        update_id: int = 9001,
    ) -> dict[str, object]:
        return self.database.claim_schema_gateway_event(
            command_name="nx_schema_approve",
            telegram_user_id=proposal.requested_by,
            chat_id=proposal.chat_id,
            thread_id=proposal.thread_id,
            message_id=message_id,
            update_id=update_id,
            command_hash=schema_approval_command_hash(
                proposal.schema_proposal_id,
                proposal.revision,
                proposal.digest,
            ),
            schema_proposal_id=proposal.schema_proposal_id,
        )

    def approved_receipt(
        self,
        proposal: SchemaProposalRevision,
        *,
        message_id: str = "message-9",
        update_id: int = 9001,
    ) -> SchemaApprovalReceipt:
        self.database.save_schema_proposal(proposal)
        self.claim_approval_event(
            proposal,
            message_id=message_id,
            update_id=update_id,
        )
        receipt = self.issue(
            proposal,
            message_id=message_id,
            update_id=update_id,
        )
        return self.database.commit_schema_approval(proposal, receipt)

    def test_purpose_thread_digest_and_hmac_are_domain_separated(self) -> None:
        proposal = self.proposal()
        other_thread = self.proposal(thread_id="")
        self.assertEqual(proposal.purpose, SCHEMA_APPROVAL_PURPOSE)
        self.assertNotEqual(proposal.digest, other_thread.digest)

        receipt = self.issue(proposal)
        self.assertEqual(receipt.purpose, SCHEMA_APPROVAL_PURPOSE)
        self.assertEqual(receipt.thread_id, "topic-7")
        unsigned = asdict(receipt)
        unsigned.pop("signature")
        non_domain_signature = hmac.new(
            SECRET.encode(),
            canonical_json(unsigned).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertNotEqual(receipt.signature, non_domain_signature)

    def test_issue_requires_exact_owner_chat_thread_digest_and_ttl(self) -> None:
        proposal = self.proposal()
        cases = (
            {"telegram_user_id": "999"},
            {"chat_id": "other-chat"},
            {"thread_id": "other-thread"},
            {"confirmed_digest": "0" * 64},
            {"telegram_message_id": ""},
            {"telegram_update_id": -1},
        )
        defaults: dict[str, object] = {
            "telegram_user_id": "123",
            "chat_id": "chat-1",
            "thread_id": "topic-7",
            "telegram_message_id": "message-9",
            "telegram_update_id": 9001,
            "confirmed_digest": proposal.digest,
            "now": self.now,
        }
        for override in cases:
            with self.subTest(override=override), self.assertRaises(
                SchemaApprovalError
            ):
                self.service.issue(proposal, **{**defaults, **override})  # type: ignore[arg-type]

        expired = replace(proposal, expires_at=self.now)
        with self.assertRaises(SchemaApprovalError):
            self.issue(expired)

    def test_verify_binds_full_event_and_nonce_state(self) -> None:
        pending = self.proposal()
        receipt = self.approved_receipt(pending)
        approved = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.service.verify(
            receipt,
            approved,
            telegram_user_id="123",
            chat_id="chat-1",
            thread_id="topic-7",
            telegram_message_id="message-9",
            telegram_update_id=9001,
            now=self.now,
        )
        with self.assertRaises(SchemaApprovalError):
            self.service.verify(
                receipt,
                approved,
                telegram_user_id="123",
                chat_id="chat-1",
                thread_id="",
                telegram_message_id="message-9",
                telegram_update_id=9001,
                now=self.now,
            )

        tampered = replace(receipt, telegram_update_id=9002)
        with self.assertRaises(SchemaApprovalError):
            self.service.verify(
                tampered,
                approved,
                telegram_user_id="123",
                chat_id="chat-1",
                thread_id="topic-7",
                telegram_message_id="message-9",
                telegram_update_id=9002,
                now=self.now,
            )

        self.database.begin_schema_apply(approved, receipt.nonce)
        applying = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.service.verify(
            receipt,
            applying,
            telegram_user_id="123",
            chat_id="chat-1",
            thread_id="topic-7",
            telegram_message_id="message-9",
            telegram_update_id=9001,
            now=self.now + timedelta(hours=1),
            require_used=True,
        )
        self.assertTrue(
            self.service.verify_operation_scope(
                receipt,
                applying,
                pending.operation_id,
            )
        )
        self.assertFalse(
            self.service.verify_operation_scope(receipt, applying, "other-operation")
        )

    def test_gateway_event_claim_is_durable_and_tamper_evident(self) -> None:
        command_hash = sha256_json({"command": "nx_schema_plan", "template": "evidence"})
        claimed = self.database.claim_schema_gateway_event(
            command_name="/nx_schema_plan",
            telegram_user_id="123",
            chat_id="chat-1",
            thread_id="",
            message_id="plan-message",
            update_id=7001,
            command_hash=command_hash,
        )
        self.assertEqual(claimed["status"], "pending")
        duplicate = self.database.claim_schema_gateway_event(
            command_name="nx_schema_plan",
            telegram_user_id="123",
            chat_id="chat-1",
            thread_id="",
            message_id="plan-message",
            update_id=7001,
            command_hash=command_hash,
        )
        self.assertEqual(duplicate["claimed_at"], claimed["claimed_at"])

        completed = self.database.complete_schema_gateway_event(
            command_name="nx_schema_plan",
            telegram_user_id="123",
            chat_id="chat-1",
            thread_id="",
            message_id="plan-message",
            update_id=7001,
            command_hash=command_hash,
            schema_proposal_id="SP-20260722-plan",
            safe_result="[NX_SCHEMA_PROPOSAL_READY] safe",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["safe_result"], "[NX_SCHEMA_PROPOSAL_READY] safe")
        redelivered = self.database.get_schema_gateway_event(
            command_name="nx_schema_plan",
            telegram_user_id="123",
            chat_id="chat-1",
            thread_id="",
            message_id="plan-message",
            update_id=7001,
            command_hash=command_hash,
        )
        assert redelivered is not None
        self.assertEqual(redelivered["schema_proposal_id"], "SP-20260722-plan")

        with self.assertRaises(SchemaEventClaimError):
            self.database.get_schema_gateway_event(
                command_name="nx_schema_plan",
                telegram_user_id="999",
                chat_id="chat-1",
                thread_id="",
                message_id="plan-message",
                update_id=7001,
                command_hash=command_hash,
            )

        with self.assertRaises(SchemaEventClaimError):
            self.database.claim_schema_gateway_event(
                command_name="nx_schema_plan",
                telegram_user_id="123",
                chat_id="chat-1",
                thread_id="",
                message_id="plan-message",
                update_id=7001,
                command_hash="f" * 64,
            )
        with self.assertRaises(SchemaEventClaimError):
            self.database.claim_schema_gateway_event(
                command_name="nx_schema_plan",
                telegram_user_id="123",
                chat_id="chat-1",
                thread_id="",
                message_id="changed-message",
                update_id=7001,
                command_hash=command_hash,
            )
        with self.assertRaises(SchemaEventClaimError):
            self.database.claim_schema_gateway_event(
                command_name="nx_schema_plan",
                telegram_user_id="123",
                chat_id="chat-1",
                thread_id="",
                message_id="plan-message",
                update_id=7002,
                command_hash=command_hash,
            )

    def test_approval_commit_requires_preclaim_and_redelivers_same_receipt(self) -> None:
        proposal = self.proposal()
        self.database.save_schema_proposal(proposal)
        receipt = self.issue(proposal)
        with self.assertRaises(SchemaEventClaimError):
            self.database.commit_schema_approval(proposal, receipt)
        self.assertEqual(
            self.database.load_schema_proposal(proposal.schema_proposal_id).status,
            SchemaProposalStatus.PENDING_APPROVAL,
        )

        self.claim_approval_event(proposal)
        stored = self.database.commit_schema_approval(proposal, receipt)
        replay = self.database.commit_schema_approval(proposal, replace(receipt, nonce="fake"))
        self.assertEqual(replay.nonce, stored.nonce)
        loaded = self.database.load_schema_receipt_for_event(
            schema_proposal_id=proposal.schema_proposal_id,
            revision=proposal.revision,
            proposal_digest=proposal.digest,
            telegram_user_id=proposal.requested_by,
            chat_id=proposal.chat_id,
            thread_id=proposal.thread_id,
            telegram_message_id=receipt.telegram_message_id,
            telegram_update_id=receipt.telegram_update_id,
        )
        assert loaded is not None
        self.assertEqual(loaded.signature, stored.signature)
        with self.assertRaises(SchemaEventClaimError):
            self.database.load_schema_receipt_for_event(
                schema_proposal_id=proposal.schema_proposal_id,
                revision=proposal.revision,
                proposal_digest=proposal.digest,
                telegram_user_id="999",
                chat_id=proposal.chat_id,
                thread_id=proposal.thread_id,
                telegram_message_id=receipt.telegram_message_id,
                telegram_update_id=receipt.telegram_update_id,
            )
        with self.assertRaises(SchemaEventClaimError):
            self.database.load_schema_receipt_for_event(
                schema_proposal_id=proposal.schema_proposal_id,
                revision=proposal.revision,
                proposal_digest="f" * 64,
                telegram_user_id=proposal.requested_by,
                chat_id=proposal.chat_id,
                thread_id=proposal.thread_id,
                telegram_message_id=receipt.telegram_message_id,
                telegram_update_id=receipt.telegram_update_id,
            )

    def test_schema_revisions_are_immutable_and_advance_one_at_a_time(self) -> None:
        draft = self.proposal(status=SchemaProposalStatus.DRAFT)
        self.database.save_schema_proposal(draft)
        pending = replace(
            draft,
            status=SchemaProposalStatus.PENDING_APPROVAL,
            expires_at=self.now + timedelta(minutes=20),
        )
        self.database.save_schema_proposal(pending)
        revision_two = self.proposal(
            revision=2,
            status=SchemaProposalStatus.DRAFT,
            payload_suffix=" 수정",
        )
        self.database.save_schema_proposal(revision_two)
        self.assertEqual(
            self.database.load_schema_proposal(draft.schema_proposal_id).revision,
            2,
        )
        self.assertEqual(
            self.database.load_active_schema_proposal(
                draft.logical_database_name
            ).schema_proposal_id,
            draft.schema_proposal_id,
        )
        with self.assertRaises(SchemaStateError):
            self.database.save_schema_proposal(
                replace(
                    draft,
                    schema_proposal_id="SP-20260722-competing",
                    operation_id="schema-op-competing",
                )
            )
        with self.assertRaises(SchemaStateError):
            self.database.save_schema_proposal(
                replace(revision_two, schema_payload={"different": True})
            )
        with self.assertRaises(SchemaStateError):
            self.database.save_schema_proposal(replace(revision_two, revision=4))

    def test_generic_save_cannot_bypass_privileged_schema_state_transitions(self) -> None:
        pending = self.proposal()
        self.database.save_schema_proposal(pending)
        for privileged_status in (
            SchemaProposalStatus.APPROVED,
            SchemaProposalStatus.APPLYING,
            SchemaProposalStatus.COMMITTED,
            SchemaProposalStatus.UNKNOWN,
        ):
            with self.subTest(status=privileged_status), self.assertRaises(
                SchemaStateError
            ):
                self.database.save_schema_proposal(
                    replace(pending, status=privileged_status)
                )
        with self.assertRaises(SchemaStateError):
            self.database.save_schema_proposal(
                replace(pending, expires_at=pending.expires_at + timedelta(hours=1))
            )
        with self.assertRaises(SchemaStateError):
            self.database.save_schema_proposal(
                self.proposal(
                    proposal_id="SP-20260722-privileged",
                    status=SchemaProposalStatus.APPROVED,
                )
            )

    def test_apply_journal_nonce_and_installation_commit_are_atomic(self) -> None:
        pending = self.proposal()
        receipt = self.approved_receipt(pending)
        approved = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.database.begin_schema_apply(approved, receipt.nonce)
        self.assertTrue(self.database.schema_nonce_used(receipt.nonce))
        state = self.database.schema_apply_journal_state(pending.operation_id)
        assert state is not None
        self.assertEqual(state["status"], "pending")
        self.database.mark_schema_apply_journal(pending.operation_id, "pending")
        with self.assertRaises(SchemaStateError):
            self.database.begin_schema_apply(approved, receipt.nonce)

        self.database.mark_schema_apply_journal(
            pending.operation_id,
            "done",
            remote_database_id="database-id",
            remote_data_source_id="data-source-id",
        )
        applying = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.database.finalize_schema_installation(
            applying,
            database_id="database-id",
            data_source_id="data-source-id",
        )
        installation = self.database.get_schema_installation(
            pending.logical_database_name
        )
        assert installation is not None
        self.assertEqual(installation["data_source_id"], "data-source-id")
        self.assertEqual(
            self.database.schema_installations(),
            {pending.logical_database_name: "data-source-id"},
        )
        self.assertEqual(
            self.database.load_schema_proposal(pending.schema_proposal_id).status,
            SchemaProposalStatus.COMMITTED,
        )
        with self.database.session() as connection:
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) AS count FROM sync_checkpoint"
            ).fetchone()["count"]
        self.assertEqual(checkpoint_count, 0)

    def test_unknown_remote_outcome_is_terminal_and_blocks_blind_retry(self) -> None:
        pending = self.proposal()
        receipt = self.approved_receipt(pending)
        approved = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.database.begin_schema_apply(approved, receipt.nonce)
        self.database.mark_schema_apply_journal(pending.operation_id, "pending")
        self.database.mark_schema_apply_journal(
            pending.operation_id,
            "unknown",
            error="transport outcome unknown",
        )
        state = self.database.schema_apply_journal_state(pending.operation_id)
        assert state is not None
        self.assertEqual(state["status"], "unknown")
        self.assertIsNotNone(state["attempt_started_at"])
        self.assertIsNotNone(state["attempt_finished_at"])
        unknown = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.assertEqual(unknown.status, SchemaProposalStatus.UNKNOWN)
        with self.assertRaises(SchemaStateError):
            self.database.mark_schema_apply_journal(pending.operation_id, "pending")
        with self.assertRaises(SchemaStateError):
            self.database.save_schema_proposal(replace(unknown, revision=2))
        self.database.mark_schema_apply_journal(
            pending.operation_id,
            "unknown",
            error="reconciled exact remote resource",
            remote_database_id="database-after-timeout",
            remote_data_source_id="data-source-after-timeout",
        )
        self.database.finalize_schema_installation(
            unknown,
            database_id="database-after-timeout",
            data_source_id="data-source-after-timeout",
        )
        self.assertEqual(
            self.database.load_schema_proposal(pending.schema_proposal_id).status,
            SchemaProposalStatus.COMMITTED,
        )

    def test_live_drift_revokes_only_an_approved_unused_schema_receipt(self) -> None:
        pending = self.proposal()
        self.approved_receipt(pending)
        approved = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.database.mark_schema_proposal_stale(
            approved,
            reason_code="live_binding_drift",
        )
        self.assertEqual(
            self.database.load_schema_proposal(pending.schema_proposal_id).status,
            SchemaProposalStatus.STALE,
        )

        other = replace(
            self.proposal(
                proposal_id="SP-20260722-started",
                payload_suffix="-started",
            ),
            operation_id="schema-op-started",
        )
        started_receipt = self.approved_receipt(
            other,
            message_id="message-started",
            update_id=9002,
        )
        started = self.database.load_schema_proposal(other.schema_proposal_id)
        self.database.begin_schema_apply(started, started_receipt.nonce)
        with self.assertRaises(SchemaStateError):
            self.database.mark_schema_proposal_stale(
                self.database.load_schema_proposal(other.schema_proposal_id),
                reason_code="too_late",
            )

    def test_apply_failure_requires_proved_no_remote_creation(self) -> None:
        pending = self.proposal()
        receipt = self.approved_receipt(pending)
        approved = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.database.begin_schema_apply(approved, receipt.nonce)
        applying = self.database.load_schema_proposal(pending.schema_proposal_id)
        self.database.fail_schema_apply(applying, reason_code="precondition_drift")
        self.assertEqual(
            self.database.load_schema_proposal(pending.schema_proposal_id).status,
            SchemaProposalStatus.FAILED,
        )

        attempted = replace(
            self.proposal(
                proposal_id="SP-20260722-attempted",
                payload_suffix="-attempted",
            ),
            operation_id="schema-op-attempted",
        )
        attempted_receipt = self.approved_receipt(
            attempted,
            message_id="message-attempted",
            update_id=9002,
        )
        attempted_approved = self.database.load_schema_proposal(
            attempted.schema_proposal_id
        )
        self.database.begin_schema_apply(attempted_approved, attempted_receipt.nonce)
        self.database.mark_schema_apply_journal(attempted.operation_id, "pending")
        attempted_journal = self.database.schema_apply_journal_state(
            attempted.operation_id
        )
        assert attempted_journal is not None
        self.assertIsNotNone(attempted_journal["attempt_started_at"])
        with self.assertRaises(SchemaStateError):
            self.database.mark_schema_apply_journal(
                attempted.operation_id,
                "pending",
            )
        attempted_applying = self.database.load_schema_proposal(
            attempted.schema_proposal_id
        )
        with self.assertRaises(SchemaStateError):
            self.database.fail_schema_apply(
                attempted_applying,
                reason_code="ambiguous_transport",
            )
        self.database.fail_schema_apply(
            attempted_applying,
            reason_code="notion_rejected_request",
            attempted_outcome_known_absent=True,
        )
        self.assertEqual(
            self.database.load_schema_proposal(attempted.schema_proposal_id).status,
            SchemaProposalStatus.FAILED,
        )

    def test_schema_lease_can_recover_after_a_terminal_holder_crash(self) -> None:
        proposal = self.proposal()
        self.database.save_schema_proposal(proposal)
        owner = self.database.acquire_apply_lease(
            drive_id=SCHEMA_APPROVAL_PURPOSE,
            item_id=proposal.parent_page_id,
            proposal_id=proposal.schema_proposal_id,
            revision=proposal.revision,
        )
        self.database.save_schema_proposal(
            replace(proposal, status=SchemaProposalStatus.REJECTED)
        )
        with patch(
            "notion_excel_sync.persistence.database._process_is_alive",
            return_value=False,
        ):
            replacement = self.database.acquire_apply_lease(
                drive_id=SCHEMA_APPROVAL_PURPOSE,
                item_id=proposal.parent_page_id,
                proposal_id="SP-replacement",
                revision=1,
            )
        self.assertNotEqual(owner, replacement)
        self.database.release_apply_lease(replacement)


if __name__ == "__main__":
    unittest.main()
