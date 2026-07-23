from __future__ import annotations

import copy
import re
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from notion_excel_sync.adapters.notion import NotionDatabaseCreateResult, NotionError
from notion_excel_sync.models import SchemaProposalStatus
from notion_excel_sync.persistence.database import SchemaStateError, StateDatabase
from notion_excel_sync.security.hermes_schema_gateway import (
    handle_schema_command,
    is_schema_command,
)
from notion_excel_sync.workflow.notion_schema import (
    SCHEMA_API_VERSION,
    SCHEMA_LOGICAL_NAME,
    SCHEMA_PARENT_PAGE_ID,
)


BOT_ID = "11111111111111111111111111111111"
CASE_SOURCE_ID = "22222222222222222222222222222222"
GROUP_SOURCE_ID = "33333333333333333333333333333333"
COST_SOURCE_ID = "44444444444444444444444444444444"
EVIDENCE_SOURCE_ID = "55555555555555555555555555555555"
CREATED_DATABASE_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CREATED_DATA_SOURCE_ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _hyphenated(value: str) -> str:
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"


def _rich_text(value: str) -> list[dict[str, object]]:
    return [
        {
            "type": "text",
            "text": {"content": value},
            "plain_text": value,
        }
    ]


@dataclass
class FakeSource:
    platform: str = "telegram"
    user_id: str = "123456789"
    chat_id: str = "chat-1"
    thread_id: str = "thread-1"
    message_id: str = "1"
    is_bot: bool = False


@dataclass
class FakeEvent:
    source: FakeSource
    platform_update_id: int


class FakeConfig:
    def __init__(self) -> None:
        self.approval_secret = "schema-test-secret-that-is-long-enough"
        self.notion = SimpleNamespace(
            write_token_env="GATEWAY_RELAY_NX_NOTION_TOKEN",
            api_version=SCHEMA_API_VERSION,
            schema_parent_page_id=SCHEMA_PARENT_PAGE_ID,
            databases={
                "한국 특허 사건": CASE_SOURCE_ID,
                "그룹": GROUP_SOURCE_ID,
                "비용·청구": COST_SOURCE_ID,
                "근거자료": EVIDENCE_SOURCE_ID,
                SCHEMA_LOGICAL_NAME: "REPLACE_WITH_DATABASE_OR_DATA_SOURCE_ID",
            },
        )
        self.approval = SimpleNamespace(
            secret_env="GATEWAY_RELAY_NX_APPROVAL_SECRET",
            ttl_seconds=1800,
            allowed_telegram_users={"123456789"},
        )

    def secret(self, env_name: str) -> str:
        if env_name == self.approval.secret_env:
            return self.approval_secret
        return "secret-notion-token"


class ReadOnlyFakeNotionGateway:
    """Reader fixture with no create_database attribute."""

    def __init__(self, source: "FakeNotionGateway") -> None:
        self.source = source

    def get_self(self):
        return self.source.get_self()

    def get_page(self, page_id: str):
        return self.source.get_page(page_id)

    def list_block_children(self, block_id: str, *, page_size: int = 100):
        return self.source.list_block_children(block_id, page_size=page_size)

    def get_database(self, database_id: str):
        return self.source.get_database(database_id)

    def get_data_source(self, data_source_id: str):
        return self.source.get_data_source(data_source_id)


class FakeNotionGateway:
    def __init__(self) -> None:
        self.verifier = None
        self.create_count = 0
        self.create_mode = "success"
        self.api_projection_mode = False
        self.hyphenated_create_result = False
        self.read_count = 0
        self.fail_next_parent_read = False
        self.parent = {
            "object": "page",
            "id": SCHEMA_PARENT_PAGE_ID,
            "in_trash": False,
        }
        self.bot = {
            "object": "user",
            "id": BOT_ID,
            "type": "bot",
            "bot": {"owner": {"type": "workspace", "workspace": True}},
        }
        relation_rows = (
            ("한국 특허 사건", CASE_SOURCE_ID, "66666666666666666666666666666666"),
            ("그룹", GROUP_SOURCE_ID, "77777777777777777777777777777777"),
            ("비용·청구", COST_SOURCE_ID, "88888888888888888888888888888888"),
            ("근거자료", EVIDENCE_SOURCE_ID, "99999999999999999999999999999999"),
        )
        self.data_sources: dict[str, dict[str, object]] = {
            source_id: {
                "object": "data_source",
                "id": source_id,
                "name": name,
                "in_trash": False,
                "parent": {"type": "database_id", "database_id": database_id},
            }
            for name, source_id, database_id in relation_rows
        }
        self.databases: dict[str, dict[str, object]] = {}

    def factory(self, _config, verifier):
        self.verifier = verifier
        return self

    def get_self(self):
        self.read_count += 1
        return copy.deepcopy(self.bot)

    def get_page(self, page_id: str):
        self.read_count += 1
        if self.fail_next_parent_read:
            self.fail_next_parent_read = False
            raise NotionError("sensitive transient read failure")
        if page_id != SCHEMA_PARENT_PAGE_ID:
            raise KeyError(page_id)
        return copy.deepcopy(self.parent)

    def list_block_children(self, block_id: str, *, page_size: int = 100):
        self.read_count += 1
        assert block_id == SCHEMA_PARENT_PAGE_ID
        assert page_size == 100
        return [
            {
                "object": "block",
                "type": "child_database",
                "id": database_id,
            }
            for database_id in self.databases
        ]

    def get_database(self, database_id: str):
        self.read_count += 1
        return copy.deepcopy(self.databases[database_id.replace("-", "").lower()])

    def get_data_source(self, data_source_id: str):
        self.read_count += 1
        return copy.deepcopy(self.data_sources[data_source_id.replace("-", "").lower()])

    def _install_remote(self, body) -> None:
        created_at = datetime.now(UTC)
        now = created_at.isoformat().replace("+00:00", "Z")
        properties: dict[str, dict[str, object]] = {}
        for name, definition in body["initial_data_source"]["properties"].items():
            property_type = next(iter(definition))
            config = copy.deepcopy(definition[property_type])
            if property_type == "relation":
                config["type"] = "single_property"
            properties[name] = {
                "id": f"property-{len(properties)}",
                "name": name,
                "type": property_type,
                property_type: config,
            }
        self.databases[CREATED_DATABASE_ID] = {
            "object": "database",
            "id": CREATED_DATABASE_ID,
            "parent": {"type": "page_id", "page_id": SCHEMA_PARENT_PAGE_ID},
            "in_trash": False,
            "title": copy.deepcopy(body["title"]),
            "description": copy.deepcopy(body["description"]),
            "is_inline": False,
            "data_sources": [
                {"id": CREATED_DATA_SOURCE_ID, "name": SCHEMA_LOGICAL_NAME}
            ],
            "created_by": {"object": "user", "id": BOT_ID},
            "created_time": now,
        }
        self.data_sources[CREATED_DATA_SOURCE_ID] = {
            "object": "data_source",
            "id": CREATED_DATA_SOURCE_ID,
            "name": SCHEMA_LOGICAL_NAME,
            "parent": {"type": "database_id", "database_id": CREATED_DATABASE_ID},
            "in_trash": False,
            "created_by": {"object": "user", "id": BOT_ID},
            "created_time": now,
            "properties": properties,
        }
        if self.api_projection_mode:
            self.databases[CREATED_DATABASE_ID].pop("created_by")
            self.data_sources[CREATED_DATA_SOURCE_ID]["created_time"] = (
                created_at.replace(second=0, microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )

    def create_database(
        self,
        expected_body,
        *,
        operation_id,
        approval,
        precondition,
    ):
        self.create_count += 1
        if self.verifier is None or self.verifier(
            approval,
            operation_id,
            expected_body,
            precondition,
            SCHEMA_API_VERSION,
        ) is not True:
            raise AssertionError("fake mutation reached without exact verifier approval")
        if self.create_mode == "rejected":
            raise NotionError(
                "sensitive rejection detail",
                status=400,
                mutation_outcome_unknown=False,
            )
        self._install_remote(expected_body)
        if self.create_mode == "unknown":
            raise NotionError(
                "sensitive remote text must not escape",
                mutation_outcome_unknown=True,
            )
        if self.create_mode == "generic_unknown":
            raise RuntimeError("sensitive unexpected create failure")
        return NotionDatabaseCreateResult(
            database_id=(
                _hyphenated(CREATED_DATABASE_ID)
                if self.hyphenated_create_result
                else CREATED_DATABASE_ID
            ),
            data_source_id=(
                _hyphenated(CREATED_DATA_SOURCE_ID)
                if self.hyphenated_create_result
                else CREATED_DATA_SOURCE_ID
            ),
        )


class HermesSchemaGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = StateDatabase(Path(self.temp_dir.name) / "state.db")
        self.database.initialize()
        self.config = FakeConfig()
        self.notion = FakeNotionGateway()
        self.factory_patch = patch(
            "notion_excel_sync.security.hermes_schema_gateway._gateway_factory",
            self.notion.factory,
        )
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)

    @staticmethod
    def event(
        update_id: int,
        *,
        message_id: str | None = None,
        user_id: str = "123456789",
        chat_id: str = "chat-1",
        thread_id: str = "thread-1",
    ) -> FakeEvent:
        return FakeEvent(
            source=FakeSource(
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id or str(update_id),
            ),
            platform_update_id=update_id,
        )

    def plan(self) -> tuple[str, int, str, str]:
        result = handle_schema_command(
            self.event(1),
            f"/nx_schema_plan government-support-evidence {SCHEMA_PARENT_PAGE_ID}",
            self.config,
            self.database,
        )
        match = re.search(
            r"Proposal: (SP-[A-Za-z0-9._:-]+).*?Revision: ([0-9]+).*?Digest: ([0-9a-f]{64})",
            result,
            re.DOTALL,
        )
        self.assertIsNotNone(match, result)
        assert match is not None
        return match.group(1), int(match.group(2)), match.group(3), result

    def prepare_rotated_recovery(self) -> tuple[str, int, str]:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.create_mode = "unknown"
        approval = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_UNKNOWN", approval)
        proposal = self.database.load_schema_proposal(proposal_id)
        self.database.mark_schema_apply_journal(
            proposal.operation_id,
            "unknown",
            remote_database_id=_hyphenated(CREATED_DATABASE_ID),
            remote_data_source_id=_hyphenated(CREATED_DATA_SOURCE_ID),
        )
        original_marker = copy.deepcopy(
            self.notion.databases[CREATED_DATABASE_ID]["description"]
        )
        self.notion.databases[CREATED_DATABASE_ID]["description"] = _rich_text(
            "foreign-marker"
        )
        conflict = handle_schema_command(
            self.event(3),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("RECOVERY_CONFLICT", conflict)
        self.notion.databases[CREATED_DATABASE_ID]["description"] = original_marker
        self.config.approval_secret = "rotated-schema-secret-that-is-long-enough"
        return proposal_id, revision, digest

    def audit_count(self, event_type: str | None = None) -> int:
        with self.database.session() as connection:
            if event_type is None:
                row = connection.execute("SELECT COUNT(*) AS count FROM audit_log").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM audit_log WHERE event_type = ?",
                    (event_type,),
                ).fetchone()
        assert row is not None
        return int(row["count"])

    def test_recognizes_only_reserved_schema_tokens(self) -> None:
        self.assertTrue(is_schema_command("/nx_schema_plan bad"))
        self.assertTrue(is_schema_command(" /nx_schema_approve x "))
        self.assertFalse(is_schema_command("/nx_schema_approve_now x"))
        self.assertFalse(is_schema_command("승인"))

    def test_plan_and_show_are_read_only_and_event_replay_is_stable(self) -> None:
        proposal_id, revision, digest, result = self.plan()
        self.assertIn("아직 Notion은 변경되지 않았습니다", result)
        self.assertEqual(self.notion.create_count, 0)
        replay = handle_schema_command(
            self.event(1),
            f"/nx_schema_plan government-support-evidence {SCHEMA_PARENT_PAGE_ID}",
            self.config,
            self.database,
        )
        self.assertEqual(replay, result)
        shown = handle_schema_command(
            self.event(2),
            f"/nx_schema_show {proposal_id} {revision}",
            self.config,
            self.database,
        )
        self.assertIn(digest, shown)
        self.assertIn("증빙명: title", shown)
        self.assertEqual(self.notion.create_count, 0)

    def test_schema_commands_require_a_configured_parent_before_remote_reads(self) -> None:
        self.config.notion.schema_parent_page_id = None

        result = handle_schema_command(
            self.event(50),
            f"/nx_schema_plan government-support-evidence {SCHEMA_PARENT_PAGE_ID}",
            self.config,
            self.database,
        )

        self.assertIn("DENIED", result)
        self.assertEqual(self.notion.read_count, 0)
        self.assertEqual(self.notion.create_count, 0)

    def test_parent_config_drift_blocks_show_reject_approve_and_recovery(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.config.notion.schema_parent_page_id = "e" * 32
        commands = (
            f"/nx_schema_show {proposal_id} {revision}",
            f"/nx_schema_reject {proposal_id} {revision}",
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
        )

        for update_id, command in enumerate(commands, start=51):
            result = handle_schema_command(
                self.event(update_id),
                command,
                self.config,
                self.database,
            )
            self.assertIn("DENIED", result)

        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.PENDING_APPROVAL,
        )
        self.assertEqual(self.notion.create_count, 0)

    def test_wrong_digest_user_chat_and_thread_never_create(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        cases = (
            (self.event(2), "0" * 64),
            (self.event(3, user_id="999"), digest),
            (self.event(4, chat_id="other"), digest),
            (self.event(5, thread_id="other"), digest),
        )
        for event, attempted_digest in cases:
            result = handle_schema_command(
                event,
                f"/nx_schema_approve {proposal_id} {revision} {attempted_digest}",
                self.config,
                self.database,
            )
            self.assertIn("DENIED", result)
        self.assertEqual(self.notion.create_count, 0)

    def test_same_telegram_event_cannot_be_replayed_as_another_user(self) -> None:
        _, _, _, original = self.plan()
        replay = handle_schema_command(
            self.event(1, user_id="999"),
            f"/nx_schema_plan government-support-evidence {SCHEMA_PARENT_PAGE_ID}",
            self.config,
            self.database,
        )
        self.assertNotEqual(replay, original)
        self.assertIn("EVENT_REPLAY_MISMATCH", replay)
        self.assertEqual(self.notion.create_count, 0)

    def test_exact_approval_creates_once_and_does_not_touch_excel_checkpoint(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        before = self.database.get_checkpoint("drive", "item")
        command = f"/nx_schema_approve {proposal_id} {revision} {digest}"
        event = self.event(2)
        result = handle_schema_command(event, command, self.config, self.database)
        self.assertIn("NX_SCHEMA_COMMITTED", result)
        self.assertEqual(self.notion.create_count, 1)
        self.assertEqual(self.database.get_checkpoint("drive", "item"), before)
        installed = self.database.get_schema_installation(SCHEMA_LOGICAL_NAME)
        self.assertIsNotNone(installed)
        assert installed is not None
        self.assertEqual(installed["data_source_id"], CREATED_DATA_SOURCE_ID)

        replay = handle_schema_command(event, command, self.config, self.database)
        self.assertEqual(replay, result)
        self.assertEqual(self.notion.create_count, 1)

    def test_private_chat_empty_thread_exact_approval_commits_once(self) -> None:
        plan_event = self.event(10, thread_id="")
        planned = handle_schema_command(
            plan_event,
            f"/nx_schema_plan government-support-evidence {SCHEMA_PARENT_PAGE_ID}",
            self.config,
            self.database,
        )
        match = re.search(
            r"Proposal: (SP-[A-Za-z0-9._:-]+).*?Revision: ([0-9]+).*?Digest: ([0-9a-f]{64})",
            planned,
            re.DOTALL,
        )
        self.assertIsNotNone(match, planned)
        assert match is not None
        command = (
            f"/nx_schema_approve {match.group(1)} {match.group(2)} {match.group(3)}"
        )
        result = handle_schema_command(
            self.event(11, thread_id=""),
            command,
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_COMMITTED", result)
        self.assertEqual(self.notion.create_count, 1)

    def test_exact_approval_accepts_current_notion_api_projection(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.api_projection_mode = True
        result = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_COMMITTED", result)
        self.assertEqual(self.notion.create_count, 1)
        self.assertIsNotNone(
            self.database.get_schema_installation(SCHEMA_LOGICAL_NAME)
        )

    def test_hyphenated_create_response_is_canonicalized_before_journaling(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.hyphenated_create_result = True
        result = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_COMMITTED", result)
        proposal = self.database.load_schema_proposal(proposal_id)
        journal = self.database.schema_apply_journal_state(proposal.operation_id)
        assert journal is not None
        self.assertEqual(journal["remote_database_id"], CREATED_DATABASE_ID)
        self.assertEqual(journal["remote_data_source_id"], CREATED_DATA_SOURCE_ID)

    def test_transient_preapproval_read_failure_does_not_strand_receipt(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.fail_next_parent_read = True
        command = f"/nx_schema_approve {proposal_id} {revision} {digest}"
        failed = handle_schema_command(
            self.event(2),
            command,
            self.config,
            self.database,
        )
        self.assertIn("READ_FAILED", failed)
        self.assertNotIn("sensitive transient read failure", failed)
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.PENDING_APPROVAL,
        )
        self.assertEqual(self.notion.create_count, 0)

        committed = handle_schema_command(
            self.event(3),
            command,
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_COMMITTED", committed)
        self.assertEqual(self.notion.create_count, 1)

    def test_preapproval_live_binding_drift_marks_proposal_stale(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.data_sources[GROUP_SOURCE_ID]["name"] = "변경된 그룹"
        result = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("DENIED", result)
        self.assertEqual(self.notion.create_count, 0)
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.STALE,
        )

    def test_runtime_api_or_target_config_drift_marks_proposal_stale(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.config.notion.api_version = "2025-09-03"
        result = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("DENIED", result)
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.STALE,
        )
        self.assertEqual(self.notion.create_count, 0)

    def test_runtime_target_configuration_drift_marks_proposal_stale(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.config.notion.databases[SCHEMA_LOGICAL_NAME] = CREATED_DATA_SOURCE_ID
        result = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("DENIED", result)
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.STALE,
        )
        self.assertEqual(self.notion.create_count, 0)

    def test_post_commit_preapply_abort_is_stale_and_replay_is_stable(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        command = f"/nx_schema_approve {proposal_id} {revision} {digest}"
        event = self.event(2)
        with patch.object(
            self.database,
            "begin_schema_apply",
            side_effect=SchemaStateError("sensitive local failure"),
        ):
            result = handle_schema_command(
                event,
                command,
                self.config,
                self.database,
            )
        self.assertIn("DENIED", result)
        self.assertNotIn("sensitive local failure", result)
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.STALE,
        )
        self.assertEqual(self.notion.create_count, 0)
        self.assertEqual(
            handle_schema_command(event, command, self.config, self.database),
            result,
        )

    def test_unknown_outcome_never_retries_and_recovery_is_get_only(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.create_mode = "unknown"
        approve_command = f"/nx_schema_approve {proposal_id} {revision} {digest}"
        result = handle_schema_command(
            self.event(2),
            approve_command,
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_UNKNOWN", result)
        self.assertNotIn("sensitive remote text", result)
        self.assertEqual(self.notion.create_count, 1)
        proposal = self.database.load_schema_proposal(proposal_id)
        self.assertIs(proposal.status, SchemaProposalStatus.UNKNOWN)

        replay = handle_schema_command(
            self.event(2),
            approve_command,
            self.config,
            self.database,
        )
        self.assertEqual(replay, result)
        self.assertEqual(self.notion.create_count, 1)

        recovered = handle_schema_command(
            self.event(3),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_RECOVERED", recovered)
        self.assertEqual(self.notion.create_count, 1)
        self.assertIsNotNone(
            self.database.get_schema_installation(SCHEMA_LOGICAL_NAME)
        )

    def test_unknown_projection_recovery_uses_exact_journal_ids_get_only(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.api_projection_mode = True
        self.notion.create_mode = "unknown"
        approval = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_UNKNOWN", approval)
        self.assertEqual(self.notion.create_count, 1)
        proposal = self.database.load_schema_proposal(proposal_id)
        self.database.mark_schema_apply_journal(
            proposal.operation_id,
            "unknown",
            remote_database_id=_hyphenated(CREATED_DATABASE_ID),
            remote_data_source_id=_hyphenated(CREATED_DATA_SOURCE_ID),
        )

        recovery = handle_schema_command(
            self.event(3),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_RECOVERED", recovery)
        self.assertEqual(self.notion.create_count, 1)
        self.assertIsNotNone(
            self.database.get_schema_installation(SCHEMA_LOGICAL_NAME)
        )
        installation = self.database.get_schema_installation(SCHEMA_LOGICAL_NAME)
        assert installation is not None
        self.assertEqual(
            installation["database_id"],
            _hyphenated(CREATED_DATABASE_ID),
        )

    def test_rotated_secret_uses_fresh_recovery_authority_get_only(self) -> None:
        proposal_id, revision, digest = self.prepare_rotated_recovery()
        checkpoint = self.database.get_checkpoint("drive", "item")
        event = self.event(4)
        command = f"/nx_schema_recover {proposal_id} {revision} {digest}"
        with patch(
            "notion_excel_sync.security.hermes_schema_gateway._gateway_factory",
            side_effect=lambda *_: ReadOnlyFakeNotionGateway(self.notion),
        ):
            result = handle_schema_command(
                event,
                command,
                self.config,
                self.database,
            )
        self.assertIn("NX_SCHEMA_RECOVERED", result)
        self.assertEqual(self.notion.create_count, 1)
        self.assertEqual(self.database.get_checkpoint("drive", "item"), checkpoint)
        installation = self.database.get_schema_installation(SCHEMA_LOGICAL_NAME)
        assert installation is not None
        self.assertEqual(
            installation["database_id"],
            _hyphenated(CREATED_DATABASE_ID),
        )
        with self.database.session() as connection:
            receipt = connection.execute(
                "SELECT purpose, used_at FROM schema_recovery_receipt"
            ).fetchone()
            recovery_event = connection.execute(
                "SELECT purpose, status FROM schema_recovery_event_claim"
            ).fetchone()
        assert receipt is not None and recovery_event is not None
        self.assertEqual(receipt["purpose"], "notion_schema_recovery/v1")
        self.assertIsNotNone(receipt["used_at"])
        self.assertEqual(recovery_event["purpose"], "notion_schema_recovery/v1")
        self.assertEqual(recovery_event["status"], "completed")

        reads = self.notion.read_count
        audits = self.audit_count()
        replay = handle_schema_command(event, command, self.config, self.database)
        self.assertEqual(replay, result)
        self.assertEqual(self.notion.read_count, reads)
        self.assertEqual(self.notion.create_count, 1)
        self.assertEqual(self.audit_count(), audits)
        mismatches = (
            (self.event(4, user_id="other-user"), command),
            (self.event(4, chat_id="other-chat"), command),
            (self.event(4, thread_id="other-thread"), command),
            (
                event,
                f"/nx_schema_recover {proposal_id} {revision} {'0' * 64}",
            ),
        )
        for changed_event, changed_command in mismatches:
            mismatch = handle_schema_command(
                changed_event,
                changed_command,
                self.config,
                self.database,
            )
            self.assertIn("EVENT_REPLAY_MISMATCH", mismatch)
        self.assertEqual(self.notion.read_count, reads)
        self.assertEqual(self.notion.create_count, 1)
        self.assertEqual(self.audit_count(), audits)

    def test_rotated_recovery_requires_completed_exact_conflict_before_reads(self) -> None:
        proposal_id, revision, digest = self.prepare_rotated_recovery()
        with self.database.session() as connection:
            connection.execute(
                "UPDATE schema_gateway_event_claim SET status = 'pending', "
                "safe_result = NULL, completed_at = NULL "
                "WHERE command_name = 'nx_schema_recover' "
                "AND telegram_update_id = 3"
            )
        reads = self.notion.read_count
        result = handle_schema_command(
            self.event(4),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("DENIED", result)
        self.assertEqual(self.notion.read_count, reads)
        self.assertEqual(self.notion.create_count, 1)
        self.assertIsNone(self.database.get_schema_installation(SCHEMA_LOGICAL_NAME))

    def test_rotated_recovery_rejects_tampered_conflict_hash_before_reads(self) -> None:
        proposal_id, revision, digest = self.prepare_rotated_recovery()
        with self.database.session() as connection:
            connection.execute(
                "UPDATE schema_gateway_event_claim SET command_hash = ? "
                "WHERE command_name = 'nx_schema_recover' "
                "AND telegram_update_id = 3",
                ("0" * 64,),
            )
        reads = self.notion.read_count
        result = handle_schema_command(
            self.event(4),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("DENIED", result)
        self.assertEqual(self.notion.read_count, reads)
        self.assertEqual(self.notion.create_count, 1)

    def test_recovery_raw_id_toctou_is_rejected_after_remote_verification(self) -> None:
        proposal_id, revision, digest = self.prepare_rotated_recovery()
        proposal = self.database.load_schema_proposal(proposal_id)
        original_finalize = self.database.finalize_schema_recovery_installation

        def tamper_then_finalize(*args, **kwargs):
            with self.database.session() as connection:
                connection.execute(
                    "UPDATE schema_apply_journal SET remote_database_id = ?, "
                    "remote_data_source_id = ? WHERE operation_id = ?",
                    (
                        CREATED_DATABASE_ID,
                        CREATED_DATA_SOURCE_ID,
                        proposal.operation_id,
                    ),
                )
            return original_finalize(*args, **kwargs)

        with patch.object(
            self.database,
            "finalize_schema_recovery_installation",
            side_effect=tamper_then_finalize,
        ):
            result = handle_schema_command(
                self.event(4),
                f"/nx_schema_recover {proposal_id} {revision} {digest}",
                self.config,
                self.database,
            )
        self.assertIn("DENIED", result)
        self.assertEqual(self.notion.create_count, 1)
        self.assertIsNone(self.database.get_schema_installation(SCHEMA_LOGICAL_NAME))
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.UNKNOWN,
        )
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT nonce FROM schema_recovery_receipt"
            ).fetchone()
        assert row is not None
        self.assertFalse(self.database.schema_recovery_nonce_used(row["nonce"]))

    def test_recovery_finalize_audit_failure_rolls_back_all_privileged_state(self) -> None:
        proposal_id, revision, digest = self.prepare_rotated_recovery()
        with self.database.session() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_recovery_commit_audit
                BEFORE INSERT ON audit_log
                WHEN NEW.event_type = 'schema_recovery_committed'
                BEGIN
                    SELECT RAISE(ABORT, 'forced audit failure');
                END
                """
            )
        result = handle_schema_command(
            self.event(4),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("INTERNAL_ERROR", result)
        self.assertEqual(self.notion.create_count, 1)
        self.assertIsNone(self.database.get_schema_installation(SCHEMA_LOGICAL_NAME))
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.UNKNOWN,
        )
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT nonce FROM schema_recovery_receipt"
            ).fetchone()
        assert row is not None
        self.assertFalse(self.database.schema_recovery_nonce_used(row["nonce"]))

    def test_recovery_outer_result_failure_replays_without_remote_or_audit(self) -> None:
        proposal_id, revision, digest = self.prepare_rotated_recovery()
        event = self.event(4)
        command = f"/nx_schema_recover {proposal_id} {revision} {digest}"
        with patch.object(
            self.database,
            "complete_schema_gateway_event",
            side_effect=SchemaStateError("forced outer completion failure"),
        ):
            first = handle_schema_command(
                event,
                command,
                self.config,
                self.database,
            )
        self.assertIn("EVENT_RESULT", first)
        self.assertIsNotNone(self.database.get_schema_installation(SCHEMA_LOGICAL_NAME))
        reads = self.notion.read_count
        audits = self.audit_count()

        replay = handle_schema_command(event, command, self.config, self.database)
        self.assertIn("NX_SCHEMA_RECOVERED", replay)
        self.assertEqual(self.notion.read_count, reads)
        self.assertEqual(self.notion.create_count, 1)
        self.assertEqual(self.audit_count(), audits)

    def _assert_unknown_mark_failure_never_retries(self, create_mode: str) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.create_mode = create_mode
        command = f"/nx_schema_approve {proposal_id} {revision} {digest}"
        event = self.event(2)
        with patch(
            "notion_excel_sync.security.hermes_schema_gateway._mark_unknown",
            side_effect=SchemaStateError("sensitive journal failure"),
        ):
            result = handle_schema_command(
                event,
                command,
                self.config,
                self.database,
            )
        self.assertIn("NX_SCHEMA_UNKNOWN", result)
        self.assertNotIn("sensitive", result)
        self.assertEqual(self.notion.create_count, 1)

        replay = handle_schema_command(
            event,
            command,
            self.config,
            self.database,
        )
        self.assertEqual(replay, result)
        self.assertEqual(self.notion.create_count, 1)

    def test_unknown_http_and_journal_failure_still_returns_unknown_once(self) -> None:
        self._assert_unknown_mark_failure_never_retries("unknown")

    def test_unexpected_create_and_journal_failure_still_returns_unknown_once(self) -> None:
        self._assert_unknown_mark_failure_never_retries("generic_unknown")

    def test_conclusive_remote_rejection_is_failed_not_unknown(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.create_mode = "rejected"
        result = handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_CREATE_REJECTED", result)
        self.assertNotIn("sensitive rejection detail", result)
        self.assertEqual(self.notion.create_count, 1)
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.FAILED,
        )
        self.assertIsNone(self.database.get_schema_installation(SCHEMA_LOGICAL_NAME))

    def test_recovery_does_not_adopt_foreign_same_title_database(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.create_mode = "unknown"
        handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.notion.databases[CREATED_DATABASE_ID]["description"] = _rich_text(
            "foreign-marker"
        )
        result = handle_schema_command(
            self.event(3),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("RECOVERY_CONFLICT", result)
        self.assertIsNone(self.database.get_schema_installation(SCHEMA_LOGICAL_NAME))
        self.assertEqual(self.notion.create_count, 1)

    def test_recovery_rejects_exact_marker_created_after_fixed_attempt_window(self) -> None:
        proposal_id, revision, digest, _ = self.plan()
        self.notion.create_mode = "unknown"
        handle_schema_command(
            self.event(2),
            f"/nx_schema_approve {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        proposal = self.database.load_schema_proposal(proposal_id)
        journal = self.database.schema_apply_journal_state(proposal.operation_id)
        assert journal is not None
        finished = datetime.fromisoformat(str(journal["attempt_finished_at"]))
        late = (finished + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        self.notion.databases[CREATED_DATABASE_ID]["created_time"] = late
        self.notion.data_sources[CREATED_DATA_SOURCE_ID]["created_time"] = late

        result = handle_schema_command(
            self.event(3),
            f"/nx_schema_recover {proposal_id} {revision} {digest}",
            self.config,
            self.database,
        )
        self.assertIn("RECOVERY_CONFLICT", result)
        self.assertIsNone(self.database.get_schema_installation(SCHEMA_LOGICAL_NAME))
        self.assertEqual(self.notion.create_count, 1)

    def test_reject_is_owner_thread_bound_and_read_only(self) -> None:
        proposal_id, revision, _, _ = self.plan()
        denied = handle_schema_command(
            self.event(2, thread_id="wrong"),
            f"/nx_schema_reject {proposal_id} {revision}",
            self.config,
            self.database,
        )
        self.assertIn("DENIED", denied)
        rejected = handle_schema_command(
            self.event(3),
            f"/nx_schema_reject {proposal_id} {revision}",
            self.config,
            self.database,
        )
        self.assertIn("NX_SCHEMA_REJECTED", rejected)
        self.assertEqual(self.notion.create_count, 0)
        self.assertIs(
            self.database.load_schema_proposal(proposal_id).status,
            SchemaProposalStatus.REJECTED,
        )


if __name__ == "__main__":
    unittest.main()
