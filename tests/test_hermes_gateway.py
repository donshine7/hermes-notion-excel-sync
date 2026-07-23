from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from notion_excel_sync.config import load_config
from notion_excel_sync.knowledge.provider import AnalyzerKnowledgeProvider
from notion_excel_sync.models import (
    ApprovalReceipt,
    ChangeKind,
    ProposalAction,
    ProposalPurpose,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    SourceRef,
    WritePreconditionState,
)
from notion_excel_sync.persistence.database import ApplyLeaseError, StateDatabase
from notion_excel_sync.security.approval import ApprovalService
from notion_excel_sync.security.hermes_gateway import (
    TelegramEventIdentity,
    _apply_gateway_receipt,
    _prepare_authenticated_sync,
    _resolve_config_path,
    handle_pre_gateway_dispatch,
)
from notion_excel_sync.security.receipt_io import (
    ReceiptReferenceError,
    load_receipt,
    resolve_receipt_reference,
)
from notion_excel_sync.workflow.apply import ApplyReport
from notion_excel_sync.workflow.notion_writer import NOTION_DEFINITIONS
from notion_excel_sync.workflow.proposals import ProposalService
from notion_excel_sync.workflow.sync import PreparationResult


SECRET = "a-secure-test-secret-at-least-24-characters"
SECRET_ENV = "GATEWAY_RELAY_NX_APPROVAL_SECRET"


@dataclass
class FakeSource:
    platform: str = "telegram"
    user_id: str = "123"
    chat_id: str = "chat"
    message_id: str = "42"
    chat_type: str = "private"
    thread_id: str = ""
    is_bot: bool = False


@dataclass
class FakeEvent:
    text: str
    source: FakeSource
    platform_update_id: int | None = 314
    timestamp: datetime = datetime(2026, 7, 16, 1, 2, 3, tzinfo=UTC)


class FakeGateway:
    def __init__(self, authorized: bool = True) -> None:
        self.authorized = authorized
        self.sources: list[FakeSource] = []

    def _is_user_authorized(self, source: FakeSource) -> bool:
        self.sources.append(source)
        return self.authorized


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, content: str, **_: object):
        self.sent.append((chat_id, content))
        return type("SendResult", (), {"success": True})()


class HermesGatewayApprovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp.name) / "project"
        config_dir = self.project_root / "config"
        config_dir.mkdir(parents=True)
        self.config_path = config_dir / "sync.local.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "onedrive": {"drive_id": "drive", "item_id": "item"},
                    "excel": {"sheets": ["Cases"]},
                    "notion": {
                        "read_token_env": "NOTION_READ_TOKEN",
                        "write_token_env": "GATEWAY_RELAY_NX_NOTION_TOKEN",
                        "databases": {"cases": "notion-data-source"},
                    },
                    "approval": {
                        "secret_env": SECRET_ENV,
                        "ttl_seconds": 1800,
                        "allowed_telegram_users": ["123"],
                    },
                    "state_db": "../data/state.db",
                    "work_dir": "../data/snapshots",
                    "initial_cutoff": "2026-07-15T00:00:00+09:00",
                }
            ),
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ,
            {
                SECRET_ENV: SECRET,
                "NOTION_EXCEL_SYNC_PROJECT_ROOT": str(self.project_root),
                "NOTION_EXCEL_SYNC_DEV_PLUGIN": "1",
                "ONEDRIVE_ACCESS_TOKEN": "read-only-test-token",
            },
        )
        self.environment.start()
        os.environ.pop("NOTION_EXCEL_SYNC_CONFIG", None)

        self.database = StateDatabase(self.project_root / "data" / "state.db")
        self.database.initialize()
        self.proposal = ProposalRevision(
            proposal_id="P-1",
            revision=1,
            source_version_id="source-v1",
            source_file_hash="source-hash",
            requested_by="123",
            chat_id="chat",
            operations=[],
            status=ProposalStatus.PENDING_APPROVAL,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        self.database.save_proposal(self.proposal)
        self.apply_patch = patch(
            "notion_excel_sync.security.hermes_gateway._apply_gateway_receipt",
            return_value=ApplyReport(
                proposal_id="P-1",
                applied=[],
                already_applied=[],
                excluded=[],
                deferred=[],
                failed={"__test__": "remote apply intentionally skipped"},
                committed=False,
            ),
        )
        self.apply_mock = self.apply_patch.start()

    def tearDown(self) -> None:
        self.apply_patch.stop()
        self.environment.stop()
        self.temp.cleanup()

    def event(
        self,
        *,
        user_id: str = "123",
        chat_id: str = "chat",
        revision: int = 1,
        digest: str | None = None,
    ) -> FakeEvent:
        digest = digest or self.proposal.digest
        return FakeEvent(
            text=f"/nx_approve P-1 {revision} {digest}",
            source=FakeSource(user_id=user_id, chat_id=chat_id),
        )

    def handle(self, event: FakeEvent, gateway: FakeGateway | None = None) -> str:
        result = handle_pre_gateway_dispatch(
            event=event,
            gateway=gateway or FakeGateway(),
            project_root=self.project_root,
        )
        self.assertIsInstance(result, str)
        return str(result)

    def marker_payload(self, marker: str) -> dict[str, object]:
        self.assertTrue(marker.startswith("[NX_SYNC_"))
        payload_start = marker.index("] ") + 2
        payload, _ = json.JSONDecoder().raw_decode(marker[payload_start:])
        self.assertIsInstance(payload, dict)
        return payload

    def receipt_count(self) -> int:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM approval_receipt"
            ).fetchone()
        assert row is not None
        return int(row["count"])

    def receipt_path(self, marker_payload: dict[str, object]) -> Path:
        reference = marker_payload["receipt_ref"]
        self.assertIsInstance(reference, str)
        return resolve_receipt_reference(
            self.project_root / "data" / "receipts",
            str(reference),
        )

    def editable_proposal(self, *, status: ProposalStatus | None = None):
        database_name, definition = next(iter(NOTION_DEFINITIONS.items()))
        change = ProposedChange(
            target_database=database_name,
            entity_key="CASE-1",
            property_name=definition.title_property,
            kind=ChangeKind.UPDATE,
            current_value="old",
            proposed_value="new",
            analyzer="workflow_status",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Gateway command contract",
            source_refs=[
                SourceRef(
                    "drive",
                    "item",
                    "source-v1",
                    "source-hash",
                    "Cases",
                    2,
                    "A2",
                    "new",
                )
            ],
        )
        service = ProposalService(self.database, ttl_seconds=1800)
        proposal = service.create(
            "source-v1",
            "source-hash",
            "123",
            "chat",
            [change],
        )
        proposal = service.request_approval(proposal.proposal_id)
        if status is not None:
            proposal.status = status
            self.database.save_proposal(proposal)
        return proposal, change

    def test_exact_authenticated_command_issues_receipt_and_audits_update(self) -> None:
        gateway = FakeGateway()
        marker = self.handle(self.event(), gateway)

        self.assertIn("[NX_SYNC_FAILED", marker)
        self.assertEqual(gateway.sources, [self.event().source])
        approved = self.database.load_proposal("P-1")
        self.assertIs(approved.status, ProposalStatus.APPROVED)
        self.assertEqual(self.receipt_count(), 1)

        receipt_files = list((self.project_root / "data" / "receipts").glob("*.json"))
        self.assertEqual(len(receipt_files), 1)
        marker_payload = self.marker_payload(marker)
        self.assertEqual(self.receipt_path(marker_payload), receipt_files[0])
        self.assertNotIn("receipt_file", marker_payload)
        self.assertNotIn(str(self.project_root), marker)
        self.assertEqual(
            marker_payload["failure_codes"],
            {"__test__": "operation_failed"},
        )
        self.assertNotIn("remote apply intentionally skipped", marker)
        self.assertFalse(marker_payload["redelivered"])
        receipt = load_receipt(receipt_files[0])
        self.assertEqual(receipt.proposal_id, "P-1")
        self.assertEqual(receipt.proposal_digest, self.proposal.digest)

        with self.database.session() as connection:
            audit = connection.execute(
                "SELECT actor, details FROM audit_log "
                "WHERE event_type = 'proposal_approved'"
            ).fetchone()
        self.assertIsNotNone(audit)
        assert audit is not None
        details = json.loads(audit["details"])
        self.assertEqual(audit["actor"], "123")
        self.assertEqual(details["telegram_message_id"], "42")
        self.assertEqual(details["telegram_update_id"], 314)
        self.assertEqual(details["receipt_ref"], marker_payload["receipt_ref"])
        self.assertNotIn("receipt_file", details)
        self.assertNotIn(str(self.project_root), audit["details"])
        self.apply_mock.assert_called_once()

    def test_lost_success_marker_redelivers_same_receipt_without_reissuing(self) -> None:
        first_marker = self.handle(self.event())
        first_payload = self.marker_payload(first_marker)
        first_receipt = load_receipt(self.receipt_path(first_payload))
        replay = self.event()
        replay.source.message_id = "43"
        replay.platform_update_id = 315

        replay_marker = self.handle(replay)

        replay_payload = self.marker_payload(replay_marker)
        replay_receipt = load_receipt(self.receipt_path(replay_payload))
        self.assertTrue(replay_payload["redelivered"])
        self.assertEqual(replay_payload["receipt_ref"], first_payload["receipt_ref"])
        self.assertEqual(replay_receipt.nonce, first_receipt.nonce)
        self.assertEqual(replay_receipt.signature, first_receipt.signature)
        self.assertEqual(self.receipt_count(), 1)
        with self.database.session() as connection:
            approved_count = connection.execute(
                "SELECT COUNT(*) AS count FROM audit_log "
                "WHERE event_type = 'proposal_approved'"
            ).fetchone()["count"]
            redelivered = connection.execute(
                "SELECT details FROM audit_log "
                "WHERE event_type = 'approval_receipt_redelivered'"
            ).fetchone()
        self.assertEqual(approved_count, 1)
        self.assertIsNotNone(redelivered)
        assert redelivered is not None
        redelivery_details = json.loads(redelivered["details"])
        self.assertEqual(redelivery_details["telegram_update_id"], 315)
        self.assertEqual(
            redelivery_details["receipt_ref"],
            first_payload["receipt_ref"],
        )
        self.assertNotIn("receipt_file", redelivery_details)
        self.assertNotIn(str(self.project_root), redelivered["details"])

    def test_expired_receipt_redelivers_only_after_apply_has_started(self) -> None:
        approval = ApprovalService(
            SECRET,
            {"123"},
            nonce_used=self.database.nonce_used,
            mark_nonce_used=self.database.mark_nonce_used,
        )
        receipt = approval.issue(
            self.proposal,
            telegram_user_id="123",
            chat_id="chat",
            confirmed_digest=self.proposal.digest,
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        self.database.save_receipt(receipt)
        approved = self.database.load_proposal("P-1")
        approved.status = ProposalStatus.APPROVED
        self.database.save_proposal(approved)
        applying = self.database.load_proposal("P-1")
        applying.status = ProposalStatus.APPLYING
        self.database.begin_apply(applying, receipt.nonce)

        marker = self.handle(self.event())

        payload = self.marker_payload(marker)
        redelivered = load_receipt(self.receipt_path(payload))
        self.assertTrue(payload["redelivered"])
        self.assertEqual(redelivered.nonce, receipt.nonce)
        self.assertLess(redelivered.expires_at, datetime.now(UTC))
        self.assertEqual(self.receipt_count(), 1)
        self.apply_mock.assert_called_once()

    def test_fresh_command_reissues_expired_unstarted_approved_receipt(self) -> None:
        approval = ApprovalService(
            SECRET,
            {"123"},
            nonce_used=self.database.nonce_used,
            mark_nonce_used=self.database.mark_nonce_used,
        )
        old = approval.issue(
            self.proposal,
            telegram_user_id="123",
            chat_id="chat",
            confirmed_digest=self.proposal.digest,
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        self.database.save_receipt(old)
        approved = self.database.load_proposal("P-1")
        approved.status = ProposalStatus.APPROVED
        self.database.save_proposal(approved)
        self.apply_mock.side_effect = ApplyLeaseError("source lease is busy")

        marker = self.handle(self.event())

        self.assertTrue(marker.startswith("[NX_SYNC_ERROR]"))
        replacement = self.database.load_latest_receipt("P-1", 1)
        self.assertNotEqual(replacement.nonce, old.nonce)
        self.assertGreater(replacement.expires_at, datetime.now(UTC))
        self.assertTrue(self.database.nonce_used(old.nonce))
        self.assertFalse(self.database.nonce_used(replacement.nonce))
        self.assertEqual(self.receipt_count(), 2)
        self.assertIs(
            self.database.load_proposal("P-1").status,
            ProposalStatus.APPROVED,
        )

    def test_sync_noop_checkpoint_requires_authenticated_telegram_event(self) -> None:
        def commit_noop(config, database, identity):
            database.commit_noop_checkpoint(
                drive_id=config.onedrive.drive_id,
                item_id=config.onedrive.item_id,
                version_id="v-noop",
                file_hash="hash-noop",
                actor=identity.user_id,
            )
            return PreparationResult("v-noop", "v-noop", 0, None, checkpoint_advanced=True)

        event = FakeEvent(text="/nx_sync", source=FakeSource())
        with patch(
            "notion_excel_sync.security.hermes_gateway._prepare_authenticated_sync",
            side_effect=commit_noop,
        ) as prepare:
            denied = self.handle(event, FakeGateway(authorized=False))
            self.assertIn("code=command_rejected", denied)
            self.assertIsNone(self.database.get_checkpoint("drive", "item"))
            prepare.assert_not_called()

            marker = self.handle(event, FakeGateway(authorized=True))

        self.assertTrue(marker.startswith("[NX_SYNC_NO_CHANGES]"))
        self.assertEqual(
            self.database.get_checkpoint("drive", "item")["version_id"],
            "v-noop",
        )

    def test_authenticated_prepare_wires_wiki_provider_only_when_index_exists(
        self,
    ) -> None:
        config = load_config(self.config_path)
        config.wiki.enabled = True
        config.wiki.index_db = self.project_root / "data" / "wiki" / "index.db"
        config.wiki.index_db.parent.mkdir(parents=True)
        config.wiki.index_db.write_bytes(b"installed-index")
        identity = TelegramEventIdentity(
            user_id="123",
            chat_id="chat",
            chat_type="private",
            thread_id="",
            message_id="42",
            update_id=314,
            event_timestamp="2026-07-16T01:02:03+00:00",
        )
        onedrive = MagicMock()
        onedrive.get_item.return_value.web_url = "https://example.invalid/workbook"
        prepared = PreparationResult("v1", "v2", 1, None)
        service = MagicMock()
        service.prepare.return_value = prepared
        read_gateway = MagicMock()

        with (
            patch(
                "notion_excel_sync.security.hermes_gateway.build_onedrive_token_provider",
                return_value=object(),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway.OneDriveReadOnlyClient",
                return_value=onedrive,
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
                return_value=read_gateway,
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway.SyncPreparationService",
                return_value=service,
            ) as service_type,
        ):
            result = _prepare_authenticated_sync(config, self.database, identity)
            provider = service_type.call_args.kwargs["knowledge_provider"]
            self.assertIsInstance(provider, AnalyzerKnowledgeProvider)
            self.assertEqual(provider.retriever.index.path, config.wiki.index_db)
            self.assertIs(result, prepared)

            config.wiki.index_db.unlink()
            _prepare_authenticated_sync(config, self.database, identity)
            self.assertIsNone(service_type.call_args.kwargs["knowledge_provider"])

    def test_gateway_apply_passes_live_wiki_binding_guard(self) -> None:
        config = load_config(self.config_path)
        config.wiki.enabled = True
        self.proposal.knowledge_binding = {"generation_digest": "generation-1"}
        self.database.save_proposal(self.proposal)
        now = datetime.now(UTC)
        receipt = ApprovalReceipt(
            proposal_id=self.proposal.proposal_id,
            revision=self.proposal.revision,
            proposal_digest=self.proposal.digest,
            source_version_id=self.proposal.source_version_id,
            source_file_hash=self.proposal.source_file_hash,
            telegram_user_id="123",
            chat_id="chat",
            issued_at=now,
            expires_at=now + timedelta(minutes=15),
            nonce="nonce",
            signature="signature",
        )
        service = MagicMock()
        expected_report = ApplyReport(
            proposal_id=self.proposal.proposal_id,
            applied=[],
            already_applied=[],
            excluded=[],
            deferred=[],
            failed={},
            committed=True,
        )
        generation_context = MagicMock()
        wiki_index = MagicMock()
        wiki_index.generation_lock.return_value = generation_context

        def guard():
            self.assertTrue(generation_context.__enter__.called)
            self.assertFalse(generation_context.__exit__.called)
            return {"generation_digest": "generation-1"}

        def apply_while_generation_is_locked(**kwargs):
            self.assertTrue(generation_context.__enter__.called)
            self.assertFalse(generation_context.__exit__.called)
            self.assertEqual(
                kwargs["knowledge_binding_guard"](),
                {"generation_digest": "generation-1"},
            )
            return expected_report

        service.apply.side_effect = apply_while_generation_is_locked

        with (
            patch(
                "notion_excel_sync.security.hermes_gateway.AppConfig.secret",
                return_value="write-token",
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway.build_onedrive_token_provider",
                return_value=object(),
            ),
            patch("notion_excel_sync.security.hermes_gateway.OneDriveReadOnlyClient"),
            patch(
                "notion_excel_sync.security.hermes_gateway._capture_current_source",
                return_value=("source-v1", "source-hash"),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._wiki_live_binding_guard",
                return_value=guard,
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway.WikiIndex",
                return_value=wiki_index,
            ),
            patch("notion_excel_sync.security.hermes_gateway.HttpNotionGateway"),
            patch("notion_excel_sync.security.hermes_gateway.ApprovedNotionWriter"),
            patch(
                "notion_excel_sync.security.hermes_gateway.ApprovalGatedApplyService",
                return_value=service,
            ),
        ):
            report = _apply_gateway_receipt(
                config,
                self.database,
                MagicMock(),
                receipt,
            )

        self.assertTrue(report.committed)
        self.assertIs(service.apply.call_args.kwargs["knowledge_binding_guard"], guard)
        wiki_index.generation_lock.assert_called_once_with(timeout_seconds=30.0)
        generation_context.__exit__.assert_called_once()

    def test_show_and_set_bind_owner_chat_revision_and_render_exact_commands(self) -> None:
        proposal, change = self.editable_proposal()

        shown = self.handle(
            FakeEvent(
                text=f"/nx_show {proposal.proposal_id} 1",
                source=FakeSource(),
            )
        )

        self.assertIn(f"Operation ID: {change.operation_id}", shown)
        self.assertIn(
            f"/nx_set {proposal.proposal_id} 1 {change.operation_id} exclude",
            shown,
        )
        self.assertIn(f"/nx_approve {proposal.proposal_id} 1 {proposal.digest}", shown)

        for label, source, revision in (
            ("owner", FakeSource(user_id="999"), 1),
            ("chat", FakeSource(chat_id="other"), 1),
            ("revision", FakeSource(), 2),
        ):
            with self.subTest(label=label):
                denied = self.handle(
                    FakeEvent(
                        text=(
                            f"/nx_set {proposal.proposal_id} {revision} "
                            f"{change.operation_id} exclude"
                        ),
                        source=source,
                    )
                )
                self.assertIn("code=command_rejected", denied)
                self.assertEqual(
                    self.database.load_proposal(proposal.proposal_id).revision,
                    1,
                )

        invalid = self.handle(
            FakeEvent(
                text=(
                    f"/nx_set {proposal.proposal_id} 1 {change.operation_id} "
                    "edit NaN"
                ),
                source=FakeSource(),
            )
        )
        self.assertIn("strict JSON", invalid)
        self.assertEqual(self.database.load_proposal(proposal.proposal_id).revision, 1)

        revised_marker = self.handle(
            FakeEvent(
                text=(
                    f"/nx_set {proposal.proposal_id} 1 {change.operation_id} "
                    'edit "user-edited"'
                ),
                source=FakeSource(),
            )
        )
        revised = self.database.load_proposal(proposal.proposal_id)
        self.assertEqual(revised.revision, 2, revised_marker)
        self.assertEqual(revised.operations[0].approved_value, "user-edited")
        self.assertIn(f"/nx_approve {proposal.proposal_id} 2 {revised.digest}", revised_marker)

    def test_independent_correction_rejects_apply_and_defer_set_actions(self) -> None:
        database_name, definition = next(iter(NOTION_DEFINITIONS.items()))
        change = ProposedChange(
            target_database=database_name,
            entity_key="CASE-CORRECTION",
            property_name=definition.title_property,
            kind=ChangeKind.UPDATE,
            current_value="old",
            proposed_value="corrected",
            analyzer="user_correction",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Synthetic independent correction",
            source_refs=[],
        )
        service = ProposalService(self.database, ttl_seconds=1800)
        proposal = service.create(
            "source-v1",
            "source-hash",
            "123",
            "chat",
            [change],
            purpose=ProposalPurpose.USER_CORRECTION,
            correction_binding={
                "version": 1,
                "request_digest": "a" * 64,
                "event_key": "synthetic-event",
            },
        )
        proposal = service.request_approval(proposal.proposal_id)

        for action in ("apply", "defer"):
            with self.subTest(action=action):
                denied = self.handle(
                    FakeEvent(
                        text=(
                            f"/nx_set {proposal.proposal_id} 1 "
                            f"{change.operation_id} {action}"
                        ),
                        source=FakeSource(),
                    )
                )
                self.assertIn("code=command_rejected", denied)
                self.assertIn("only edit or exclude", denied)
                unchanged = self.database.load_proposal(proposal.proposal_id)
                self.assertEqual(unchanged.revision, 1)
                self.assertIs(
                    unchanged.status,
                    ProposalStatus.PENDING_APPROVAL,
                )

    def test_hidden_history_edit_and_orphan_approval_fail_closed(self) -> None:
        database_name, definition = next(iter(NOTION_DEFINITIONS.items()))
        main = ProposedChange(
            target_database=database_name,
            entity_key="CASE-HISTORY-BOUNDARY",
            property_name=definition.title_property,
            kind=ChangeKind.UPDATE,
            current_value="old",
            proposed_value="new",
            analyzer="synthetic_analyzer",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Synthetic visible change",
            source_refs=[],
        )
        history = ProposedChange(
            target_database="변경이력",
            entity_key=f"history:{main.operation_id}",
            property_name="새값",
            kind=ChangeKind.CREATE,
            current_value=None,
            proposed_value="new",
            analyzer="synthetic_history",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Synthetic generated history",
            source_refs=[],
        )
        service = ProposalService(self.database, ttl_seconds=1800)
        proposal = service.create(
            "source-v1",
            "source-hash",
            "123",
            "chat",
            [main, history],
        )
        proposal = service.request_approval(proposal.proposal_id)

        denied_edit = self.handle(
            FakeEvent(
                text=(
                    f"/nx_set {proposal.proposal_id} 1 "
                    f"{history.operation_id} exclude"
                ),
                source=FakeSource(),
            )
        )
        self.assertIn("code=command_rejected", denied_edit)
        self.assertIn(
            "history operations cannot be edited directly",
            denied_edit,
        )
        self.assertEqual(
            self.database.load_proposal(proposal.proposal_id).revision,
            1,
        )

        orphan = ProposedChange(
            target_database="변경이력",
            entity_key="history:missing-operation",
            property_name="새값",
            kind=ChangeKind.CREATE,
            current_value=None,
            proposed_value="orphaned",
            analyzer="synthetic_history",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Synthetic orphan history",
            source_refs=[],
        )
        orphan_proposal = service.create(
            "source-v2",
            "source-hash-2",
            "123",
            "chat",
            [orphan],
        )
        orphan_proposal.status = ProposalStatus.PENDING_APPROVAL
        orphan_proposal.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        self.database.save_proposal(orphan_proposal)

        shown = self.handle(
            FakeEvent(
                text=f"/nx_show {orphan_proposal.proposal_id} 1",
                source=FakeSource(),
            )
        )
        self.assertIn("승인 불가", shown)
        self.assertNotIn("/nx_approve", shown)
        denied_approval = self.handle(
            FakeEvent(
                text=(
                    f"/nx_approve {orphan_proposal.proposal_id} 1 "
                    f"{orphan_proposal.digest}"
                ),
                source=FakeSource(),
            )
        )
        self.assertIn("code=approval_rejected", denied_approval)
        self.assertIn("not displayed on a review card", denied_approval)

    def test_reject_and_recover_are_bound_gateway_commands(self) -> None:
        rejected_proposal, _ = self.editable_proposal()
        rejected = self.handle(
            FakeEvent(
                text=f"/nx_reject {rejected_proposal.proposal_id} 1",
                source=FakeSource(),
            )
        )
        self.assertTrue(rejected.startswith("[NX_PROPOSAL_REJECTED]"))
        self.assertIs(
            self.database.load_proposal(rejected_proposal.proposal_id).status,
            ProposalStatus.REJECTED,
        )

        failed, _ = self.editable_proposal(status=ProposalStatus.FAILED)
        writer = MagicMock()
        writer.inspect.return_value = WritePreconditionState.PENDING
        with (
            patch(
                "notion_excel_sync.security.hermes_gateway._capture_current_source",
                return_value=(failed.source_version_id, failed.source_file_hash),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
                return_value=object(),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway.ApprovedNotionWriter",
                return_value=writer,
            ),
        ):
            recovered = self.handle(
                FakeEvent(
                    text=f"/nx_recover {failed.proposal_id} 1",
                    source=FakeSource(),
                )
            )
        recovery = self.database.load_proposal(failed.proposal_id)
        self.assertEqual(recovery.revision, 2)
        self.assertIs(recovery.status, ProposalStatus.PENDING_APPROVAL)
        self.assertTrue(recovered.startswith("[NX_RECOVERY_PROPOSAL_READY]"))

    def test_recovery_rejects_wiki_drift_before_notion_inspection(self) -> None:
        failed, _ = self.editable_proposal(status=ProposalStatus.FAILED)
        failed.knowledge_binding = {"generation_digest": "generation-1"}
        self.database.save_proposal(failed)
        writer_type = MagicMock()
        read_gateway = MagicMock()

        with (
            patch(
                "notion_excel_sync.security.hermes_gateway._capture_current_source",
                return_value=(failed.source_version_id, failed.source_file_hash),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._wiki_live_binding_guard",
                return_value=lambda: {"generation_digest": "generation-2"},
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
                read_gateway,
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway.ApprovedNotionWriter",
                writer_type,
            ),
        ):
            marker = self.handle(
                FakeEvent(
                    text=f"/nx_recover {failed.proposal_id} 1",
                    source=FakeSource(),
                )
            )

        self.assertIn("Wiki sources changed", marker)
        read_gateway.assert_not_called()
        writer_type.assert_not_called()
        unchanged = self.database.load_proposal(failed.proposal_id)
        self.assertEqual(unchanged.revision, 1)
        self.assertIs(unchanged.status, ProposalStatus.FAILED)

    def test_completed_correction_recovery_tolerates_source_and_wiki_drift(
        self,
    ) -> None:
        database_name, definition = next(iter(NOTION_DEFINITIONS.items()))
        change = ProposedChange(
            target_database=database_name,
            entity_key="CASE-CORRECTION-RECOVERY",
            property_name=definition.title_property,
            kind=ChangeKind.UPDATE,
            current_value="old",
            proposed_value="corrected",
            analyzer="user_correction",
            analyzer_version="1.0.0",
            confidence=1.0,
            reason="Synthetic completed correction",
            source_refs=[
                SourceRef(
                    "drive",
                    "item",
                    "source-v1",
                    "source-hash",
                    "Cases",
                    3,
                    "A3",
                    "corrected",
                )
            ],
        )
        approval = ApprovalService(
            secret=SECRET,
            allowed_users={"123"},
            nonce_used=self.database.nonce_used,
            mark_nonce_used=self.database.mark_nonce_used,
        )
        service = ProposalService(
            self.database,
            approval,
            ttl_seconds=1800,
        )
        proposal = service.create(
            "source-v1",
            "source-hash",
            "123",
            "chat",
            [change],
            purpose=ProposalPurpose.USER_CORRECTION,
            knowledge_binding={"generation_digest": "old-wiki"},
            correction_binding={
                "version": 1,
                "request_digest": "a" * 64,
                "event_key": "synthetic-recovery-event",
                "target": {
                    "version": 1,
                    "database": database_name,
                    "data_source_id": "notion-data-source",
                    "entity_key": change.entity_key,
                    "property": change.property_name,
                    "page_id": "synthetic-page",
                    "parent": {
                        "type": "data_source_id",
                        "id": "notion-data-source",
                    },
                },
            },
        )
        proposal = service.request_approval(proposal.proposal_id)
        receipt = service.approve(
            proposal.proposal_id,
            proposal.revision,
            "123",
            "chat",
            proposal.digest,
        )
        applying = self.database.load_proposal(proposal.proposal_id)
        applying.status = ProposalStatus.APPLYING
        self.database.begin_apply(applying, receipt.nonce)
        self.database.mark_outbox(change.operation_id, "done")
        failed = self.database.load_proposal(proposal.proposal_id)
        failed.status = ProposalStatus.FAILED
        self.database.save_proposal(failed)

        wiki_guard = MagicMock(
            side_effect=AssertionError("Wiki guard must not run")
        )
        notion_reader = MagicMock(
            side_effect=AssertionError("Notion reader must not run")
        )
        with (
            patch(
                "notion_excel_sync.security.hermes_gateway._source_client",
                return_value=object(),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._capture_current_source",
                return_value=("source-v2", "new-source-hash"),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._wiki_live_binding_guard",
                wiki_guard,
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
                notion_reader,
            ),
        ):
            marker = self.handle(
                FakeEvent(
                    text=f"/nx_recover {proposal.proposal_id} 1",
                    source=FakeSource(),
                )
            )

        recovery = self.database.load_proposal(proposal.proposal_id)
        self.assertIs(recovery.status, ProposalStatus.PENDING_APPROVAL)
        self.assertEqual(recovery.revision, 2)
        self.assertEqual(recovery.source_version_id, "source-v2")
        self.assertEqual(recovery.source_file_hash, "new-source-hash")
        self.assertEqual(recovery.knowledge_binding, {})
        self.assertEqual(recovery.operations[0].change.source_refs, [])
        self.assertIs(
            recovery.operations[0].action,
            ProposalAction.EXCLUDE,
        )
        self.assertIn("Wiki 승인 오버레이만 복구", marker)
        self.assertIn("Notion 쓰기는 이전 승인에서 이미 완료", marker)
        self.assertIn("Wiki 승인 오버레이와 최종화만 복구", marker)
        self.assertNotIn('Notion: "old" → "corrected"', marker)
        self.assertNotIn("/nx_set", marker)
        self.assertNotIn("/nx_reject", marker)
        self.assertNotIn(
            "Notion과 Wiki는 아직 변경되지 않았습니다.",
            marker,
        )
        self.assertIn(
            f"/nx_approve {proposal.proposal_id} 2 {recovery.digest}",
            marker,
        )
        for action in (
            'edit "changed-again"',
            "exclude",
            "defer",
            "apply",
        ):
            with self.subTest(action=action):
                denied = self.handle(
                    FakeEvent(
                        text=(
                            f"/nx_set {proposal.proposal_id} 2 "
                            f"{change.operation_id} {action}"
                        ),
                        source=FakeSource(),
                    )
                )
                self.assertIn("code=command_rejected", denied)
                self.assertIn("cannot be revised", denied)
        rejected = self.handle(
            FakeEvent(
                text=f"/nx_reject {proposal.proposal_id} 2",
                source=FakeSource(),
            )
        )
        self.assertIn("code=command_rejected", rejected)
        self.assertIn("proposal state transition was rejected", rejected)
        unchanged = self.database.load_proposal(proposal.proposal_id)
        self.assertEqual(unchanged.revision, 2)
        self.assertIs(unchanged.status, ProposalStatus.PENDING_APPROVAL)
        with self.assertRaises(KeyError):
            self.database.load_latest_receipt(
                proposal.proposal_id,
                recovery.revision,
            )
        wiki_guard.assert_not_called()
        notion_reader.assert_not_called()

    def test_pending_correction_recovery_keeps_strict_source_guard(self) -> None:
        proposal, change = self.editable_proposal()
        proposal.purpose = ProposalPurpose.USER_CORRECTION
        proposal.operations[0].action = ProposalAction.EDIT
        proposal.operations[0].user_override = True
        proposal.correction_binding = {
            "version": 1,
            "request_digest": "b" * 64,
            "event_key": "synthetic-pending-recovery",
        }
        proposal.status = ProposalStatus.FAILED
        self.database.save_proposal(proposal)

        wiki_guard = MagicMock()
        notion_reader = MagicMock()
        with (
            patch(
                "notion_excel_sync.security.hermes_gateway._source_client",
                return_value=object(),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._capture_current_source",
                return_value=("source-v2", "new-source-hash"),
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._wiki_live_binding_guard",
                wiki_guard,
            ),
            patch(
                "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
                notion_reader,
            ),
        ):
            denied = self.handle(
                FakeEvent(
                    text=f"/nx_recover {proposal.proposal_id} 1",
                    source=FakeSource(),
                )
            )

        self.assertIn("configured source changed", denied)
        unchanged = self.database.load_proposal(proposal.proposal_id)
        self.assertEqual(unchanged.revision, 1)
        self.assertIs(unchanged.status, ProposalStatus.FAILED)
        self.assertEqual(
            unchanged.operations[0].change.operation_id,
            change.operation_id,
        )
        wiki_guard.assert_not_called()
        notion_reader.assert_not_called()

    def test_committed_result_redelivery_never_replays_apply(self) -> None:
        committed = self.database.load_proposal("P-1")
        committed.status = ProposalStatus.COMMITTED
        self.database.finalize_success(
            committed,
            drive_id="drive",
            item_id="item",
            version_id="source-v1",
            file_hash="source-hash",
        )
        self.apply_mock.reset_mock()

        marker = self.handle(self.event())

        self.assertTrue(marker.startswith("[NX_SYNC_COMMITTED]"))
        payload = self.marker_payload(marker)
        self.assertTrue(payload["redelivered"])
        self.apply_mock.assert_not_called()

    def test_malformed_reserved_command_is_consumed_without_receipt(self) -> None:
        gateway = FakeGateway()
        event = FakeEvent(
            text="/nx_approve P-1 1 not-a-digest",
            source=FakeSource(),
        )

        marker = self.handle(event, gateway)

        self.assertIn("code=invalid_syntax", marker)
        self.assertEqual(gateway.sources, [])
        self.assertEqual(self.receipt_count(), 0)

    def test_schema_commands_are_authenticated_then_delegated_before_the_model(self) -> None:
        event = FakeEvent(
            text=(
                "/nx_schema_plan government-support-evidence "
                "f0000000000000000000000000000001"
            ),
            source=FakeSource(),
        )
        gateway = FakeGateway()
        with patch(
            "notion_excel_sync.security.hermes_schema_gateway.handle_schema_command",
            return_value="[NX_SCHEMA_TEST_DELEGATED]",
        ) as delegated:
            marker = self.handle(event, gateway)
        self.assertEqual(marker, "[NX_SCHEMA_TEST_DELEGATED]")
        self.assertEqual(gateway.sources, [event.source])
        delegated.assert_called_once()
        self.assertIs(delegated.call_args.args[0], event)
        self.assertEqual(delegated.call_args.args[1], event.text)
        self.assertEqual(self.receipt_count(), 0)
        self.assertIs(
            self.database.load_proposal("P-1").status,
            ProposalStatus.PENDING_APPROVAL,
        )

    def test_gateway_denial_fails_closed_before_project_approval(self) -> None:
        gateway = FakeGateway(authorized=False)

        marker = self.handle(self.event(), gateway)

        self.assertIn("code=approval_rejected", marker)
        self.assertEqual(len(gateway.sources), 1)
        self.assertEqual(self.receipt_count(), 0)
        self.assertFalse((self.project_root / "data" / "receipts").exists())

    def test_project_owner_chat_revision_and_digest_are_all_bound(self) -> None:
        cases = {
            "project_allowlist_and_owner": self.event(user_id="999"),
            "original_chat": self.event(chat_id="other-chat"),
            "current_revision": self.event(revision=2),
            "current_digest": self.event(digest="0" * 64),
        }
        for label, event in cases.items():
            with self.subTest(label=label):
                marker = self.handle(event)
                self.assertIn("code=approval_rejected", marker)
                self.assertEqual(self.receipt_count(), 0)

    def test_expired_proposal_fails_closed(self) -> None:
        self.proposal.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        self.database.save_proposal(self.proposal)

        marker = self.handle(self.event())

        self.assertIn("code=approval_rejected", marker)
        self.assertIn("expired", marker)
        self.assertEqual(self.receipt_count(), 0)

    def test_default_config_path_uses_sync_local_json(self) -> None:
        self.assertEqual(_resolve_config_path(self.project_root), self.config_path.resolve())

    def test_windows_reserved_colon_is_not_used_in_receipt_filename(self) -> None:
        proposal = ProposalRevision(
            proposal_id="case:123",
            revision=1,
            source_version_id="source-v1",
            source_file_hash="source-hash",
            requested_by="123",
            chat_id="chat",
            operations=[],
            status=ProposalStatus.PENDING_APPROVAL,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        self.database.save_proposal(proposal)
        event = FakeEvent(
            text=f"/nx_approve case:123 1 {proposal.digest}",
            source=FakeSource(),
        )

        marker = self.handle(event)

        payload = self.marker_payload(marker)
        receipt_path = self.receipt_path(payload)
        self.assertTrue(receipt_path.exists())
        self.assertNotIn(":", receipt_path.name)

    def test_receipt_reference_rejects_path_traversal(self) -> None:
        receipts = self.project_root / "data" / "receipts"
        for reference in (
            "../receipt.json",
            r"C:\Users\private-user\receipt.json",
            "nxr-" + ("a" * 63) + ".json",
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(ReceiptReferenceError):
                    resolve_receipt_reference(receipts, reference)

    def test_apply_error_marker_never_echoes_exception_or_local_path(self) -> None:
        sensitive = (
            r"C:\Users\private-user\AppData\Local\secret.txt "
            "gateway-write-token=do-not-echo"
        )
        self.apply_mock.side_effect = RuntimeError(sensitive)

        marker = self.handle(self.event())
        payload = self.marker_payload(marker)

        self.assertTrue(marker.startswith("[NX_SYNC_ERROR]"))
        self.assertEqual(payload["error_code"], "apply_worker_failed")
        self.assertNotIn("RuntimeError", marker)
        self.assertNotIn("private-user", marker)
        self.assertNotIn("gateway-write-token", marker)
        self.assertNotIn(sensitive, marker)

    def test_plugin_prefers_explicit_project_root_when_copied(self) -> None:
        plugin_path = (
            Path(__file__).resolve().parents[1]
            / ".hermes"
            / "plugins"
            / "notion-excel-sync-attestor"
            / "__init__.py"
        )
        spec = importlib.util.spec_from_file_location("test_nx_attestor_plugin", plugin_path)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        self.assertIsInstance(module, ModuleType)
        spec.loader.exec_module(module)

        self.assertEqual(module._PROJECT_ROOT, self.project_root.resolve())

    def test_plugin_runs_reserved_command_off_loop_and_skips_agent(self) -> None:
        plugin_path = (
            Path(__file__).resolve().parents[1]
            / ".hermes"
            / "plugins"
            / "notion-excel-sync-attestor"
            / "__init__.py"
        )
        spec = importlib.util.spec_from_file_location("test_nx_worker_plugin", plugin_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        adapter = FakeAdapter()
        gateway = FakeGateway()
        gateway._adapter_for_source = lambda _source: adapter  # type: ignore[attr-defined]
        commands = [
            "/nx_sync",
            "/nx_show P-1 1",
            "/nx_set P-1 1 op-1 exclude",
            "/nx_reject P-1 1",
            "/nx_recover P-1 1",
            (
                '/nx_correct {"database":"한국 특허 사건",'
                '"entity_key":"SS-SYNTHETIC","property":"현재상태",'
                '"value":"보류","reason":"합성 확인"}'
            ),
            f"/nx_approve P-1 1 {self.proposal.digest}",
        ]

        async def exercise() -> list[dict[str, str]]:
            decisions = []
            with patch(
                "notion_excel_sync.security.hermes_gateway.handle_pre_gateway_dispatch",
                return_value="[NX_SYNC_COMMITTED] {}",
            ):
                for index, text in enumerate(commands):
                    event = FakeEvent(
                        text=text,
                        source=FakeSource(message_id=str(100 + index)),
                        platform_update_id=400 + index,
                    )
                    decision = module._pre_gateway_dispatch(event=event, gateway=gateway)
                    self.assertEqual(decision["action"], "skip")
                    decisions.append(decision)
                    for _ in range(50):
                        if len(adapter.sent) == index + 1:
                            break
                        await asyncio.sleep(0.01)
                return decisions

        decisions = asyncio.run(exercise())
        self.assertTrue(
            all(item["reason"] == "nx-command-consumed" for item in decisions)
        )
        self.assertEqual(
            adapter.sent,
            [("chat", "[NX_SYNC_COMMITTED] {}")] * len(commands),
        )

        set_one = FakeEvent(
            text="/nx_set P-1 1 op-1 exclude",
            source=FakeSource(),
        )
        set_two = FakeEvent(
            text="/nx_set P-1 1 op-2 defer",
            source=FakeSource(),
        )
        approve = self.event()
        self.assertEqual(module._active_key(set_one), module._active_key(set_two))
        self.assertEqual(module._active_key(set_one), module._active_key(approve))
        proposal_key = module._active_key(set_one)
        with module._ACTIVE_LOCK:
            module._ACTIVE.add(proposal_key)
        try:
            blocked = module._pre_gateway_dispatch(event=set_two, gateway=gateway)
        finally:
            with module._ACTIVE_LOCK:
                module._ACTIVE.discard(proposal_key)
        self.assertEqual(blocked["reason"], "nx-command-in-progress")


if __name__ == "__main__":
    unittest.main()
