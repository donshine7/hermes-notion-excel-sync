from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from notion_excel_sync.cli import main
from notion_excel_sync.models import ChangeKind, ProposedChange, SourceRef
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.approval import ApprovalService
from notion_excel_sync.workflow.proposals import ProposalService


SECRET = "a-secure-test-secret-at-least-24-characters"


class CliContractTest(unittest.TestCase):
    def test_show_edit_and_direct_approval_guard(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "sync.json"
            config_path.write_text(
                json.dumps(
                    {
                        "initial_cutoff": "2026-07-16T00:00:00+09:00",
                        "state_db": "state.db",
                        "work_dir": "work",
                        "onedrive": {"drive_id": "drive", "item_id": "item"},
                        "excel": {"sheets": ["2026"], "key_columns": ["ID"]},
                        "notion": {
                            "databases": {"한국 특허 사건": "cases"}
                        },
                        "approval": {"allowed_telegram_users": ["123"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"GATEWAY_RELAY_NX_APPROVAL_SECRET": SECRET},
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        main(["init-db", "--config", str(config_path)]),
                        0,
                    )
                database = StateDatabase(root / "state.db")
                approval = ApprovalService(
                    SECRET,
                    {"123"},
                    nonce_used=database.nonce_used,
                    mark_nonce_used=database.mark_nonce_used,
                )
                proposals = ProposalService(database, approval)
                change = ProposedChange(
                    target_database="한국 특허 사건",
                    entity_key="A-1",
                    property_name="현재상태",
                    kind=ChangeKind.UPDATE,
                    current_value="접수",
                    proposed_value="착수",
                    analyzer="workflow_status",
                    analyzer_version="1.0.0",
                    confidence=1.0,
                    reason="CLI contract",
                    source_refs=[
                        SourceRef(
                            "drive",
                            "item",
                            "2.0",
                            "hash",
                            "2026",
                            2,
                            "B2",
                            "착수",
                        )
                    ],
                )
                proposal = proposals.create(
                    "2.0", "hash", "123", "chat", [change]
                )
                proposal = proposals.request_approval(proposal.proposal_id)

                shown = io.StringIO()
                with redirect_stdout(shown):
                    code = main(
                        [
                            "show",
                            "--config",
                            str(config_path),
                            "--proposal-id",
                            proposal.proposal_id,
                        ]
                    )
                self.assertEqual(code, 0)
                shown_json = json.loads(shown.getvalue())
                self.assertEqual(shown_json["proposal"]["revision"], 1)
                self.assertEqual(len(shown_json["operations"]), 1)

                prepare_rejected = io.StringIO()
                with redirect_stderr(prepare_rejected):
                    code = main(
                        [
                            "prepare",
                            "--config",
                            str(config_path),
                            "--telegram-user-id",
                            "123",
                            "--chat-id",
                            "chat",
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn(
                    "/nx_sync",
                    json.loads(prepare_rejected.getvalue())["message"],
                )

                edited = io.StringIO()
                with redirect_stderr(edited):
                    code = main(
                        [
                            "edit",
                            "--config",
                            str(config_path),
                            "--proposal-id",
                            proposal.proposal_id,
                            "--operation-id",
                            change.operation_id,
                            "--action",
                            "edit",
                            "--value-json",
                            '"완료"',
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn("/nx_set", json.loads(edited.getvalue())["message"])
                revised = database.load_proposal(proposal.proposal_id)
                self.assertEqual(revised.revision, 1)

                for command, gateway_command, extra in (
                    (
                        "reject",
                        "/nx_reject",
                        [
                            "--proposal-id",
                            proposal.proposal_id,
                            "--telegram-user-id",
                            "123",
                            "--chat-id",
                            "chat",
                        ],
                    ),
                    (
                        "recover",
                        "/nx_recover",
                        [
                            "--proposal-id",
                            proposal.proposal_id,
                            "--telegram-user-id",
                            "123",
                            "--chat-id",
                            "chat",
                        ],
                    ),
                ):
                    guarded = io.StringIO()
                    with redirect_stderr(guarded):
                        code = main(
                            [command, "--config", str(config_path), *extra]
                        )
                    self.assertEqual(code, 1)
                    self.assertIn(
                        gateway_command,
                        json.loads(guarded.getvalue())["message"],
                    )

                receipt_path = root / "receipt.json"
                rejected = io.StringIO()
                with redirect_stderr(rejected):
                    code = main(
                        [
                            "approve",
                            "--config",
                            str(config_path),
                            "--proposal-id",
                            revised.proposal_id,
                            "--revision",
                            str(revised.revision),
                            "--telegram-user-id",
                            "123",
                            "--chat-id",
                            "chat",
                            "--digest",
                            revised.digest,
                            "--output",
                            str(receipt_path),
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertFalse(receipt_path.exists())
                rejected_json = json.loads(rejected.getvalue())
                self.assertEqual(rejected_json["error"], "PermissionError")
                self.assertIn("/nx_approve", rejected_json["message"])

                apply_rejected = io.StringIO()
                with redirect_stderr(apply_rejected):
                    code = main(
                        [
                            "apply",
                            "--config",
                            str(config_path),
                            "--receipt-file",
                            str(receipt_path),
                        ]
                    )
                self.assertEqual(code, 1)
                apply_json = json.loads(apply_rejected.getvalue())
                self.assertEqual(apply_json["error"], "PermissionError")
                self.assertIn("trusted Hermes gateway", apply_json["message"])


if __name__ == "__main__":
    unittest.main()
