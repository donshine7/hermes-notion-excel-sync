from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from notion_excel_sync.models import (
    ApprovalReceipt,
    ChangeKind,
    KnowledgeRef,
    ProposalAction,
    ProposalOperation,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    SCHEMA_APPROVAL_PURPOSE,
    SCHEMA_RECOVERY_PURPOSE,
    SchemaApprovalReceipt,
    SchemaProposalRevision,
    SchemaProposalStatus,
    SchemaRecoveryReceipt,
    SourceRef,
    dataclass_to_dict,
    legacy_schema_recovery_command_hash,
    schema_approval_command_hash,
    schema_recovery_command_hash,
    sha256_json,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sync_checkpoint (
    drive_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    PRIMARY KEY (drive_id, item_id)
);

CREATE TABLE IF NOT EXISTS ingestion_checkpoint (
    stream_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal (
    proposal_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    requested_by TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    source_file_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS proposal_revision (
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (proposal_id, revision),
    FOREIGN KEY (proposal_id) REFERENCES proposal(proposal_id)
);

CREATE TABLE IF NOT EXISTS approval_receipt (
    nonce TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_change (
    operation_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
    operation_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_run (
    run_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    status TEXT NOT NULL,
    source_version_id TEXT,
    details TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    proposal_id TEXT,
    actor TEXT,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyzer_definition (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS analyzer_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mapping (
    database_name TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    notion_page_id TEXT NOT NULL,
    match_value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (database_name, entity_key)
);

CREATE TABLE IF NOT EXISTS source_apply_lease (
    drive_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    owner_token TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    PRIMARY KEY (drive_id, item_id),
    UNIQUE (owner_token)
);

CREATE TABLE IF NOT EXISTS schema_proposal (
    schema_proposal_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'notion_schema_create/v1'),
    requested_by TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    logical_database_name TEXT NOT NULL,
    parent_page_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_proposal_revision (
    schema_proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'notion_schema_create/v1'),
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (schema_proposal_id, revision),
    FOREIGN KEY (schema_proposal_id)
        REFERENCES schema_proposal(schema_proposal_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_schema_proposal_per_target
ON schema_proposal(logical_database_name)
WHERE status IN ('draft', 'pending_approval', 'approved', 'applying', 'unknown');

CREATE TABLE IF NOT EXISTS schema_approval_receipt (
    nonce TEXT PRIMARY KEY,
    schema_proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'notion_schema_create/v1'),
    telegram_update_id INTEGER NOT NULL,
    telegram_message_id TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (schema_proposal_id, revision)
        REFERENCES schema_proposal_revision(schema_proposal_id, revision)
);

CREATE TABLE IF NOT EXISTS schema_recovery_receipt (
    nonce TEXT PRIMARY KEY,
    schema_proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'notion_schema_recovery/v1'),
    telegram_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    telegram_update_id INTEGER NOT NULL,
    telegram_message_id TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    approval_receipt_nonce TEXT NOT NULL,
    conflict_telegram_update_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (purpose, telegram_update_id),
    UNIQUE (purpose, chat_id, telegram_message_id),
    FOREIGN KEY (schema_proposal_id, revision)
        REFERENCES schema_proposal_revision(schema_proposal_id, revision),
    FOREIGN KEY (approval_receipt_nonce)
        REFERENCES schema_approval_receipt(nonce)
);

CREATE TABLE IF NOT EXISTS schema_recovery_event_claim (
    purpose TEXT NOT NULL CHECK (purpose = 'notion_schema_recovery/v1'),
    telegram_update_id INTEGER NOT NULL,
    telegram_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    telegram_message_id TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    schema_proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    proposal_digest TEXT NOT NULL,
    receipt_nonce TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    safe_result TEXT,
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (purpose, telegram_update_id),
    UNIQUE (purpose, chat_id, telegram_message_id),
    FOREIGN KEY (schema_proposal_id, revision)
        REFERENCES schema_proposal_revision(schema_proposal_id, revision),
    FOREIGN KEY (receipt_nonce) REFERENCES schema_recovery_receipt(nonce),
    CHECK (status IN ('pending', 'completed'))
);

CREATE TABLE IF NOT EXISTS schema_gateway_event_claim (
    purpose TEXT NOT NULL CHECK (purpose = 'notion_schema_create/v1'),
    telegram_update_id INTEGER NOT NULL,
    telegram_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    telegram_message_id TEXT NOT NULL,
    command_name TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    schema_proposal_id TEXT,
    receipt_nonce TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    safe_result TEXT,
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (purpose, telegram_update_id),
    UNIQUE (purpose, chat_id, telegram_message_id),
    FOREIGN KEY (receipt_nonce) REFERENCES schema_approval_receipt(nonce),
    CHECK (status IN ('pending', 'completed'))
);

CREATE TABLE IF NOT EXISTS schema_apply_journal (
    operation_id TEXT PRIMARY KEY,
    schema_proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'notion_schema_create/v1'),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    remote_database_id TEXT,
    remote_data_source_id TEXT,
    attempt_started_at TEXT,
    attempt_finished_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (schema_proposal_id, revision)
        REFERENCES schema_proposal_revision(schema_proposal_id, revision)
);

CREATE TABLE IF NOT EXISTS notion_schema_installation (
    logical_database_name TEXT PRIMARY KEY,
    database_id TEXT NOT NULL UNIQUE,
    data_source_id TEXT NOT NULL UNIQUE,
    parent_page_id TEXT NOT NULL,
    schema_digest TEXT NOT NULL,
    schema_proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    installed_at TEXT NOT NULL,
    FOREIGN KEY (schema_proposal_id, revision)
        REFERENCES schema_proposal_revision(schema_proposal_id, revision)
);
"""


class ApplyLeaseError(RuntimeError):
    """Raised when another worker owns the source-scoped remote apply lease."""


class SchemaStateError(RuntimeError):
    """Raised when a schema workflow state transition is unsafe."""


class SchemaEventClaimError(SchemaStateError):
    """Raised when a Telegram update/message is replayed with changed content."""


def _safe_schema_reason_code(value: str) -> str:
    normalized = str(value).strip().lower()
    if (
        not normalized
        or len(normalized) > 64
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in normalized)
    ):
        raise SchemaStateError("Schema transition reason code is invalid")
    return normalized


def _process_is_alive(pid: int) -> bool:
    """Fail closed when checking whether a persisted lease owner still exists."""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        ctypes.set_last_error(0)
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            # Access denied is treated as alive so a lease is never stolen from
            # a process that merely cannot be inspected.
            return ctypes.get_last_error() == 5
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        return True
    return True


class StateDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.session() as connection:
            connection.executescript(SCHEMA)
            event_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(schema_gateway_event_claim)"
                ).fetchall()
            }
            if "telegram_user_id" not in event_columns:
                connection.execute(
                    "ALTER TABLE schema_gateway_event_claim ADD COLUMN "
                    "telegram_user_id TEXT NOT NULL DEFAULT ''"
                )
            journal_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(schema_apply_journal)"
                ).fetchall()
            }
            if "attempt_started_at" not in journal_columns:
                connection.execute(
                    "ALTER TABLE schema_apply_journal ADD COLUMN attempt_started_at TEXT"
                )
            if "attempt_finished_at" not in journal_columns:
                connection.execute(
                    "ALTER TABLE schema_apply_journal ADD COLUMN attempt_finished_at TEXT"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def acquire_apply_lease(
        self,
        *,
        drive_id: str,
        item_id: str,
        proposal_id: str,
        revision: int,
    ) -> str:
        """Atomically acquire the exclusive remote-apply lease for one source.

        A live owner is never pre-empted. After a process crash, only the same
        proposal may reclaim an unfinished lease; a different proposal must
        wait for the unfinished APPLYING proposal to be reconciled first.
        """

        owner_token = secrets.token_urlsafe(32)
        owner_pid = os.getpid()
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM source_apply_lease WHERE drive_id = ? AND item_id = ?",
                (drive_id, item_id),
            ).fetchone()
            if row is not None:
                if drive_id == SCHEMA_APPROVAL_PURPOSE:
                    holder_status_row = connection.execute(
                        "SELECT status FROM schema_proposal "
                        "WHERE schema_proposal_id = ?",
                        (str(row["proposal_id"]),),
                    ).fetchone()
                else:
                    holder_status_row = connection.execute(
                        "SELECT status FROM proposal WHERE proposal_id = ?",
                        (str(row["proposal_id"]),),
                    ).fetchone()
                holder_status = (
                    str(holder_status_row["status"])
                    if holder_status_row is not None
                    else "UNKNOWN"
                )
                owner_alive = _process_is_alive(int(row["owner_pid"]))
                same_proposal = (
                    str(row["proposal_id"]) == proposal_id
                    and int(row["revision"]) == revision
                )
                terminal_holder = holder_status in {
                    ProposalStatus.COMMITTED.value,
                    ProposalStatus.FAILED.value,
                    ProposalStatus.STALE.value,
                    ProposalStatus.REJECTED.value,
                    ProposalStatus.EXPIRED.value,
                    SchemaProposalStatus.COMMITTED.value,
                    SchemaProposalStatus.FAILED.value,
                    SchemaProposalStatus.STALE.value,
                    SchemaProposalStatus.REJECTED.value,
                    SchemaProposalStatus.EXPIRED.value,
                }
                if owner_alive or (not same_proposal and not terminal_holder):
                    raise ApplyLeaseError(
                        "Another approved synchronization owns this OneDrive "
                        f"source (proposal {row['proposal_id']}/{row['revision']})"
                    )
                connection.execute(
                    "DELETE FROM source_apply_lease WHERE drive_id = ? AND item_id = ?",
                    (drive_id, item_id),
                )
            connection.execute(
                "INSERT INTO source_apply_lease("
                "drive_id, item_id, proposal_id, revision, owner_token, owner_pid, "
                "acquired_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    drive_id,
                    item_id,
                    proposal_id,
                    revision,
                    owner_token,
                    owner_pid,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    "source_apply_lease_acquired",
                    proposal_id,
                    None,
                    json.dumps(
                        {
                            "drive_id": drive_id,
                            "item_id": item_id,
                            "revision": revision,
                            "owner_pid": owner_pid,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        return owner_token

    def heartbeat_apply_lease(self, owner_token: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE source_apply_lease SET heartbeat_at = ? "
                "WHERE owner_token = ? AND owner_pid = ?",
                (now, owner_token, os.getpid()),
            )
        if cursor.rowcount != 1:
            raise ApplyLeaseError("The source apply lease was lost")

    def release_apply_lease(self, owner_token: str) -> None:
        with self.session() as connection:
            connection.execute(
                "DELETE FROM source_apply_lease "
                "WHERE owner_token = ? AND owner_pid = ?",
                (owner_token, os.getpid()),
            )

    @contextmanager
    def apply_lease(
        self,
        *,
        drive_id: str,
        item_id: str,
        proposal_id: str,
        revision: int,
    ) -> Iterator[str]:
        token = self.acquire_apply_lease(
            drive_id=drive_id,
            item_id=item_id,
            proposal_id=proposal_id,
            revision=revision,
        )
        try:
            yield token
        finally:
            self.release_apply_lease(token)

    def audit(
        self,
        event_type: str,
        details: dict[str, Any],
        proposal_id: str | None = None,
        actor: str | None = None,
    ) -> None:
        with self.session() as connection:
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event_type,
                    proposal_id,
                    actor,
                    json.dumps(details, ensure_ascii=False, default=str),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def get_checkpoint(self, drive_id: str, item_id: str) -> dict[str, str] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM sync_checkpoint WHERE drive_id = ? AND item_id = ?",
                (drive_id, item_id),
            ).fetchone()
        return dict(row) if row else None

    def get_ingestion_checkpoint(self, stream_key: str) -> dict[str, Any] | None:
        if not stream_key.strip():
            raise ValueError("stream_key must not be empty")
        with self.session() as connection:
            row = connection.execute(
                "SELECT payload FROM ingestion_checkpoint WHERE stream_key = ?",
                (stream_key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            raise RuntimeError("Ingestion checkpoint payload is invalid")
        return payload

    def save_ingestion_checkpoint(
        self,
        stream_key: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> None:
        if not stream_key.strip():
            raise ValueError("stream_key must not be empty")
        now = datetime.now().astimezone().isoformat()
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO ingestion_checkpoint(stream_key, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(stream_key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (stream_key, serialized, now),
            )
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, NULL, ?, ?, ?)",
                (
                    "ingestion_checkpoint_saved",
                    actor,
                    json.dumps(
                        {"stream_key": stream_key, "payload_hash": sha256_json(payload)},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    def initialize_ingestion_checkpoint(
        self,
        stream_key: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Create a command-time baseline exactly once and return the winner."""

        if not stream_key.strip():
            raise ValueError("stream_key must not be empty")
        now = datetime.now().astimezone().isoformat()
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ingestion_checkpoint("
                "stream_key, payload, updated_at"
                ") VALUES (?, ?, ?)",
                (stream_key, serialized, now),
            )
            row = connection.execute(
                "SELECT payload FROM ingestion_checkpoint WHERE stream_key = ?",
                (stream_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Ingestion checkpoint could not be initialized")
            winner = json.loads(str(row["payload"]))
            if not isinstance(winner, dict):
                raise RuntimeError("Ingestion checkpoint payload is invalid")
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, NULL, ?, ?, ?)",
                (
                    "ingestion_checkpoint_initialized",
                    actor,
                    json.dumps(
                        {
                            "stream_key": stream_key,
                            "payload_hash": sha256_json(winner),
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        return winner

    def commit_checkpoint(
        self,
        drive_id: str,
        item_id: str,
        version_id: str,
        file_hash: str,
        proposal_id: str,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO sync_checkpoint(
                    drive_id, item_id, version_id, file_hash, committed_at, proposal_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(drive_id, item_id) DO UPDATE SET
                    version_id = excluded.version_id,
                    file_hash = excluded.file_hash,
                    committed_at = excluded.committed_at,
                    proposal_id = excluded.proposal_id
                """,
                (drive_id, item_id, version_id, file_hash, now, proposal_id),
            )

    def commit_noop_checkpoint(
        self,
        *,
        drive_id: str,
        item_id: str,
        version_id: str,
        file_hash: str,
        actor: str,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        run_id = f"NOOP-{version_id}"
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sync_checkpoint(
                    drive_id, item_id, version_id, file_hash, committed_at, proposal_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(drive_id, item_id) DO UPDATE SET
                    version_id = excluded.version_id,
                    file_hash = excluded.file_hash,
                    committed_at = excluded.committed_at,
                    proposal_id = excluded.proposal_id
                """,
                (drive_id, item_id, version_id, file_hash, now, run_id),
            )
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    "noop_checkpoint_committed",
                    None,
                    actor,
                    json.dumps(
                        {"version_id": version_id, "file_hash": file_hash},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    def save_proposal(self, proposal: ProposalRevision) -> None:
        payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO proposal(
                    proposal_id, current_revision, requested_by, chat_id,
                    source_version_id, source_file_hash, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    current_revision = excluded.current_revision,
                    source_version_id = excluded.source_version_id,
                    source_file_hash = excluded.source_file_hash,
                    status = excluded.status,
                    expires_at = excluded.expires_at
                """,
                (
                    proposal.proposal_id,
                    proposal.revision,
                    proposal.requested_by,
                    proposal.chat_id,
                    proposal.source_version_id,
                    proposal.source_file_hash,
                    proposal.status.value,
                    proposal.created_at.isoformat(),
                    proposal.expires_at.isoformat() if proposal.expires_at else None,
                ),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO proposal_revision(
                    proposal_id, revision, digest, payload, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.revision,
                    proposal.digest,
                    payload,
                    proposal.created_at.isoformat(),
                ),
            )

    def load_proposal(self, proposal_id: str, revision: int | None = None) -> ProposalRevision:
        with self.session() as connection:
            if revision is None:
                head = connection.execute(
                    "SELECT current_revision FROM proposal WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if not head:
                    raise KeyError(f"Proposal not found: {proposal_id}")
                revision = int(head["current_revision"])
            row = connection.execute(
                "SELECT payload FROM proposal_revision WHERE proposal_id = ? AND revision = ?",
                (proposal_id, revision),
            ).fetchone()
        if not row:
            raise KeyError(f"Proposal revision not found: {proposal_id}/{revision}")
        return _proposal_from_dict(json.loads(row["payload"]))

    def update_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        with self.session() as connection:
            connection.execute(
                "UPDATE proposal SET status = ? WHERE proposal_id = ?",
                (status.value, proposal_id),
            )
            row = connection.execute(
                "SELECT current_revision FROM proposal WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row:
                revision = self.load_proposal(proposal_id, int(row["current_revision"]))
                revision.status = status
                connection.execute(
                    "UPDATE proposal_revision SET payload = ? "
                    "WHERE proposal_id = ? AND revision = ?",
                    (
                        json.dumps(dataclass_to_dict(revision), ensure_ascii=False),
                        proposal_id,
                        revision.revision,
                    ),
                )

    def save_receipt(self, receipt: Any) -> None:
        payload = json.dumps(dataclass_to_dict(receipt), ensure_ascii=False)
        with self.session() as connection:
            connection.execute(
                "INSERT INTO approval_receipt(nonce, proposal_id, revision, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    receipt.nonce,
                    receipt.proposal_id,
                    receipt.revision,
                    payload,
                    receipt.issued_at.isoformat(),
                ),
            )

    def load_latest_receipt(
        self, proposal_id: str, revision: int
    ) -> ApprovalReceipt:
        with self.session() as connection:
            row = connection.execute(
                "SELECT payload FROM approval_receipt WHERE proposal_id = ? "
                "AND revision = ? ORDER BY created_at DESC LIMIT 1",
                (proposal_id, revision),
            ).fetchone()
        if not row:
            raise KeyError(f"Approval receipt not found: {proposal_id}/{revision}")
        data = json.loads(row["payload"])
        return ApprovalReceipt(
            proposal_id=data["proposal_id"],
            revision=int(data["revision"]),
            proposal_digest=data["proposal_digest"],
            source_version_id=data["source_version_id"],
            source_file_hash=data["source_file_hash"],
            telegram_user_id=str(data["telegram_user_id"]),
            chat_id=str(data["chat_id"]),
            issued_at=datetime.fromisoformat(data["issued_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            nonce=data["nonce"],
            signature=data["signature"],
            notion_binding_digest=data.get("notion_binding_digest", ""),
        )

    def nonce_used(self, nonce: str) -> bool:
        with self.session() as connection:
            row = connection.execute(
                "SELECT used_at FROM approval_receipt WHERE nonce = ?", (nonce,)
            ).fetchone()
        return bool(row and row["used_at"])

    def mark_nonce_used(self, nonce: str) -> None:
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE approval_receipt SET used_at = ? WHERE nonce = ? AND used_at IS NULL",
                (datetime.now().astimezone().isoformat(), nonce),
            )
        if cursor.rowcount != 1:
            raise ValueError("Approval nonce is missing or already used")

    def begin_apply(self, proposal: ProposalRevision, nonce: str) -> None:
        """Atomically consume approval, enter APPLYING, and create the outbox."""

        now = datetime.now().astimezone().isoformat()
        payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        with self.transaction() as connection:
            nonce_cursor = connection.execute(
                "UPDATE approval_receipt SET used_at = ? "
                "WHERE nonce = ? AND used_at IS NULL",
                (now, nonce),
            )
            proposal_cursor = connection.execute(
                "UPDATE proposal SET status = ?, expires_at = ? "
                "WHERE proposal_id = ? AND current_revision = ? AND status = ?",
                (
                    proposal.status.value,
                    proposal.expires_at.isoformat() if proposal.expires_at else None,
                    proposal.proposal_id,
                    proposal.revision,
                    ProposalStatus.APPROVED.value,
                ),
            )
            revision_cursor = connection.execute(
                "UPDATE proposal_revision SET payload = ?, digest = ? "
                "WHERE proposal_id = ? AND revision = ?",
                (
                    payload,
                    proposal.digest,
                    proposal.proposal_id,
                    proposal.revision,
                ),
            )
            if (
                nonce_cursor.rowcount != 1
                or proposal_cursor.rowcount != 1
                or revision_cursor.rowcount != 1
            ):
                raise RuntimeError(
                    "Approval or proposal state changed before apply could start"
                )
            for operation in proposal.operations:
                if operation.action not in {
                    ProposalAction.APPLY,
                    ProposalAction.EDIT,
                }:
                    continue
                operation_payload = json.dumps(
                    dataclass_to_dict(operation),
                    ensure_ascii=False,
                    default=str,
                )
                connection.execute(
                    """
                    INSERT INTO outbox(
                        operation_id, proposal_id, revision, payload, status, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?)
                    ON CONFLICT(operation_id) DO UPDATE SET
                        proposal_id = excluded.proposal_id,
                        revision = excluded.revision,
                        payload = excluded.payload,
                        status = 'pending',
                        last_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        operation.change.operation_id,
                        proposal.proposal_id,
                        proposal.revision,
                        operation_payload,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    "apply_started",
                    proposal.proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "revision": proposal.revision,
                            "nonce": nonce,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    def enqueue_operations(self, proposal: ProposalRevision) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            for operation in proposal.operations:
                payload = json.dumps(
                    dataclass_to_dict(operation),
                    ensure_ascii=False,
                    default=str,
                )
                if operation.action is ProposalAction.DEFER:
                    connection.execute(
                        "INSERT OR REPLACE INTO pending_change("
                        "operation_id, proposal_id, payload, created_at, resolved_at"
                        ") VALUES (?, ?, ?, ?, NULL)",
                        (operation.change.operation_id, proposal.proposal_id, payload, now),
                    )
                    continue
                if operation.action in {ProposalAction.APPLY, ProposalAction.EDIT}:
                    connection.execute(
                        """
                        INSERT INTO outbox(
                            operation_id, proposal_id, revision, payload, status, updated_at
                        ) VALUES (?, ?, ?, ?, 'pending', ?)
                        ON CONFLICT(operation_id) DO UPDATE SET
                            proposal_id = excluded.proposal_id,
                            revision = excluded.revision,
                            payload = excluded.payload,
                            status = 'pending',
                            last_error = NULL,
                            updated_at = excluded.updated_at
                        """,
                        (
                            operation.change.operation_id,
                            proposal.proposal_id,
                            proposal.revision,
                            payload,
                            now,
                        ),
                    )

    def load_pending_operations(self) -> list[ProposalOperation]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT payload FROM pending_change WHERE resolved_at IS NULL "
                "ORDER BY created_at, operation_id"
            ).fetchall()
        return [_operation_from_dict(json.loads(row["payload"])) for row in rows]

    def pending_outbox(self, proposal_id: str) -> list[sqlite3.Row]:
        with self.session() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM outbox WHERE proposal_id = ? AND status != 'done' "
                    "ORDER BY operation_id",
                    (proposal_id,),
                ).fetchall()
            )

    def outbox_status(
        self, proposal_id: str, revision: int, operation_id: str
    ) -> str | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE proposal_id = ? "
                "AND revision = ? AND operation_id = ?",
                (proposal_id, revision, operation_id),
            ).fetchone()
        return str(row["status"]) if row else None

    def operation_outbox_state(self, operation_id: str) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT proposal_id, revision, status, attempts, last_error, updated_at "
                "FROM outbox WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return dict(row) if row else None

    def reset_outbox_for_resume(
        self,
        proposal_id: str,
        revision: int,
        operation_ids: list[str],
    ) -> None:
        if not operation_ids:
            return
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            for operation_id in operation_ids:
                cursor = connection.execute(
                    "UPDATE outbox SET status = 'pending', last_error = NULL, "
                    "updated_at = ? WHERE proposal_id = ? AND revision = ? "
                    "AND operation_id = ? AND status != 'done'",
                    (now, proposal_id, revision, operation_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Outbox operation is not resumable: {operation_id}"
                    )

    def mark_outbox(
        self, operation_id: str, status: str, error: str | None = None
    ) -> None:
        with self.session() as connection:
            connection.execute(
                """
                UPDATE outbox SET status = ?, attempts = attempts + 1,
                    last_error = ?, updated_at = ? WHERE operation_id = ?
                """,
                (
                    status,
                    error,
                    datetime.now().astimezone().isoformat(),
                    operation_id,
                ),
            )

    def get_entity_mapping(self, database_name: str, entity_key: str) -> dict[str, str] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM entity_mapping WHERE database_name = ? AND entity_key = ?",
                (database_name, entity_key),
            ).fetchone()
        return dict(row) if row else None

    def save_entity_mapping(
        self,
        database_name: str,
        entity_key: str,
        notion_page_id: str,
        match_value: str,
    ) -> None:
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO entity_mapping(
                    database_name, entity_key, notion_page_id, match_value, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(database_name, entity_key) DO UPDATE SET
                    notion_page_id = excluded.notion_page_id,
                    match_value = excluded.match_value,
                    updated_at = excluded.updated_at
                """,
                (
                    database_name,
                    entity_key,
                    notion_page_id,
                    match_value,
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def save_schema_proposal(self, proposal: SchemaProposalRevision) -> None:
        """Persist one immutable schema revision and its mutable lifecycle state."""

        if proposal.purpose != SCHEMA_APPROVAL_PURPOSE:
            raise SchemaStateError("Unsupported schema proposal purpose")
        payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        immutable = (
            proposal.purpose,
            proposal.requested_by,
            proposal.chat_id,
            proposal.thread_id,
            proposal.operation_id,
            proposal.logical_database_name,
            proposal.parent_page_id,
        )
        with self.transaction() as connection:
            head = connection.execute(
                "SELECT * FROM schema_proposal WHERE schema_proposal_id = ?",
                (proposal.schema_proposal_id,),
            ).fetchone()
            if head is None:
                if proposal.revision != 1:
                    raise SchemaStateError("A new schema proposal must start at revision 1")
                if proposal.status not in {
                    SchemaProposalStatus.DRAFT,
                    SchemaProposalStatus.PENDING_APPROVAL,
                }:
                    raise SchemaStateError(
                        "A new schema proposal must start as draft or pending approval"
                    )
                if proposal.status in {
                    SchemaProposalStatus.DRAFT,
                    SchemaProposalStatus.PENDING_APPROVAL,
                    SchemaProposalStatus.APPROVED,
                    SchemaProposalStatus.APPLYING,
                    SchemaProposalStatus.UNKNOWN,
                }:
                    active = connection.execute(
                        "SELECT schema_proposal_id FROM schema_proposal "
                        "WHERE logical_database_name = ? "
                        "AND status IN (?, ?, ?, ?, ?) LIMIT 1",
                        (
                            proposal.logical_database_name,
                            SchemaProposalStatus.DRAFT.value,
                            SchemaProposalStatus.PENDING_APPROVAL.value,
                            SchemaProposalStatus.APPROVED.value,
                            SchemaProposalStatus.APPLYING.value,
                            SchemaProposalStatus.UNKNOWN.value,
                        ),
                    ).fetchone()
                    if active is not None:
                        raise SchemaStateError(
                            "An unresolved schema proposal already owns this target"
                        )
                connection.execute(
                    """
                    INSERT INTO schema_proposal(
                        schema_proposal_id, current_revision, purpose,
                        requested_by, chat_id, thread_id, operation_id,
                        logical_database_name, parent_page_id, status,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.schema_proposal_id,
                        proposal.revision,
                        proposal.purpose,
                        proposal.requested_by,
                        proposal.chat_id,
                        proposal.thread_id,
                        proposal.operation_id,
                        proposal.logical_database_name,
                        proposal.parent_page_id,
                        proposal.status.value,
                        proposal.created_at.isoformat(),
                        proposal.expires_at.isoformat() if proposal.expires_at else None,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO schema_proposal_revision(
                        schema_proposal_id, revision, purpose, digest,
                        payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.schema_proposal_id,
                        proposal.revision,
                        proposal.purpose,
                        proposal.digest,
                        payload,
                        proposal.created_at.isoformat(),
                    ),
                )
                return

            persisted_immutable = (
                str(head["purpose"]),
                str(head["requested_by"]),
                str(head["chat_id"]),
                str(head["thread_id"]),
                str(head["operation_id"]),
                str(head["logical_database_name"]),
                str(head["parent_page_id"]),
            )
            if persisted_immutable != immutable:
                raise SchemaStateError("Schema proposal routing or target is immutable")
            current_revision = int(head["current_revision"])
            if proposal.revision not in {current_revision, current_revision + 1}:
                raise SchemaStateError("Schema proposal revisions must advance exactly once")

            if proposal.revision == current_revision:
                stored = connection.execute(
                    "SELECT digest, payload FROM schema_proposal_revision "
                    "WHERE schema_proposal_id = ? AND revision = ?",
                    (proposal.schema_proposal_id, proposal.revision),
                ).fetchone()
                if stored is None or str(stored["digest"]) != proposal.digest:
                    raise SchemaStateError("An existing schema revision is immutable")
                previous = _schema_proposal_from_dict(json.loads(stored["payload"]))
                allowed_same_revision_transitions = {
                    SchemaProposalStatus.DRAFT: {
                        SchemaProposalStatus.DRAFT,
                        SchemaProposalStatus.PENDING_APPROVAL,
                        SchemaProposalStatus.REJECTED,
                    },
                    SchemaProposalStatus.PENDING_APPROVAL: {
                        SchemaProposalStatus.PENDING_APPROVAL,
                        SchemaProposalStatus.REJECTED,
                        SchemaProposalStatus.EXPIRED,
                        SchemaProposalStatus.STALE,
                    },
                    SchemaProposalStatus.FAILED: {
                        SchemaProposalStatus.FAILED,
                        SchemaProposalStatus.REJECTED,
                    },
                    SchemaProposalStatus.STALE: {
                        SchemaProposalStatus.STALE,
                        SchemaProposalStatus.REJECTED,
                    },
                }
                allowed = allowed_same_revision_transitions.get(
                    previous.status,
                    {previous.status},
                )
                if proposal.status not in allowed:
                    raise SchemaStateError(
                        "Schema proposal status requires its dedicated atomic transition"
                    )
                if proposal.created_at != previous.created_at:
                    raise SchemaStateError("Schema proposal creation time is immutable")
                if not (
                    previous.status is SchemaProposalStatus.DRAFT
                    and proposal.status is SchemaProposalStatus.PENDING_APPROVAL
                ) and proposal.expires_at != previous.expires_at:
                    raise SchemaStateError("Schema proposal expiry is immutable")
                connection.execute(
                    "UPDATE schema_proposal_revision SET payload = ? "
                    "WHERE schema_proposal_id = ? AND revision = ?",
                    (payload, proposal.schema_proposal_id, proposal.revision),
                )
            else:
                if str(head["status"]) not in {
                    SchemaProposalStatus.DRAFT.value,
                    SchemaProposalStatus.PENDING_APPROVAL.value,
                    SchemaProposalStatus.STALE.value,
                    SchemaProposalStatus.FAILED.value,
                }:
                    raise SchemaStateError(
                        "A finalized schema proposal cannot create another revision"
                    )
                if proposal.status not in {
                    SchemaProposalStatus.DRAFT,
                    SchemaProposalStatus.PENDING_APPROVAL,
                }:
                    raise SchemaStateError(
                        "A new schema revision must start as draft or pending approval"
                    )
                connection.execute(
                    """
                    INSERT INTO schema_proposal_revision(
                        schema_proposal_id, revision, purpose, digest,
                        payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.schema_proposal_id,
                        proposal.revision,
                        proposal.purpose,
                        proposal.digest,
                        payload,
                        proposal.created_at.isoformat(),
                    ),
                )

            cursor = connection.execute(
                "UPDATE schema_proposal SET current_revision = ?, status = ?, "
                "expires_at = ? WHERE schema_proposal_id = ? "
                "AND current_revision = ?",
                (
                    proposal.revision,
                    proposal.status.value,
                    proposal.expires_at.isoformat() if proposal.expires_at else None,
                    proposal.schema_proposal_id,
                    current_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise SchemaStateError("Schema proposal head changed concurrently")

    def load_schema_proposal(
        self,
        schema_proposal_id: str,
        revision: int | None = None,
    ) -> SchemaProposalRevision:
        with self.session() as connection:
            if revision is None:
                head = connection.execute(
                    "SELECT current_revision FROM schema_proposal "
                    "WHERE schema_proposal_id = ?",
                    (schema_proposal_id,),
                ).fetchone()
                if head is None:
                    raise KeyError(f"Schema proposal not found: {schema_proposal_id}")
                revision = int(head["current_revision"])
            row = connection.execute(
                "SELECT payload FROM schema_proposal_revision "
                "WHERE schema_proposal_id = ? AND revision = ?",
                (schema_proposal_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError(
                f"Schema proposal revision not found: {schema_proposal_id}/{revision}"
            )
        return _schema_proposal_from_dict(json.loads(row["payload"]))

    def load_active_schema_proposal(
        self,
        logical_database_name: str,
    ) -> SchemaProposalRevision | None:
        """Return the sole unresolved proposal for a logical Notion target."""

        with self.session() as connection:
            rows = connection.execute(
                "SELECT r.payload FROM schema_proposal AS p "
                "JOIN schema_proposal_revision AS r "
                "ON r.schema_proposal_id = p.schema_proposal_id "
                "AND r.revision = p.current_revision "
                "WHERE p.logical_database_name = ? "
                "AND p.status IN (?, ?, ?, ?, ?) "
                "ORDER BY p.created_at, p.schema_proposal_id",
                (
                    logical_database_name,
                    SchemaProposalStatus.DRAFT.value,
                    SchemaProposalStatus.PENDING_APPROVAL.value,
                    SchemaProposalStatus.APPROVED.value,
                    SchemaProposalStatus.APPLYING.value,
                    SchemaProposalStatus.UNKNOWN.value,
                ),
            ).fetchall()
        if len(rows) > 1:
            raise SchemaStateError(
                "Multiple active schema proposals exist for one logical target"
            )
        return (
            _schema_proposal_from_dict(json.loads(rows[0]["payload"]))
            if rows
            else None
        )

    def claim_schema_gateway_event(
        self,
        *,
        command_name: str,
        telegram_user_id: str,
        chat_id: str,
        message_id: str,
        update_id: int,
        command_hash: str,
        schema_proposal_id: str | None = None,
        thread_id: str = "",
    ) -> dict[str, Any]:
        """Durably claim any reserved schema command before executing it.

        Exact duplicate delivery returns the existing pending/completed claim.
        Reusing either Telegram identifier with changed canonical content raises
        :class:`SchemaEventClaimError`.
        """

        command = _normalize_schema_command_name(command_name)
        _validate_schema_gateway_event(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
            command_hash=command_hash,
        )
        proposal_id = str(schema_proposal_id) if schema_proposal_id else None
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            by_update, by_message = _load_schema_gateway_claim_rows(
                connection,
                chat_id=chat_id,
                message_id=message_id,
                update_id=update_id,
            )
            if by_update is not None or by_message is not None:
                claim = _exact_schema_event_claim(
                    by_update,
                    by_message,
                    command_name=command,
                    schema_proposal_id=proposal_id,
                    telegram_user_id=telegram_user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    telegram_message_id=message_id,
                    telegram_update_id=update_id,
                    command_hash=command_hash,
                )
                return dict(claim)
            connection.execute(
                """
                INSERT INTO schema_gateway_event_claim(
                    purpose, telegram_update_id, telegram_user_id, chat_id, thread_id,
                    telegram_message_id, command_name, command_hash,
                    schema_proposal_id, status, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    SCHEMA_APPROVAL_PURPOSE,
                    update_id,
                    telegram_user_id,
                    chat_id,
                    thread_id,
                    message_id,
                    command,
                    command_hash,
                    proposal_id,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM schema_gateway_event_claim "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_APPROVAL_PURPOSE, update_id),
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_schema_gateway_event(
        self,
        *,
        command_name: str,
        telegram_user_id: str,
        chat_id: str,
        message_id: str,
        update_id: int,
        command_hash: str,
        schema_proposal_id: str | None = None,
        thread_id: str = "",
    ) -> dict[str, Any] | None:
        command = _normalize_schema_command_name(command_name)
        _validate_schema_gateway_event(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
            command_hash=command_hash,
        )
        proposal_id = str(schema_proposal_id) if schema_proposal_id else None
        with self.session() as connection:
            by_update, by_message = _load_schema_gateway_claim_rows(
                connection,
                chat_id=chat_id,
                message_id=message_id,
                update_id=update_id,
            )
            if by_update is None and by_message is None:
                return None
            claim = _exact_schema_event_claim(
                by_update,
                by_message,
                command_name=command,
                schema_proposal_id=proposal_id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                telegram_message_id=message_id,
                telegram_update_id=update_id,
                command_hash=command_hash,
            )
            return dict(claim)

    def complete_schema_gateway_event(
        self,
        *,
        command_name: str,
        telegram_user_id: str,
        chat_id: str,
        message_id: str,
        update_id: int,
        command_hash: str,
        safe_result: str,
        schema_proposal_id: str | None = None,
        thread_id: str = "",
    ) -> dict[str, Any]:
        """Store a bounded redelivery result for an exactly claimed event."""

        if not isinstance(safe_result, str) or len(safe_result) > 65_536:
            raise SchemaStateError("Schema gateway result must be at most 65536 chars")
        command = _normalize_schema_command_name(command_name)
        _validate_schema_gateway_event(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
            command_hash=command_hash,
        )
        proposal_id = str(schema_proposal_id) if schema_proposal_id else None
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            by_update, by_message = _load_schema_gateway_claim_rows(
                connection,
                chat_id=chat_id,
                message_id=message_id,
                update_id=update_id,
            )
            bound_proposal_id = (
                proposal_id
                if by_update is not None and by_update["schema_proposal_id"] is not None
                else None
            )
            claim = _exact_schema_event_claim(
                by_update,
                by_message,
                command_name=command,
                schema_proposal_id=bound_proposal_id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                telegram_message_id=message_id,
                telegram_update_id=update_id,
                command_hash=command_hash,
            )
            if str(claim["status"]) == "completed":
                return dict(claim)
            cursor = connection.execute(
                "UPDATE schema_gateway_event_claim SET status = 'completed', "
                "safe_result = ?, completed_at = ?, "
                "schema_proposal_id = COALESCE(schema_proposal_id, ?) "
                "WHERE purpose = ? AND telegram_update_id = ? "
                "AND status = 'pending' AND command_hash = ?",
                (
                    safe_result,
                    now,
                    proposal_id,
                    SCHEMA_APPROVAL_PURPOSE,
                    update_id,
                    command_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise SchemaEventClaimError(
                    "Schema gateway event changed before result completion"
                )
            row = connection.execute(
                "SELECT * FROM schema_gateway_event_claim "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_APPROVAL_PURPOSE, update_id),
            ).fetchone()
            assert row is not None
            return dict(row)

    def load_schema_receipt_for_event(
        self,
        *,
        schema_proposal_id: str,
        revision: int,
        proposal_digest: str,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
    ) -> SchemaApprovalReceipt | None:
        """Return an exact replay receipt, rejecting changed event content."""

        command_hash = schema_approval_command_hash(
            schema_proposal_id,
            revision,
            proposal_digest,
        )
        with self.session() as connection:
            by_update = connection.execute(
                "SELECT * FROM schema_gateway_event_claim "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_APPROVAL_PURPOSE, telegram_update_id),
            ).fetchone()
            by_message = connection.execute(
                "SELECT * FROM schema_gateway_event_claim "
                "WHERE purpose = ? AND chat_id = ? AND telegram_message_id = ?",
                (SCHEMA_APPROVAL_PURPOSE, chat_id, telegram_message_id),
            ).fetchone()
            if by_update is None and by_message is None:
                return None
            claim = _exact_schema_event_claim(
                by_update,
                by_message,
                command_name="nx_schema_approve",
                schema_proposal_id=schema_proposal_id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                telegram_message_id=telegram_message_id,
                telegram_update_id=telegram_update_id,
                command_hash=command_hash,
            )
            receipt_row = connection.execute(
                "SELECT payload FROM schema_approval_receipt WHERE nonce = ?",
                (claim["receipt_nonce"],),
            ).fetchone()
        if receipt_row is None:
            raise SchemaEventClaimError("Schema event claim has no receipt")
        return _schema_receipt_from_dict(json.loads(receipt_row["payload"]))

    def commit_schema_approval(
        self,
        proposal: SchemaProposalRevision,
        receipt: SchemaApprovalReceipt,
    ) -> SchemaApprovalReceipt:
        """Atomically claim the Telegram event and approve the exact schema head.

        An exact duplicate Telegram delivery returns the already stored receipt.
        Reusing either its update ID or its chat/message ID with changed canonical
        command content fails closed.
        """

        _assert_schema_receipt_matches_proposal(receipt, proposal)
        now = datetime.now().astimezone()
        if proposal.expires_at is None or now >= proposal.expires_at:
            raise SchemaStateError("Schema proposal approval window has expired")
        if now >= receipt.expires_at:
            raise SchemaStateError("Schema approval receipt expired before commit")
        receipt_payload = json.dumps(dataclass_to_dict(receipt), ensure_ascii=False)
        approved = _copy_schema_proposal_with_status(
            proposal,
            SchemaProposalStatus.APPROVED,
        )
        approved_payload = json.dumps(dataclass_to_dict(approved), ensure_ascii=False)
        with self.transaction() as connection:
            by_update = connection.execute(
                "SELECT * FROM schema_gateway_event_claim "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_APPROVAL_PURPOSE, receipt.telegram_update_id),
            ).fetchone()
            by_message = connection.execute(
                "SELECT * FROM schema_gateway_event_claim "
                "WHERE purpose = ? AND chat_id = ? AND telegram_message_id = ?",
                (
                    SCHEMA_APPROVAL_PURPOSE,
                    receipt.chat_id,
                    receipt.telegram_message_id,
                ),
            ).fetchone()
            if by_update is None and by_message is None:
                raise SchemaEventClaimError(
                    "Schema approval event must be durably claimed before signing"
                )
            claim = _exact_schema_event_claim(
                by_update,
                by_message,
                command_name="nx_schema_approve",
                schema_proposal_id=receipt.schema_proposal_id,
                telegram_user_id=receipt.telegram_user_id,
                chat_id=receipt.chat_id,
                thread_id=receipt.thread_id,
                telegram_message_id=receipt.telegram_message_id,
                telegram_update_id=receipt.telegram_update_id,
                command_hash=receipt.command_hash,
            )
            if claim["receipt_nonce"] is not None:
                stored = connection.execute(
                    "SELECT payload FROM schema_approval_receipt WHERE nonce = ?",
                    (claim["receipt_nonce"],),
                ).fetchone()
                if stored is None:
                    raise SchemaEventClaimError("Schema event claim has no receipt")
                return _schema_receipt_from_dict(json.loads(stored["payload"]))

            head = connection.execute(
                "SELECT * FROM schema_proposal WHERE schema_proposal_id = ?",
                (proposal.schema_proposal_id,),
            ).fetchone()
            revision = connection.execute(
                "SELECT digest FROM schema_proposal_revision "
                "WHERE schema_proposal_id = ? AND revision = ?",
                (proposal.schema_proposal_id, proposal.revision),
            ).fetchone()
            if head is None or revision is None:
                raise SchemaStateError("Schema proposal disappeared before approval")
            expected_expiry = proposal.expires_at.isoformat()
            if (
                int(head["current_revision"]) != proposal.revision
                or str(head["status"])
                != SchemaProposalStatus.PENDING_APPROVAL.value
                or str(head["purpose"]) != proposal.purpose
                or str(head["requested_by"]) != proposal.requested_by
                or str(head["chat_id"]) != proposal.chat_id
                or str(head["thread_id"]) != proposal.thread_id
                or str(head["operation_id"]) != proposal.operation_id
                or str(head["logical_database_name"])
                != proposal.logical_database_name
                or str(head["parent_page_id"]) != proposal.parent_page_id
                or head["expires_at"] != expected_expiry
                or str(revision["digest"]) != proposal.digest
            ):
                raise SchemaStateError("Schema proposal changed before approval commit")

            connection.execute(
                """
                INSERT INTO schema_approval_receipt(
                    nonce, schema_proposal_id, revision, purpose,
                    telegram_update_id, telegram_message_id, command_hash,
                    payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.nonce,
                    receipt.schema_proposal_id,
                    receipt.revision,
                    receipt.purpose,
                    receipt.telegram_update_id,
                    receipt.telegram_message_id,
                    receipt.command_hash,
                    receipt_payload,
                    receipt.issued_at.isoformat(),
                ),
            )
            claim_update = connection.execute(
                "UPDATE schema_gateway_event_claim SET receipt_nonce = ? "
                "WHERE purpose = ? AND telegram_update_id = ? "
                "AND command_name = ? AND command_hash = ? "
                "AND schema_proposal_id = ? AND receipt_nonce IS NULL "
                "AND status = 'pending'",
                (
                    receipt.nonce,
                    SCHEMA_APPROVAL_PURPOSE,
                    receipt.telegram_update_id,
                    "nx_schema_approve",
                    receipt.command_hash,
                    receipt.schema_proposal_id,
                ),
            )
            head_update = connection.execute(
                "UPDATE schema_proposal SET status = ? "
                "WHERE schema_proposal_id = ? AND current_revision = ? "
                "AND status = ?",
                (
                    SchemaProposalStatus.APPROVED.value,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    SchemaProposalStatus.PENDING_APPROVAL.value,
                ),
            )
            revision_update = connection.execute(
                "UPDATE schema_proposal_revision SET payload = ? "
                "WHERE schema_proposal_id = ? AND revision = ? AND digest = ?",
                (
                    approved_payload,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                ),
            )
            if (
                claim_update.rowcount != 1
                or head_update.rowcount != 1
                or revision_update.rowcount != 1
            ):
                raise SchemaStateError("Schema proposal changed during approval commit")
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "schema_proposal_approved",
                    proposal.schema_proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "purpose": receipt.purpose,
                            "revision": receipt.revision,
                            "digest": receipt.proposal_digest,
                            "command_hash": receipt.command_hash,
                            "telegram_message_id": receipt.telegram_message_id,
                            "telegram_update_id": receipt.telegram_update_id,
                            "thread_id": receipt.thread_id,
                            "nonce": receipt.nonce,
                        },
                        ensure_ascii=False,
                    ),
                    now.isoformat(),
                ),
            )
        return receipt

    def load_latest_schema_receipt(
        self,
        schema_proposal_id: str,
        revision: int,
    ) -> SchemaApprovalReceipt:
        with self.session() as connection:
            row = connection.execute(
                "SELECT payload FROM schema_approval_receipt "
                "WHERE schema_proposal_id = ? AND revision = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (schema_proposal_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError(
                f"Schema approval receipt not found: {schema_proposal_id}/{revision}"
            )
        return _schema_receipt_from_dict(json.loads(row["payload"]))

    def schema_nonce_used(self, nonce: str) -> bool:
        with self.session() as connection:
            row = connection.execute(
                "SELECT used_at FROM schema_approval_receipt WHERE nonce = ?",
                (nonce,),
            ).fetchone()
        return bool(row and row["used_at"])

    def schema_recovery_nonce_used(self, nonce: str) -> bool:
        with self.session() as connection:
            row = connection.execute(
                "SELECT used_at FROM schema_recovery_receipt WHERE nonce = ?",
                (nonce,),
            ).fetchone()
        return bool(row and row["used_at"])

    def claim_schema_recovery_event(
        self,
        proposal: SchemaProposalRevision,
        *,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
        command_hash: str,
    ) -> dict[str, Any]:
        """Claim fresh GET-only recovery authority in its own purpose table."""

        _validate_schema_gateway_event(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            message_id=telegram_message_id,
            update_id=telegram_update_id,
            command_hash=command_hash,
        )
        if proposal.status is not SchemaProposalStatus.UNKNOWN:
            raise SchemaStateError("Only an UNKNOWN proposal can claim recovery")
        expected_hash = schema_recovery_command_hash(
            proposal.schema_proposal_id,
            proposal.revision,
            proposal.digest,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            telegram_message_id=telegram_message_id,
            telegram_update_id=telegram_update_id,
        )
        if command_hash != expected_hash:
            raise SchemaEventClaimError("Schema recovery command hash is not exact")
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            by_update, by_message = _load_schema_recovery_claim_rows(
                connection,
                chat_id=chat_id,
                message_id=telegram_message_id,
                update_id=telegram_update_id,
            )
            if by_update is not None or by_message is not None:
                return dict(
                    _exact_schema_recovery_event_claim(
                        by_update,
                        by_message,
                        proposal=proposal,
                        telegram_user_id=telegram_user_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        telegram_message_id=telegram_message_id,
                        telegram_update_id=telegram_update_id,
                        command_hash=command_hash,
                    )
                )
            connection.execute(
                """
                INSERT INTO schema_recovery_event_claim(
                    purpose, telegram_update_id, telegram_user_id, chat_id,
                    thread_id, telegram_message_id, command_hash,
                    schema_proposal_id, revision, proposal_digest,
                    status, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    SCHEMA_RECOVERY_PURPOSE,
                    telegram_update_id,
                    telegram_user_id,
                    chat_id,
                    thread_id,
                    telegram_message_id,
                    command_hash,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM schema_recovery_event_claim "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_RECOVERY_PURPOSE, telegram_update_id),
            ).fetchone()
            assert row is not None
            return dict(row)

    def complete_schema_recovery_event(
        self,
        proposal: SchemaProposalRevision,
        *,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
        command_hash: str,
        safe_result: str,
    ) -> dict[str, Any] | None:
        """Complete a separately claimed recovery event; legacy paths are no-op."""

        if not isinstance(safe_result, str) or len(safe_result) > 65_536:
            raise SchemaStateError("Schema recovery result must be at most 65536 chars")
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            by_update, by_message = _load_schema_recovery_claim_rows(
                connection,
                chat_id=chat_id,
                message_id=telegram_message_id,
                update_id=telegram_update_id,
            )
            if by_update is None and by_message is None:
                return None
            claim = _exact_schema_recovery_event_claim(
                by_update,
                by_message,
                proposal=proposal,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                telegram_message_id=telegram_message_id,
                telegram_update_id=telegram_update_id,
                command_hash=command_hash,
            )
            if str(claim["status"]) == "completed":
                return dict(claim)
            cursor = connection.execute(
                "UPDATE schema_recovery_event_claim SET status = 'completed', "
                "safe_result = ?, completed_at = ? WHERE purpose = ? "
                "AND telegram_update_id = ? AND status = 'pending' "
                "AND command_hash = ?",
                (
                    safe_result,
                    now,
                    SCHEMA_RECOVERY_PURPOSE,
                    telegram_update_id,
                    command_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise SchemaEventClaimError(
                    "Schema recovery event changed before completion"
                )
            row = connection.execute(
                "SELECT * FROM schema_recovery_event_claim "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_RECOVERY_PURPOSE, telegram_update_id),
            ).fetchone()
            assert row is not None
            return dict(row)

    def load_schema_recovery_receipt_for_event(
        self,
        *,
        schema_proposal_id: str,
        revision: int,
        proposal_digest: str,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
    ) -> SchemaRecoveryReceipt | None:
        """Load an exact recovery receipt for crash-safe event replay."""

        command_hash = schema_recovery_command_hash(
            schema_proposal_id,
            revision,
            proposal_digest,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            telegram_message_id=telegram_message_id,
            telegram_update_id=telegram_update_id,
        )
        with self.session() as connection:
            by_update = connection.execute(
                "SELECT payload FROM schema_recovery_receipt "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_RECOVERY_PURPOSE, telegram_update_id),
            ).fetchone()
            by_message = connection.execute(
                "SELECT payload FROM schema_recovery_receipt "
                "WHERE purpose = ? AND chat_id = ? AND telegram_message_id = ?",
                (SCHEMA_RECOVERY_PURPOSE, chat_id, telegram_message_id),
            ).fetchone()
            event = connection.execute(
                "SELECT receipt_nonce FROM schema_recovery_event_claim "
                "WHERE purpose = ? AND telegram_update_id = ? "
                "AND chat_id = ? AND telegram_message_id = ?",
                (
                    SCHEMA_RECOVERY_PURPOSE,
                    telegram_update_id,
                    chat_id,
                    telegram_message_id,
                ),
            ).fetchone()
        if by_update is None and by_message is None:
            return None
        if by_update is None or by_message is None or by_update["payload"] != by_message["payload"]:
            raise SchemaEventClaimError(
                "Telegram schema recovery update/message identity changed"
            )
        receipt = _schema_recovery_receipt_from_dict(json.loads(by_update["payload"]))
        if (
            event is None
            or str(event["receipt_nonce"] or "") != receipt.nonce
            or
            receipt.schema_proposal_id != schema_proposal_id
            or receipt.revision != revision
            or receipt.proposal_digest != proposal_digest
            or receipt.telegram_user_id != telegram_user_id
            or receipt.chat_id != chat_id
            or receipt.thread_id != thread_id
            or receipt.telegram_message_id != telegram_message_id
            or receipt.telegram_update_id != telegram_update_id
            or receipt.command_hash != command_hash
        ):
            raise SchemaEventClaimError(
                "Telegram schema recovery event was replayed with changed content"
            )
        return receipt

    def schema_recovery_evidence(
        self,
        proposal: SchemaProposalRevision,
        *,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
        command_hash: str,
    ) -> dict[str, Any]:
        """Validate and return immutable evidence for restart-safe recovery."""

        with self.session() as connection:
            return _validated_schema_recovery_evidence(
                connection,
                proposal,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                telegram_message_id=telegram_message_id,
                telegram_update_id=telegram_update_id,
                command_hash=command_hash,
            )

    def commit_schema_recovery_authority(
        self,
        proposal: SchemaProposalRevision,
        receipt: SchemaRecoveryReceipt,
    ) -> SchemaRecoveryReceipt:
        """Persist one fresh recovery receipt after rechecking all old evidence."""

        _assert_schema_recovery_receipt_matches_proposal(receipt, proposal)
        payload = json.dumps(dataclass_to_dict(receipt), ensure_ascii=False)
        with self.transaction() as connection:
            existing_update = connection.execute(
                "SELECT payload FROM schema_recovery_receipt "
                "WHERE purpose = ? AND telegram_update_id = ?",
                (SCHEMA_RECOVERY_PURPOSE, receipt.telegram_update_id),
            ).fetchone()
            existing_message = connection.execute(
                "SELECT payload FROM schema_recovery_receipt "
                "WHERE purpose = ? AND chat_id = ? AND telegram_message_id = ?",
                (
                    SCHEMA_RECOVERY_PURPOSE,
                    receipt.chat_id,
                    receipt.telegram_message_id,
                ),
            ).fetchone()
            if existing_update is not None or existing_message is not None:
                if (
                    existing_update is None
                    or existing_message is None
                    or existing_update["payload"] != existing_message["payload"]
                ):
                    raise SchemaEventClaimError(
                        "Telegram schema recovery identifiers were reused"
                    )
                stored = _schema_recovery_receipt_from_dict(
                    json.loads(existing_update["payload"])
                )
                if stored != receipt:
                    raise SchemaEventClaimError(
                        "Telegram schema recovery event changed after signing"
                    )
                return stored

            evidence = _validated_schema_recovery_evidence(
                connection,
                proposal,
                telegram_user_id=receipt.telegram_user_id,
                chat_id=receipt.chat_id,
                thread_id=receipt.thread_id,
                telegram_message_id=receipt.telegram_message_id,
                telegram_update_id=receipt.telegram_update_id,
                command_hash=receipt.command_hash,
            )
            if (
                receipt.raw_database_id != evidence["raw_database_id"]
                or receipt.raw_data_source_id != evidence["raw_data_source_id"]
                or receipt.canonical_database_id
                != evidence["canonical_database_id"]
                or receipt.canonical_data_source_id
                != evidence["canonical_data_source_id"]
                or receipt.attempt_started_at != evidence["attempt_started_at"]
                or receipt.attempt_finished_at != evidence["attempt_finished_at"]
                or receipt.approval_receipt_nonce
                != evidence["approval_receipt_nonce"]
                or receipt.conflict_telegram_update_id
                != evidence["conflict_telegram_update_id"]
                or receipt.issued_at < evidence["conflict_completed_at"]
                or receipt.issued_at < evidence["current_claimed_at"]
                or evidence["current_receipt_nonce"] is not None
            ):
                raise SchemaStateError(
                    "Schema recovery receipt differs from persisted evidence"
                )
            connection.execute(
                """
                INSERT INTO schema_recovery_receipt(
                    nonce, schema_proposal_id, revision, purpose,
                    telegram_user_id, chat_id, thread_id, telegram_update_id,
                    telegram_message_id, command_hash, approval_receipt_nonce,
                    conflict_telegram_update_id, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.nonce,
                    receipt.schema_proposal_id,
                    receipt.revision,
                    receipt.purpose,
                    receipt.telegram_user_id,
                    receipt.chat_id,
                    receipt.thread_id,
                    receipt.telegram_update_id,
                    receipt.telegram_message_id,
                    receipt.command_hash,
                    receipt.approval_receipt_nonce,
                    receipt.conflict_telegram_update_id,
                    payload,
                    receipt.issued_at.isoformat(),
                ),
            )
            claim_update = connection.execute(
                "UPDATE schema_recovery_event_claim SET receipt_nonce = ? "
                "WHERE purpose = ? AND telegram_update_id = ? "
                "AND command_hash = ? AND schema_proposal_id = ? "
                "AND revision = ? AND receipt_nonce IS NULL "
                "AND status = 'pending'",
                (
                    receipt.nonce,
                    SCHEMA_RECOVERY_PURPOSE,
                    receipt.telegram_update_id,
                    receipt.command_hash,
                    receipt.schema_proposal_id,
                    receipt.revision,
                ),
            )
            if claim_update.rowcount != 1:
                raise SchemaEventClaimError(
                    "Schema recovery event changed before receipt commit"
                )
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "schema_recovery_authorized",
                    proposal.schema_proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "purpose": receipt.purpose,
                            "revision": receipt.revision,
                            "operation_id": proposal.operation_id,
                            "command_hash": receipt.command_hash,
                            "telegram_update_id": receipt.telegram_update_id,
                            "conflict_telegram_update_id": (
                                receipt.conflict_telegram_update_id
                            ),
                            "nonce": receipt.nonce,
                        },
                        ensure_ascii=False,
                    ),
                    receipt.issued_at.isoformat(),
                ),
            )
        return receipt

    def mark_schema_nonce_used(self, nonce: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE schema_approval_receipt SET used_at = ? "
                "WHERE nonce = ? AND used_at IS NULL",
                (datetime.now().astimezone().isoformat(), nonce),
            )
            if cursor.rowcount != 1:
                raise SchemaStateError("Schema approval nonce is missing or already used")

    def begin_schema_apply(
        self,
        proposal: SchemaProposalRevision,
        nonce: str,
    ) -> None:
        """Atomically consume schema authority, enter APPLYING, and journal it."""

        applying = _copy_schema_proposal_with_status(
            proposal,
            SchemaProposalStatus.APPLYING,
        )
        payload = json.dumps(dataclass_to_dict(applying), ensure_ascii=False)
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            receipt = connection.execute(
                "SELECT payload, used_at FROM schema_approval_receipt "
                "WHERE nonce = ? AND schema_proposal_id = ? AND revision = ?",
                (nonce, proposal.schema_proposal_id, proposal.revision),
            ).fetchone()
            if receipt is None or receipt["used_at"] is not None:
                raise SchemaStateError("Schema approval nonce is missing or already used")
            stored_receipt = _schema_receipt_from_dict(json.loads(receipt["payload"]))
            _assert_schema_receipt_matches_proposal(stored_receipt, proposal)
            nonce_update = connection.execute(
                "UPDATE schema_approval_receipt SET used_at = ? "
                "WHERE nonce = ? AND used_at IS NULL",
                (now, nonce),
            )
            head_update = connection.execute(
                "UPDATE schema_proposal SET status = ? "
                "WHERE schema_proposal_id = ? AND current_revision = ? "
                "AND status = ?",
                (
                    SchemaProposalStatus.APPLYING.value,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    SchemaProposalStatus.APPROVED.value,
                ),
            )
            revision_update = connection.execute(
                "UPDATE schema_proposal_revision SET payload = ? "
                "WHERE schema_proposal_id = ? AND revision = ? AND digest = ?",
                (
                    payload,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                ),
            )
            if (
                nonce_update.rowcount != 1
                or head_update.rowcount != 1
                or revision_update.rowcount != 1
            ):
                raise SchemaStateError("Schema approval changed before apply could start")
            connection.execute(
                """
                INSERT INTO schema_apply_journal(
                    operation_id, schema_proposal_id, revision, purpose,
                    payload, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    proposal.operation_id,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.purpose,
                    payload,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "schema_apply_started",
                    proposal.schema_proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "purpose": proposal.purpose,
                            "revision": proposal.revision,
                            "operation_id": proposal.operation_id,
                            "nonce": nonce,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    def mark_schema_proposal_stale(
        self,
        proposal: SchemaProposalRevision,
        *,
        reason_code: str,
    ) -> None:
        """Atomically revoke an approved, unused schema proposal after live drift."""

        reason = _safe_schema_reason_code(reason_code)
        stale = _copy_schema_proposal_with_status(
            proposal,
            SchemaProposalStatus.STALE,
        )
        payload = json.dumps(dataclass_to_dict(stale), ensure_ascii=False)
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            used = connection.execute(
                "SELECT 1 FROM schema_approval_receipt "
                "WHERE schema_proposal_id = ? AND revision = ? "
                "AND used_at IS NOT NULL LIMIT 1",
                (proposal.schema_proposal_id, proposal.revision),
            ).fetchone()
            if used is not None:
                raise SchemaStateError(
                    "A started schema apply cannot be downgraded to stale"
                )
            head = connection.execute(
                "UPDATE schema_proposal SET status = ? "
                "WHERE schema_proposal_id = ? AND current_revision = ? "
                "AND status = ?",
                (
                    SchemaProposalStatus.STALE.value,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    SchemaProposalStatus.APPROVED.value,
                ),
            )
            revision = connection.execute(
                "UPDATE schema_proposal_revision SET payload = ? "
                "WHERE schema_proposal_id = ? AND revision = ? AND digest = ?",
                (
                    payload,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                ),
            )
            if head.rowcount != 1 or revision.rowcount != 1:
                raise SchemaStateError(
                    "Schema proposal changed before it could be marked stale"
                )
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "schema_proposal_stale",
                    proposal.schema_proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "purpose": proposal.purpose,
                            "revision": proposal.revision,
                            "operation_id": proposal.operation_id,
                            "reason_code": reason,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    def fail_schema_apply(
        self,
        proposal: SchemaProposalRevision,
        *,
        reason_code: str,
        attempted_outcome_known_absent: bool = False,
    ) -> None:
        """Finish an apply only when creation is proved not to have occurred.

        A zero-attempt journal is safe to fail.  Once a network attempt was
        recorded, callers must explicitly attest that a conclusive Notion
        response proved no database was created.  Ambiguous outcomes must use
        the terminal ``unknown`` transition instead.
        """

        reason = _safe_schema_reason_code(reason_code)
        failed = _copy_schema_proposal_with_status(
            proposal,
            SchemaProposalStatus.FAILED,
        )
        payload = json.dumps(dataclass_to_dict(failed), ensure_ascii=False)
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            journal = connection.execute(
                "SELECT status, attempts, remote_database_id, remote_data_source_id "
                "FROM schema_apply_journal WHERE operation_id = ? "
                "AND schema_proposal_id = ? AND revision = ?",
                (
                    proposal.operation_id,
                    proposal.schema_proposal_id,
                    proposal.revision,
                ),
            ).fetchone()
            if (
                journal is None
                or str(journal["status"]) != "pending"
                or journal["remote_database_id"] is not None
                or journal["remote_data_source_id"] is not None
            ):
                raise SchemaStateError(
                    "Schema apply is not eligible for a proved-no-create failure"
                )
            attempts = int(journal["attempts"])
            if attempts > 0 and not attempted_outcome_known_absent:
                raise SchemaStateError(
                    "An attempted schema create without conclusive absence must be unknown"
                )
            journal_update = connection.execute(
                "UPDATE schema_apply_journal SET status = 'failed', last_error = ?, "
                "updated_at = ? WHERE operation_id = ? AND status = 'pending' "
                "AND attempts = ?",
                (reason, now, proposal.operation_id, attempts),
            )
            head_update = connection.execute(
                "UPDATE schema_proposal SET status = ? "
                "WHERE schema_proposal_id = ? AND current_revision = ? "
                "AND status = ?",
                (
                    SchemaProposalStatus.FAILED.value,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    SchemaProposalStatus.APPLYING.value,
                ),
            )
            revision_update = connection.execute(
                "UPDATE schema_proposal_revision SET payload = ? "
                "WHERE schema_proposal_id = ? AND revision = ? AND digest = ?",
                (
                    payload,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                ),
            )
            if (
                journal_update.rowcount != 1
                or head_update.rowcount != 1
                or revision_update.rowcount != 1
            ):
                raise SchemaStateError(
                    "Schema apply changed before failure could be recorded"
                )
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "schema_apply_failed_no_create",
                    proposal.schema_proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "purpose": proposal.purpose,
                            "revision": proposal.revision,
                            "operation_id": proposal.operation_id,
                            "reason_code": reason,
                            "attempts": attempts,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
    def schema_apply_journal_state(self, operation_id: str) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT operation_id, schema_proposal_id, revision, purpose, "
                "status, attempts, last_error, remote_database_id, "
                "remote_data_source_id, attempt_started_at, attempt_finished_at, "
                "updated_at "
                "FROM schema_apply_journal "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_schema_apply_journal(
        self,
        operation_id: str,
        status: str,
        *,
        error: str | None = None,
        remote_database_id: str | None = None,
        remote_data_source_id: str | None = None,
    ) -> None:
        if status not in {"pending", "done", "failed", "blocked", "unknown"}:
            raise ValueError(f"Unsupported schema journal status: {status}")
        if status == "done" and (
            not remote_database_id or not remote_data_source_id
        ):
            raise SchemaStateError(
                "A completed schema journal entry requires both remote IDs"
            )
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, attempts, schema_proposal_id, revision, "
                "remote_database_id, remote_data_source_id, attempt_started_at "
                ", attempt_finished_at "
                "FROM schema_apply_journal "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise SchemaStateError("Schema apply journal entry does not exist")
            current = str(row["status"])
            if current in {"done", "unknown"} and status != current:
                raise SchemaStateError(
                    "A terminal schema operation cannot be blindly reopened"
                )
            has_database_id = bool(remote_database_id)
            has_data_source_id = bool(remote_data_source_id)
            if has_database_id != has_data_source_id:
                raise SchemaStateError(
                    "Schema journal remote IDs must be recorded as an exact pair"
                )
            existing_database_id = str(row["remote_database_id"] or "")
            existing_data_source_id = str(row["remote_data_source_id"] or "")
            if (
                existing_database_id
                and remote_database_id
                and existing_database_id != remote_database_id
            ) or (
                existing_data_source_id
                and remote_data_source_id
                and existing_data_source_id != remote_data_source_id
            ):
                raise SchemaStateError("Schema journal remote identity is immutable")

            attempts = int(row["attempts"])
            attempt_increment = 0
            attempt_started_at: str | None = None
            if status == "pending":
                if current != "pending":
                    raise SchemaStateError("A schema attempt cannot reopen its journal")
                if attempts == 0 and not has_database_id:
                    attempt_increment = 1
                    attempt_started_at = datetime.now().astimezone().isoformat()
                elif attempts == 1 and has_database_id:
                    # The POST returned IDs. Recording them is part of the same
                    # single attempt, not authority for a second POST.
                    pass
                else:
                    raise SchemaStateError(
                        "A schema create POST may be attempted at most once"
                    )
            elif status in {"done", "unknown"}:
                if attempts != 1:
                    raise SchemaStateError(
                        "A terminal schema result requires exactly one recorded attempt"
                    )
                if status == "done" and current not in {"pending", "done"}:
                    raise SchemaStateError("Schema completion state is invalid")
                if status == "unknown" and current not in {"pending", "unknown"}:
                    raise SchemaStateError("Schema unknown state is invalid")
            else:
                raise SchemaStateError(
                    "Use the dedicated atomic API for schema failure states"
                )

            now = datetime.now().astimezone().isoformat()
            attempt_finished_at = (
                now
                if status in {"done", "unknown"}
                or (status == "pending" and has_database_id)
                else None
            )
            cursor = connection.execute(
                "UPDATE schema_apply_journal SET status = ?, attempts = attempts + ?, "
                "last_error = ?, remote_database_id = COALESCE(?, remote_database_id), "
                "remote_data_source_id = COALESCE(?, remote_data_source_id), "
                "attempt_started_at = COALESCE(attempt_started_at, ?), "
                "attempt_finished_at = COALESCE(attempt_finished_at, ?), "
                "updated_at = ? WHERE operation_id = ?",
                (
                    status,
                    attempt_increment,
                    error,
                    remote_database_id,
                    remote_data_source_id,
                    attempt_started_at,
                    attempt_finished_at,
                    now,
                    operation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise SchemaStateError("Schema apply journal changed concurrently")
            if status == "unknown" and current != "unknown":
                proposal_row = connection.execute(
                    "SELECT payload FROM schema_proposal_revision "
                    "WHERE schema_proposal_id = ? AND revision = ?",
                    (row["schema_proposal_id"], row["revision"]),
                ).fetchone()
                if proposal_row is None:
                    raise SchemaStateError("Unknown schema apply lost its proposal")
                proposal = _schema_proposal_from_dict(
                    json.loads(proposal_row["payload"])
                )
                unknown = _copy_schema_proposal_with_status(
                    proposal,
                    SchemaProposalStatus.UNKNOWN,
                )
                unknown_payload = json.dumps(
                    dataclass_to_dict(unknown),
                    ensure_ascii=False,
                )
                head_update = connection.execute(
                    "UPDATE schema_proposal SET status = ? "
                    "WHERE schema_proposal_id = ? AND current_revision = ? "
                    "AND status = ?",
                    (
                        SchemaProposalStatus.UNKNOWN.value,
                        proposal.schema_proposal_id,
                        proposal.revision,
                        SchemaProposalStatus.APPLYING.value,
                    ),
                )
                revision_update = connection.execute(
                    "UPDATE schema_proposal_revision SET payload = ? "
                    "WHERE schema_proposal_id = ? AND revision = ? AND digest = ?",
                    (
                        unknown_payload,
                        proposal.schema_proposal_id,
                        proposal.revision,
                        proposal.digest,
                    ),
                )
                if head_update.rowcount != 1 or revision_update.rowcount != 1:
                    raise SchemaStateError(
                        "Schema proposal changed before UNKNOWN could be recorded"
                    )
                connection.execute(
                    "INSERT INTO audit_log(" 
                    "event_type, proposal_id, actor, details, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        "schema_apply_outcome_unknown",
                        proposal.schema_proposal_id,
                        proposal.requested_by,
                        json.dumps(
                            {
                                "purpose": proposal.purpose,
                                "revision": proposal.revision,
                                "operation_id": proposal.operation_id,
                            },
                            ensure_ascii=False,
                        ),
                        datetime.now().astimezone().isoformat(),
                    ),
                )

    def finalize_schema_installation(
        self,
        proposal: SchemaProposalRevision,
        *,
        database_id: str,
        data_source_id: str,
    ) -> None:
        """Commit an installed schema without touching the Excel checkpoint."""

        if not database_id.strip() or not data_source_id.strip():
            raise SchemaStateError("Remote database and data source IDs are required")
        committed = _copy_schema_proposal_with_status(
            proposal,
            SchemaProposalStatus.COMMITTED,
        )
        payload = json.dumps(dataclass_to_dict(committed), ensure_ascii=False)
        schema_digest = sha256_json(proposal.schema_payload)
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            head = connection.execute(
                "SELECT current_revision, status FROM schema_proposal "
                "WHERE schema_proposal_id = ?",
                (proposal.schema_proposal_id,),
            ).fetchone()
            journal = connection.execute(
                "SELECT status, remote_database_id, remote_data_source_id "
                "FROM schema_apply_journal WHERE operation_id = ? "
                "AND schema_proposal_id = ? AND revision = ?",
                (
                    proposal.operation_id,
                    proposal.schema_proposal_id,
                    proposal.revision,
                ),
            ).fetchone()
            used_receipt = connection.execute(
                "SELECT 1 FROM schema_approval_receipt "
                "WHERE schema_proposal_id = ? AND revision = ? "
                "AND used_at IS NOT NULL LIMIT 1",
                (proposal.schema_proposal_id, proposal.revision),
            ).fetchone()
            head_status = str(head["status"]) if head is not None else ""
            expected_journal_status = (
                "done"
                if head_status == SchemaProposalStatus.APPLYING.value
                else "unknown"
            )
            if (
                head is None
                or int(head["current_revision"]) != proposal.revision
                or head_status
                not in {
                    SchemaProposalStatus.APPLYING.value,
                    SchemaProposalStatus.UNKNOWN.value,
                }
                or proposal.status.value != head_status
                or journal is None
                or str(journal["status"]) != expected_journal_status
                or str(journal["remote_database_id"] or "") != database_id
                or str(journal["remote_data_source_id"] or "") != data_source_id
                or used_receipt is None
            ):
                raise SchemaStateError(
                    "Schema installation is not an exactly completed apply"
                )
            installed = connection.execute(
                "SELECT * FROM notion_schema_installation "
                "WHERE logical_database_name = ? OR database_id = ? "
                "OR data_source_id = ?",
                (
                    proposal.logical_database_name,
                    database_id,
                    data_source_id,
                ),
            ).fetchall()
            if installed:
                exact = all(
                    str(row["logical_database_name"])
                    == proposal.logical_database_name
                    and str(row["database_id"]) == database_id
                    and str(row["data_source_id"]) == data_source_id
                    and str(row["parent_page_id"]) == proposal.parent_page_id
                    and str(row["schema_digest"]) == schema_digest
                    for row in installed
                )
                if not exact:
                    raise SchemaStateError(
                        "A conflicting Notion schema installation already exists"
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO notion_schema_installation(
                        logical_database_name, database_id, data_source_id,
                        parent_page_id, schema_digest, schema_proposal_id,
                        revision, installed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.logical_database_name,
                        database_id,
                        data_source_id,
                        proposal.parent_page_id,
                        schema_digest,
                        proposal.schema_proposal_id,
                        proposal.revision,
                        now,
                    ),
                )
            head_update = connection.execute(
                "UPDATE schema_proposal SET status = ? "
                "WHERE schema_proposal_id = ? AND current_revision = ? "
                "AND status = ?",
                (
                    SchemaProposalStatus.COMMITTED.value,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    head_status,
                ),
            )
            revision_update = connection.execute(
                "UPDATE schema_proposal_revision SET payload = ? "
                "WHERE schema_proposal_id = ? AND revision = ? AND digest = ?",
                (
                    payload,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                ),
            )
            if head_update.rowcount != 1 or revision_update.rowcount != 1:
                raise SchemaStateError("Schema proposal changed before finalization")
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "schema_apply_committed",
                    proposal.schema_proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "purpose": proposal.purpose,
                            "revision": proposal.revision,
                            "operation_id": proposal.operation_id,
                            "logical_database_name": proposal.logical_database_name,
                            "database_id": database_id,
                            "data_source_id": data_source_id,
                            "schema_digest": schema_digest,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    def finalize_schema_recovery_installation(
        self,
        proposal: SchemaProposalRevision,
        receipt: SchemaRecoveryReceipt,
        *,
        database_id: str,
        data_source_id: str,
    ) -> None:
        """Atomically consume GET-only recovery authority and register the schema."""

        _assert_schema_recovery_receipt_matches_proposal(receipt, proposal)
        if (
            database_id != receipt.canonical_database_id
            or data_source_id != receipt.canonical_data_source_id
        ):
            raise SchemaStateError(
                "Verified remote identity differs from recovery authority"
            )
        committed = _copy_schema_proposal_with_status(
            proposal,
            SchemaProposalStatus.COMMITTED,
        )
        proposal_payload = json.dumps(dataclass_to_dict(committed), ensure_ascii=False)
        receipt_payload = json.dumps(dataclass_to_dict(receipt), ensure_ascii=False)
        schema_digest = sha256_json(proposal.schema_payload)
        now = datetime.now().astimezone()
        with self.transaction() as connection:
            evidence = _validated_schema_recovery_evidence(
                connection,
                proposal,
                telegram_user_id=receipt.telegram_user_id,
                chat_id=receipt.chat_id,
                thread_id=receipt.thread_id,
                telegram_message_id=receipt.telegram_message_id,
                telegram_update_id=receipt.telegram_update_id,
                command_hash=receipt.command_hash,
            )
            if (
                receipt.raw_database_id != evidence["raw_database_id"]
                or receipt.raw_data_source_id != evidence["raw_data_source_id"]
                or receipt.canonical_database_id
                != evidence["canonical_database_id"]
                or receipt.canonical_data_source_id
                != evidence["canonical_data_source_id"]
                or receipt.attempt_started_at != evidence["attempt_started_at"]
                or receipt.attempt_finished_at != evidence["attempt_finished_at"]
                or receipt.approval_receipt_nonce
                != evidence["approval_receipt_nonce"]
                or receipt.conflict_telegram_update_id
                != evidence["conflict_telegram_update_id"]
                or receipt.issued_at < evidence["conflict_completed_at"]
                or receipt.issued_at < evidence["current_claimed_at"]
                or evidence["current_receipt_nonce"] != receipt.nonce
            ):
                raise SchemaStateError(
                    "Schema recovery evidence changed before finalization"
                )
            stored_receipt = connection.execute(
                "SELECT payload, used_at FROM schema_recovery_receipt "
                "WHERE nonce = ? AND schema_proposal_id = ? AND revision = ?",
                (
                    receipt.nonce,
                    receipt.schema_proposal_id,
                    receipt.revision,
                ),
            ).fetchone()
            if (
                stored_receipt is None
                or stored_receipt["used_at"] is not None
                or stored_receipt["payload"] != receipt_payload
            ):
                raise SchemaStateError(
                    "Schema recovery receipt is missing, changed, or already used"
                )
            head = connection.execute(
                "SELECT current_revision, status FROM schema_proposal "
                "WHERE schema_proposal_id = ?",
                (proposal.schema_proposal_id,),
            ).fetchone()
            revision = connection.execute(
                "SELECT digest FROM schema_proposal_revision "
                "WHERE schema_proposal_id = ? AND revision = ?",
                (proposal.schema_proposal_id, proposal.revision),
            ).fetchone()
            if (
                head is None
                or int(head["current_revision"]) != proposal.revision
                or str(head["status"]) != SchemaProposalStatus.UNKNOWN.value
                or revision is None
                or str(revision["digest"]) != proposal.digest
            ):
                raise SchemaStateError(
                    "Schema recovery proposal changed before finalization"
                )
            installed = connection.execute(
                "SELECT * FROM notion_schema_installation "
                "WHERE logical_database_name = ? OR database_id = ? "
                "OR data_source_id = ?",
                (
                    proposal.logical_database_name,
                    receipt.raw_database_id,
                    receipt.raw_data_source_id,
                ),
            ).fetchall()
            if installed:
                exact = all(
                    str(row["logical_database_name"])
                    == proposal.logical_database_name
                    and str(row["database_id"]) == receipt.raw_database_id
                    and str(row["data_source_id"]) == receipt.raw_data_source_id
                    and str(row["parent_page_id"]) == proposal.parent_page_id
                    and str(row["schema_digest"]) == schema_digest
                    for row in installed
                )
                if not exact:
                    raise SchemaStateError(
                        "A conflicting Notion schema installation already exists"
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO notion_schema_installation(
                        logical_database_name, database_id, data_source_id,
                        parent_page_id, schema_digest, schema_proposal_id,
                        revision, installed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.logical_database_name,
                        receipt.raw_database_id,
                        receipt.raw_data_source_id,
                        proposal.parent_page_id,
                        schema_digest,
                        proposal.schema_proposal_id,
                        proposal.revision,
                        now.isoformat(),
                    ),
                )
            nonce_update = connection.execute(
                "UPDATE schema_recovery_receipt SET used_at = ? "
                "WHERE nonce = ? AND used_at IS NULL",
                (now.isoformat(), receipt.nonce),
            )
            head_update = connection.execute(
                "UPDATE schema_proposal SET status = ? "
                "WHERE schema_proposal_id = ? AND current_revision = ? "
                "AND status = ?",
                (
                    SchemaProposalStatus.COMMITTED.value,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    SchemaProposalStatus.UNKNOWN.value,
                ),
            )
            revision_update = connection.execute(
                "UPDATE schema_proposal_revision SET payload = ? "
                "WHERE schema_proposal_id = ? AND revision = ? AND digest = ?",
                (
                    proposal_payload,
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                ),
            )
            if (
                nonce_update.rowcount != 1
                or head_update.rowcount != 1
                or revision_update.rowcount != 1
            ):
                raise SchemaStateError(
                    "Schema recovery state changed during finalization"
                )
            connection.execute(
                "INSERT INTO audit_log(event_type, proposal_id, actor, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "schema_recovery_committed",
                    proposal.schema_proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "purpose": receipt.purpose,
                            "revision": proposal.revision,
                            "operation_id": proposal.operation_id,
                            "logical_database_name": proposal.logical_database_name,
                            "database_id": receipt.raw_database_id,
                            "data_source_id": receipt.raw_data_source_id,
                            "schema_digest": schema_digest,
                            "recovery_nonce": receipt.nonce,
                        },
                        ensure_ascii=False,
                    ),
                    now.isoformat(),
                ),
            )

    def get_schema_installation(
        self,
        logical_database_name: str,
    ) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM notion_schema_installation "
                "WHERE logical_database_name = ?",
                (logical_database_name,),
            ).fetchone()
        return dict(row) if row else None

    def schema_installations(self) -> dict[str, str]:
        """Return verified logical-name to data-source-ID overlays."""

        with self.session() as connection:
            rows = connection.execute(
                "SELECT logical_database_name, data_source_id "
                "FROM notion_schema_installation ORDER BY logical_database_name"
            ).fetchall()
        return {
            str(row["logical_database_name"]): str(row["data_source_id"])
            for row in rows
        }

    def schema_installation_registry_exists(self) -> bool:
        """Check for the optional schema registry without migrating the database."""

        with self.session() as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'notion_schema_installation' LIMIT 1"
            ).fetchone()
        return row is not None

    def save_analyzer_definition(
        self,
        name: str,
        version: str,
        lifecycle: str,
        manifest: dict[str, Any],
    ) -> None:
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO analyzer_definition(
                    name, version, lifecycle, manifest, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name, version) DO UPDATE SET
                    lifecycle = excluded.lifecycle,
                    manifest = excluded.manifest
                """,
                (
                    name,
                    version,
                    lifecycle,
                    json.dumps(manifest, ensure_ascii=False, default=str),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def finalize_success(
        self,
        proposal: ProposalRevision,
        *,
        drive_id: str,
        item_id: str,
        version_id: str,
        file_hash: str,
    ) -> None:
        """Atomically commit local proposal, pending queue, and source checkpoint."""

        now = datetime.now().astimezone().isoformat()
        payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        with self.transaction() as connection:
            proposal_cursor = connection.execute(
                "UPDATE proposal SET status = ?, expires_at = ? "
                "WHERE proposal_id = ? AND current_revision = ?",
                (
                    proposal.status.value,
                    proposal.expires_at.isoformat() if proposal.expires_at else None,
                    proposal.proposal_id,
                    proposal.revision,
                ),
            )
            revision_cursor = connection.execute(
                "UPDATE proposal_revision SET payload = ?, digest = ? "
                "WHERE proposal_id = ? AND revision = ?",
                (
                    payload,
                    proposal.digest,
                    proposal.proposal_id,
                    proposal.revision,
                ),
            )
            if proposal_cursor.rowcount != 1 or revision_cursor.rowcount != 1:
                raise RuntimeError(
                    "Proposal head changed before the local commit could finalize"
                )
            for operation in proposal.operations:
                if operation.action is ProposalAction.DEFER:
                    operation_payload = json.dumps(
                        dataclass_to_dict(operation),
                        ensure_ascii=False,
                        default=str,
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO pending_change("
                        "operation_id, proposal_id, payload, created_at, resolved_at"
                        ") VALUES (?, ?, ?, ?, NULL)",
                        (
                            operation.change.operation_id,
                            proposal.proposal_id,
                            operation_payload,
                            now,
                        ),
                    )
                    continue
                connection.execute(
                    "UPDATE pending_change SET resolved_at = ? "
                    "WHERE operation_id = ? AND resolved_at IS NULL",
                    (now, operation.change.operation_id),
                )
            connection.execute(
                """
                INSERT INTO sync_checkpoint(
                    drive_id, item_id, version_id, file_hash, committed_at, proposal_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(drive_id, item_id) DO UPDATE SET
                    version_id = excluded.version_id,
                    file_hash = excluded.file_hash,
                    committed_at = excluded.committed_at,
                    proposal_id = excluded.proposal_id
                """,
                (
                    drive_id,
                    item_id,
                    version_id,
                    file_hash,
                    now,
                    proposal.proposal_id,
                ),
            )
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    "apply_committed",
                    proposal.proposal_id,
                    proposal.requested_by,
                    json.dumps(
                        {
                            "revision": proposal.revision,
                            "checkpoint": version_id,
                            "file_hash": file_hash,
                            "applied_scope": [
                                item.change.operation_id
                                for item in proposal.operations
                                if item.action
                                in {ProposalAction.APPLY, ProposalAction.EDIT}
                            ],
                            "excluded": [
                                item.change.operation_id
                                for item in proposal.operations
                                if item.action is ProposalAction.EXCLUDE
                            ],
                            "deferred": [
                                item.change.operation_id
                                for item in proposal.operations
                                if item.action is ProposalAction.DEFER
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )


def _source_ref(data: dict[str, Any]) -> SourceRef:
    return SourceRef(**data)


def _knowledge_ref(data: dict[str, Any]) -> KnowledgeRef:
    return KnowledgeRef(**data)


def _change(data: dict[str, Any]) -> ProposedChange:
    return ProposedChange(
        target_database=data["target_database"],
        entity_key=data["entity_key"],
        property_name=data["property_name"],
        kind=ChangeKind(data["kind"]),
        current_value=data.get("current_value"),
        proposed_value=data.get("proposed_value"),
        analyzer=data["analyzer"],
        analyzer_version=data["analyzer_version"],
        confidence=float(data["confidence"]),
        reason=data["reason"],
        source_refs=[_source_ref(item) for item in data.get("source_refs", [])],
        knowledge_refs=[
            _knowledge_ref(item) for item in data.get("knowledge_refs", [])
        ],
        operation_id=data["operation_id"],
    )


def _operation_from_dict(data: dict[str, Any]) -> ProposalOperation:
    return ProposalOperation(
        change=_change(data["change"]),
        action=ProposalAction(data["action"]),
        approved_value=data.get("approved_value"),
        user_override=bool(data.get("user_override", False)),
    )


def _proposal_from_dict(data: dict[str, Any]) -> ProposalRevision:
    operations = [_operation_from_dict(item) for item in data["operations"]]
    return ProposalRevision(
        proposal_id=data["proposal_id"],
        revision=int(data["revision"]),
        source_version_id=data["source_version_id"],
        source_file_hash=data["source_file_hash"],
        requested_by=data["requested_by"],
        chat_id=data["chat_id"],
        operations=operations,
        notion_binding=data.get("notion_binding", {}),
        knowledge_binding=data.get("knowledge_binding", {}),
        status=ProposalStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
    )


def _schema_proposal_from_dict(data: dict[str, Any]) -> SchemaProposalRevision:
    if data.get("purpose", SCHEMA_APPROVAL_PURPOSE) != SCHEMA_APPROVAL_PURPOSE:
        raise SchemaStateError("Stored schema proposal has an unsupported purpose")
    return SchemaProposalRevision(
        schema_proposal_id=str(data["schema_proposal_id"]),
        revision=int(data["revision"]),
        requested_by=str(data["requested_by"]),
        chat_id=str(data["chat_id"]),
        thread_id=str(data.get("thread_id", "")),
        operation_id=str(data["operation_id"]),
        logical_database_name=str(data["logical_database_name"]),
        parent_page_id=str(data["parent_page_id"]),
        schema_payload=dict(data["schema_payload"]),
        precondition=dict(data["precondition"]),
        status=SchemaProposalStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        expires_at=(
            datetime.fromisoformat(data["expires_at"])
            if data.get("expires_at")
            else None
        ),
    )


def _schema_receipt_from_dict(data: dict[str, Any]) -> SchemaApprovalReceipt:
    if data.get("purpose", SCHEMA_APPROVAL_PURPOSE) != SCHEMA_APPROVAL_PURPOSE:
        raise SchemaStateError("Stored schema receipt has an unsupported purpose")
    return SchemaApprovalReceipt(
        schema_proposal_id=str(data["schema_proposal_id"]),
        revision=int(data["revision"]),
        proposal_digest=str(data["proposal_digest"]),
        precondition_digest=str(data["precondition_digest"]),
        telegram_user_id=str(data["telegram_user_id"]),
        chat_id=str(data["chat_id"]),
        thread_id=str(data.get("thread_id", "")),
        telegram_message_id=str(data["telegram_message_id"]),
        telegram_update_id=int(data["telegram_update_id"]),
        command_hash=str(data["command_hash"]),
        issued_at=datetime.fromisoformat(data["issued_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        nonce=str(data["nonce"]),
        signature=str(data["signature"]),
    )


def _schema_recovery_receipt_from_dict(
    data: dict[str, Any],
) -> SchemaRecoveryReceipt:
    if data.get("purpose", SCHEMA_RECOVERY_PURPOSE) != SCHEMA_RECOVERY_PURPOSE:
        raise SchemaStateError("Stored schema recovery receipt has an unsupported purpose")
    return SchemaRecoveryReceipt(
        schema_proposal_id=str(data["schema_proposal_id"]),
        revision=int(data["revision"]),
        proposal_digest=str(data["proposal_digest"]),
        precondition_digest=str(data["precondition_digest"]),
        operation_id=str(data["operation_id"]),
        raw_database_id=str(data["raw_database_id"]),
        raw_data_source_id=str(data["raw_data_source_id"]),
        canonical_database_id=str(data["canonical_database_id"]),
        canonical_data_source_id=str(data["canonical_data_source_id"]),
        attempt_started_at=datetime.fromisoformat(data["attempt_started_at"]),
        attempt_finished_at=datetime.fromisoformat(data["attempt_finished_at"]),
        approval_receipt_nonce=str(data["approval_receipt_nonce"]),
        conflict_telegram_update_id=int(data["conflict_telegram_update_id"]),
        telegram_user_id=str(data["telegram_user_id"]),
        chat_id=str(data["chat_id"]),
        thread_id=str(data.get("thread_id", "")),
        telegram_message_id=str(data["telegram_message_id"]),
        telegram_update_id=int(data["telegram_update_id"]),
        command_hash=str(data["command_hash"]),
        issued_at=datetime.fromisoformat(data["issued_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        nonce=str(data["nonce"]),
        signature=str(data["signature"]),
    )


def _copy_schema_proposal_with_status(
    proposal: SchemaProposalRevision,
    status: SchemaProposalStatus,
) -> SchemaProposalRevision:
    return SchemaProposalRevision(
        schema_proposal_id=proposal.schema_proposal_id,
        revision=proposal.revision,
        requested_by=proposal.requested_by,
        chat_id=proposal.chat_id,
        thread_id=proposal.thread_id,
        operation_id=proposal.operation_id,
        logical_database_name=proposal.logical_database_name,
        parent_page_id=proposal.parent_page_id,
        schema_payload=dict(proposal.schema_payload),
        precondition=dict(proposal.precondition),
        status=status,
        created_at=proposal.created_at,
        expires_at=proposal.expires_at,
    )


def _assert_schema_receipt_matches_proposal(
    receipt: SchemaApprovalReceipt,
    proposal: SchemaProposalRevision,
) -> None:
    if receipt.purpose != SCHEMA_APPROVAL_PURPOSE:
        raise SchemaStateError("Unsupported schema approval receipt purpose")
    expected_command_hash = schema_approval_command_hash(
        proposal.schema_proposal_id,
        proposal.revision,
        proposal.digest,
    )
    if (
        receipt.schema_proposal_id != proposal.schema_proposal_id
        or receipt.revision != proposal.revision
        or receipt.proposal_digest != proposal.digest
        or receipt.precondition_digest != proposal.precondition_digest
        or receipt.telegram_user_id != proposal.requested_by
        or receipt.chat_id != proposal.chat_id
        or receipt.thread_id != proposal.thread_id
        or receipt.command_hash != expected_command_hash
    ):
        raise SchemaStateError("Schema approval receipt does not match the proposal")
    if (
        not receipt.telegram_message_id.strip()
        or receipt.telegram_update_id < 0
        or not receipt.nonce
        or not receipt.signature
    ):
        raise SchemaStateError("Schema approval receipt is incomplete")
    if (
        receipt.issued_at.tzinfo is None
        or receipt.expires_at.tzinfo is None
        or receipt.expires_at <= receipt.issued_at
    ):
        raise SchemaStateError("Schema approval receipt validity window is invalid")


def _assert_schema_recovery_receipt_matches_proposal(
    receipt: SchemaRecoveryReceipt,
    proposal: SchemaProposalRevision,
) -> None:
    if not isinstance(receipt, SchemaRecoveryReceipt):
        raise SchemaStateError("A schema-recovery receipt is required")
    expected_command_hash = schema_recovery_command_hash(
        proposal.schema_proposal_id,
        proposal.revision,
        proposal.digest,
        telegram_user_id=receipt.telegram_user_id,
        chat_id=receipt.chat_id,
        thread_id=receipt.thread_id,
        telegram_message_id=receipt.telegram_message_id,
        telegram_update_id=receipt.telegram_update_id,
    )
    if (
        receipt.purpose != SCHEMA_RECOVERY_PURPOSE
        or proposal.status is not SchemaProposalStatus.UNKNOWN
        or receipt.schema_proposal_id != proposal.schema_proposal_id
        or receipt.revision != proposal.revision
        or receipt.proposal_digest != proposal.digest
        or receipt.precondition_digest != proposal.precondition_digest
        or receipt.operation_id != proposal.operation_id
        or receipt.telegram_user_id != proposal.requested_by
        or receipt.chat_id != proposal.chat_id
        or receipt.thread_id != proposal.thread_id
        or receipt.command_hash != expected_command_hash
        or _canonical_schema_remote_id(receipt.raw_database_id)
        != receipt.canonical_database_id
        or _canonical_schema_remote_id(receipt.raw_data_source_id)
        != receipt.canonical_data_source_id
    ):
        raise SchemaStateError(
            "Schema recovery receipt does not match the exact proposal scope"
        )
    if (
        not receipt.telegram_message_id.strip()
        or receipt.telegram_update_id < 0
        or receipt.conflict_telegram_update_id < 0
        or not receipt.approval_receipt_nonce.strip()
        or not receipt.nonce.strip()
        or not receipt.signature.strip()
    ):
        raise SchemaStateError("Schema recovery receipt is incomplete")
    if (
        receipt.attempt_started_at.tzinfo is None
        or receipt.attempt_finished_at.tzinfo is None
        or receipt.issued_at.tzinfo is None
        or receipt.expires_at.tzinfo is None
        or receipt.attempt_finished_at < receipt.attempt_started_at
        or receipt.issued_at < receipt.attempt_finished_at
        or receipt.expires_at <= receipt.issued_at
    ):
        raise SchemaStateError("Schema recovery receipt time ordering is invalid")


def _normalize_schema_command_name(command_name: str) -> str:
    normalized = str(command_name).strip().lstrip("/").lower()
    if (
        not normalized.startswith("nx_schema_")
        or len(normalized) > 64
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in normalized)
    ):
        raise SchemaEventClaimError("Unsupported schema gateway command name")
    return normalized


def _validate_schema_gateway_event(
    *,
    telegram_user_id: str,
    chat_id: str,
    message_id: str,
    update_id: int,
    command_hash: str,
) -> None:
    if (
        not str(telegram_user_id).strip()
        or not str(chat_id).strip()
        or not str(message_id).strip()
    ):
        raise SchemaEventClaimError(
            "Telegram user, chat, and message IDs are required"
        )
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        raise SchemaEventClaimError("Telegram update ID is invalid")
    if len(command_hash) != 64 or any(
        character not in "0123456789abcdef" for character in command_hash
    ):
        raise SchemaEventClaimError("Schema gateway command hash must be lowercase SHA-256")


def _load_schema_gateway_claim_rows(
    connection: sqlite3.Connection,
    *,
    chat_id: str,
    message_id: str,
    update_id: int,
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    by_update = connection.execute(
        "SELECT * FROM schema_gateway_event_claim "
        "WHERE purpose = ? AND telegram_update_id = ?",
        (SCHEMA_APPROVAL_PURPOSE, update_id),
    ).fetchone()
    by_message = connection.execute(
        "SELECT * FROM schema_gateway_event_claim "
        "WHERE purpose = ? AND chat_id = ? AND telegram_message_id = ?",
        (SCHEMA_APPROVAL_PURPOSE, chat_id, message_id),
    ).fetchone()
    return by_update, by_message


def _exact_schema_event_claim(
    by_update: sqlite3.Row | None,
    by_message: sqlite3.Row | None,
    *,
    command_name: str,
    schema_proposal_id: str | None,
    telegram_user_id: str,
    chat_id: str,
    thread_id: str,
    telegram_message_id: str,
    telegram_update_id: int,
    command_hash: str,
) -> sqlite3.Row:
    if by_update is None or by_message is None:
        raise SchemaEventClaimError(
            "Telegram schema approval update/message was replayed with changed identity"
        )
    if (
        int(by_update["telegram_update_id"])
        != int(by_message["telegram_update_id"])
        or str(by_update["chat_id"]) != str(by_message["chat_id"])
        or str(by_update["telegram_message_id"])
        != str(by_message["telegram_message_id"])
    ):
        raise SchemaEventClaimError(
            "Telegram schema approval update and message claims disagree"
        )
    claim = by_update
    if (
        str(claim["purpose"]) != SCHEMA_APPROVAL_PURPOSE
        or int(claim["telegram_update_id"]) != telegram_update_id
        or str(claim["telegram_user_id"]) != telegram_user_id
        or str(claim["chat_id"]) != chat_id
        or str(claim["thread_id"]) != thread_id
        or str(claim["telegram_message_id"]) != telegram_message_id
        or str(claim["command_name"]) != command_name
        or str(claim["command_hash"]) != command_hash
        or (
            schema_proposal_id is not None
            and
            str(claim["schema_proposal_id"] or "")
            != str(schema_proposal_id or "")
        )
    ):
        raise SchemaEventClaimError(
            "Telegram schema approval event was replayed with changed command content"
        )
    return claim


def _load_schema_recovery_claim_rows(
    connection: sqlite3.Connection,
    *,
    chat_id: str,
    message_id: str,
    update_id: int,
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    by_update = connection.execute(
        "SELECT * FROM schema_recovery_event_claim "
        "WHERE purpose = ? AND telegram_update_id = ?",
        (SCHEMA_RECOVERY_PURPOSE, update_id),
    ).fetchone()
    by_message = connection.execute(
        "SELECT * FROM schema_recovery_event_claim "
        "WHERE purpose = ? AND chat_id = ? AND telegram_message_id = ?",
        (SCHEMA_RECOVERY_PURPOSE, chat_id, message_id),
    ).fetchone()
    return by_update, by_message


def _exact_schema_recovery_event_claim(
    by_update: sqlite3.Row | None,
    by_message: sqlite3.Row | None,
    *,
    proposal: SchemaProposalRevision,
    telegram_user_id: str,
    chat_id: str,
    thread_id: str,
    telegram_message_id: str,
    telegram_update_id: int,
    command_hash: str,
) -> sqlite3.Row:
    if by_update is None or by_message is None:
        raise SchemaEventClaimError(
            "Telegram recovery update/message was replayed with changed identity"
        )
    if (
        int(by_update["telegram_update_id"])
        != int(by_message["telegram_update_id"])
        or str(by_update["chat_id"]) != str(by_message["chat_id"])
        or str(by_update["telegram_message_id"])
        != str(by_message["telegram_message_id"])
    ):
        raise SchemaEventClaimError(
            "Telegram recovery update and message claims disagree"
        )
    claim = by_update
    if (
        str(claim["purpose"]) != SCHEMA_RECOVERY_PURPOSE
        or int(claim["telegram_update_id"]) != telegram_update_id
        or str(claim["telegram_user_id"]) != telegram_user_id
        or str(claim["chat_id"]) != chat_id
        or str(claim["thread_id"]) != thread_id
        or str(claim["telegram_message_id"]) != telegram_message_id
        or str(claim["command_hash"]) != command_hash
        or str(claim["schema_proposal_id"]) != proposal.schema_proposal_id
        or int(claim["revision"]) != proposal.revision
        or str(claim["proposal_digest"]) != proposal.digest
    ):
        raise SchemaEventClaimError(
            "Telegram recovery event was replayed with changed command content"
        )
    return claim


_SCHEMA_REMOTE_ID_RE = re.compile(
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)


def _canonical_schema_remote_id(value: object) -> str:
    raw = str(value or "").strip()
    if _SCHEMA_REMOTE_ID_RE.fullmatch(raw) is None:
        raise SchemaStateError("Schema journal remote ID is invalid")
    return raw.replace("-", "").lower()


def _schema_evidence_time(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SchemaStateError(f"Schema recovery {label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaStateError(f"Schema recovery {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise SchemaStateError(f"Schema recovery {label} has no timezone")
    return parsed.astimezone()


def _validated_schema_recovery_evidence(
    connection: sqlite3.Connection,
    proposal: SchemaProposalRevision,
    *,
    telegram_user_id: str,
    chat_id: str,
    thread_id: str,
    telegram_message_id: str,
    telegram_update_id: int,
    command_hash: str,
) -> dict[str, Any]:
    """Validate the complete local attestation chain for recovery issuance."""

    if proposal.status is not SchemaProposalStatus.UNKNOWN:
        raise SchemaStateError("Only an UNKNOWN schema proposal can use recovery authority")
    expected_recovery_hash = schema_recovery_command_hash(
        proposal.schema_proposal_id,
        proposal.revision,
        proposal.digest,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        telegram_message_id=telegram_message_id,
        telegram_update_id=telegram_update_id,
    )
    if command_hash != expected_recovery_hash:
        raise SchemaEventClaimError("Schema recovery command hash is not exact")
    current_update, current_message = _load_schema_recovery_claim_rows(
        connection,
        chat_id=chat_id,
        message_id=telegram_message_id,
        update_id=telegram_update_id,
    )
    current_claim = _exact_schema_recovery_event_claim(
        current_update,
        current_message,
        proposal=proposal,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        telegram_message_id=telegram_message_id,
        telegram_update_id=telegram_update_id,
        command_hash=command_hash,
    )
    if str(current_claim["status"]) != "pending":
        raise SchemaEventClaimError("Fresh schema recovery event is not pending")
    current_claimed_at = _schema_evidence_time(
        current_claim["claimed_at"],
        label="event claim time",
    )

    head = connection.execute(
        "SELECT * FROM schema_proposal WHERE schema_proposal_id = ?",
        (proposal.schema_proposal_id,),
    ).fetchone()
    revision = connection.execute(
        "SELECT digest, payload FROM schema_proposal_revision "
        "WHERE schema_proposal_id = ? AND revision = ?",
        (proposal.schema_proposal_id, proposal.revision),
    ).fetchone()
    if head is None or revision is None:
        raise SchemaStateError("Schema recovery proposal is missing")
    persisted_proposal = _schema_proposal_from_dict(json.loads(revision["payload"]))
    if (
        int(head["current_revision"]) != proposal.revision
        or str(head["purpose"]) != SCHEMA_APPROVAL_PURPOSE
        or str(head["status"]) != SchemaProposalStatus.UNKNOWN.value
        or str(head["requested_by"]) != proposal.requested_by
        or str(head["chat_id"]) != proposal.chat_id
        or str(head["thread_id"]) != proposal.thread_id
        or str(head["operation_id"]) != proposal.operation_id
        or str(revision["digest"]) != proposal.digest
        or persisted_proposal.status is not SchemaProposalStatus.UNKNOWN
        or persisted_proposal.digest != proposal.digest
    ):
        raise SchemaStateError("Schema recovery proposal scope changed")

    journal = connection.execute(
        "SELECT * FROM schema_apply_journal WHERE operation_id = ?",
        (proposal.operation_id,),
    ).fetchone()
    if journal is None:
        raise SchemaStateError("Schema recovery journal is missing")
    raw_database_id = str(journal["remote_database_id"] or "")
    raw_data_source_id = str(journal["remote_data_source_id"] or "")
    if (
        str(journal["schema_proposal_id"]) != proposal.schema_proposal_id
        or int(journal["revision"]) != proposal.revision
        or str(journal["purpose"]) != SCHEMA_APPROVAL_PURPOSE
        or str(journal["status"]) != "unknown"
        or int(journal["attempts"]) != 1
        or not raw_database_id
        or not raw_data_source_id
    ):
        raise SchemaStateError("Schema recovery journal is not an exact UNKNOWN attempt")
    journal_proposal = _schema_proposal_from_dict(json.loads(journal["payload"]))
    if (
        journal_proposal.status is not SchemaProposalStatus.APPLYING
        or journal_proposal.digest != proposal.digest
        or journal_proposal.operation_id != proposal.operation_id
    ):
        raise SchemaStateError("Schema recovery journal payload differs")
    attempt_started_at = _schema_evidence_time(
        journal["attempt_started_at"],
        label="attempt start",
    )
    attempt_finished_at = _schema_evidence_time(
        journal["attempt_finished_at"],
        label="attempt finish",
    )
    journal_updated_at = _schema_evidence_time(
        journal["updated_at"],
        label="journal update time",
    )
    if attempt_finished_at < attempt_started_at or journal_updated_at < attempt_finished_at:
        raise SchemaStateError("Schema recovery journal time ordering is invalid")

    approval_rows = connection.execute(
        "SELECT * FROM schema_approval_receipt "
        "WHERE schema_proposal_id = ? AND revision = ? AND used_at IS NOT NULL",
        (proposal.schema_proposal_id, proposal.revision),
    ).fetchall()
    if len(approval_rows) != 1:
        raise SchemaStateError("Schema recovery requires one used create receipt")
    approval_row = approval_rows[0]
    approval_receipt = _schema_receipt_from_dict(json.loads(approval_row["payload"]))
    _assert_schema_receipt_matches_proposal(approval_receipt, proposal)
    if (
        str(approval_row["nonce"]) != approval_receipt.nonce
        or str(approval_row["purpose"]) != SCHEMA_APPROVAL_PURPOSE
        or int(approval_row["telegram_update_id"])
        != approval_receipt.telegram_update_id
        or str(approval_row["telegram_message_id"])
        != approval_receipt.telegram_message_id
        or str(approval_row["command_hash"]) != approval_receipt.command_hash
    ):
        raise SchemaStateError("Stored create receipt metadata differs")
    approval_claim = connection.execute(
        "SELECT * FROM schema_gateway_event_claim "
        "WHERE purpose = ? AND receipt_nonce = ?",
        (SCHEMA_APPROVAL_PURPOSE, approval_receipt.nonce),
    ).fetchone()
    if approval_claim is None:
        raise SchemaStateError("Create receipt is not linked to an approval event")
    expected_approval_hash = schema_approval_command_hash(
        proposal.schema_proposal_id,
        proposal.revision,
        proposal.digest,
    )
    if (
        str(approval_claim["command_name"]) != "nx_schema_approve"
        or str(approval_claim["status"]) != "completed"
        or str(approval_claim["schema_proposal_id"]) != proposal.schema_proposal_id
        or str(approval_claim["telegram_user_id"]) != proposal.requested_by
        or str(approval_claim["chat_id"]) != proposal.chat_id
        or str(approval_claim["thread_id"]) != proposal.thread_id
        or str(approval_claim["telegram_message_id"])
        != approval_receipt.telegram_message_id
        or int(approval_claim["telegram_update_id"])
        != approval_receipt.telegram_update_id
        or str(approval_claim["command_hash"]) != expected_approval_hash
        or str(approval_claim["receipt_nonce"]) != approval_receipt.nonce
        or not str(approval_claim["safe_result"] or "").startswith(
            "[NX_SCHEMA_UNKNOWN]"
        )
    ):
        raise SchemaStateError("Create approval event linkage is not exact")
    approval_claimed_at = _schema_evidence_time(
        approval_claim["claimed_at"],
        label="approval claim time",
    )
    approval_completed_at = _schema_evidence_time(
        approval_claim["completed_at"],
        label="approval completion time",
    )
    receipt_created_at = _schema_evidence_time(
        approval_row["created_at"],
        label="create receipt time",
    )
    receipt_used_at = _schema_evidence_time(
        approval_row["used_at"],
        label="create receipt use time",
    )
    issued_at = approval_receipt.issued_at.astimezone()
    expires_at = approval_receipt.expires_at.astimezone()
    if not (
        approval_claimed_at
        <= issued_at
        == receipt_created_at
        <= receipt_used_at
        < expires_at
    ):
        raise SchemaStateError("Create approval receipt time ordering is invalid")
    if not (
        receipt_used_at
        <= attempt_started_at
        <= attempt_finished_at
        <= approval_completed_at
    ):
        raise SchemaStateError("Create attempt time ordering is invalid")

    conflict_rows = connection.execute(
        "SELECT * FROM schema_gateway_event_claim "
        "WHERE purpose = ? AND command_name = 'nx_schema_recover' "
        "AND status = 'completed' AND telegram_user_id = ? AND chat_id = ? "
        "AND thread_id = ? AND schema_proposal_id = ? "
        "AND telegram_update_id <> ? ORDER BY completed_at DESC",
        (
            SCHEMA_APPROVAL_PURPOSE,
            proposal.requested_by,
            proposal.chat_id,
            proposal.thread_id,
            proposal.schema_proposal_id,
            telegram_update_id,
        ),
    ).fetchall()
    valid_conflicts: list[tuple[sqlite3.Row, datetime]] = []
    for conflict in conflict_rows:
        if not str(conflict["safe_result"] or "").startswith(
            "[NX_SCHEMA_RECOVERY_CONFLICT]"
        ):
            continue
        conflict_hashes = {
            legacy_schema_recovery_command_hash(
                proposal.schema_proposal_id,
                proposal.revision,
                proposal.digest,
                telegram_user_id=str(conflict["telegram_user_id"]),
                chat_id=str(conflict["chat_id"]),
                thread_id=str(conflict["thread_id"]),
                telegram_message_id=str(conflict["telegram_message_id"]),
                telegram_update_id=int(conflict["telegram_update_id"]),
            ),
            schema_recovery_command_hash(
                proposal.schema_proposal_id,
                proposal.revision,
                proposal.digest,
                telegram_user_id=str(conflict["telegram_user_id"]),
                chat_id=str(conflict["chat_id"]),
                thread_id=str(conflict["thread_id"]),
                telegram_message_id=str(conflict["telegram_message_id"]),
                telegram_update_id=int(conflict["telegram_update_id"]),
            ),
        }
        if str(conflict["command_hash"]) not in conflict_hashes:
            continue
        conflict_claimed_at = _schema_evidence_time(
            conflict["claimed_at"],
            label="conflict claim time",
        )
        conflict_completed_at = _schema_evidence_time(
            conflict["completed_at"],
            label="conflict completion time",
        )
        if not (
            journal_updated_at
            <= conflict_claimed_at
            <= conflict_completed_at
            <= current_claimed_at
        ):
            continue
        valid_conflicts.append((conflict, conflict_completed_at))
    if not valid_conflicts:
        raise SchemaStateError(
            "No exact prior completed schema recovery conflict attestation exists"
        )
    conflict, conflict_completed_at = valid_conflicts[0]

    return {
        "raw_database_id": raw_database_id,
        "raw_data_source_id": raw_data_source_id,
        "canonical_database_id": _canonical_schema_remote_id(raw_database_id),
        "canonical_data_source_id": _canonical_schema_remote_id(raw_data_source_id),
        "attempt_started_at": attempt_started_at,
        "attempt_finished_at": attempt_finished_at,
        "approval_receipt_nonce": approval_receipt.nonce,
        "conflict_telegram_update_id": int(conflict["telegram_update_id"]),
        "conflict_completed_at": conflict_completed_at,
        "current_claimed_at": current_claimed_at,
        "current_receipt_nonce": current_claim["receipt_nonce"],
    }
