from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, NamedTuple

from notion_excel_sync.models import (
    ApprovalReceipt,
    ChangeKind,
    EXCEL_SYNC_PURPOSE,
    KnowledgeRef,
    ProposalAction,
    ProposalOperation,
    ProposalPurpose,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    SCHEMA_APPROVAL_PURPOSE,
    SCHEMA_RECOVERY_PURPOSE,
    USER_CORRECTION_PURPOSE,
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
    source_basis_digest,
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
    purpose TEXT NOT NULL DEFAULT 'excel_sync/v1',
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

CREATE TABLE IF NOT EXISTS correction_gateway_event_claim (
    event_key TEXT PRIMARY KEY,
    telegram_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    request_digest TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (telegram_user_id, update_id),
    UNIQUE (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS mutation_scope_claim (
    scope_digest TEXT PRIMARY KEY,
    case_number TEXT NOT NULL,
    database_name TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    property_name TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    claim_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposal(proposal_id)
);

CREATE INDEX IF NOT EXISTS mutation_scope_claim_by_proposal
ON mutation_scope_claim(proposal_id, revision);

CREATE TABLE IF NOT EXISTS approved_override (
    scope_digest TEXT PRIMARY KEY,
    case_number TEXT NOT NULL,
    database_name TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    property_name TEXT NOT NULL,
    approved_value TEXT NOT NULL,
    source_basis_digest TEXT NOT NULL,
    observed_basis_digest TEXT NOT NULL,
    source_file_hash TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposal(proposal_id)
);

CREATE INDEX IF NOT EXISTS approved_override_by_entity
ON approved_override(database_name, entity_key, property_name);
"""


class ApplyLeaseError(RuntimeError):
    """Raised when another worker owns the source-scoped remote apply lease."""


class SchemaStateError(RuntimeError):
    """Raised when a schema workflow state transition is unsafe."""


class SchemaEventClaimError(SchemaStateError):
    """Raised when a Telegram update/message is replayed with changed content."""


class CorrectionEventClaimError(RuntimeError):
    """Raised when a correction command event is replayed inconsistently."""


class MutationScopeConflict(RuntimeError):
    """Raised when another live proposal owns the same Notion property."""


class ProposalInternalScopeConflict(MutationScopeConflict):
    """Raised when one proposal contains conflicting writes to one target."""


class ProposalRejectionError(RuntimeError):
    """Raised when an atomic proposal rejection cannot be completed safely."""


_HISTORY_DATABASE = "변경이력"
_ACTIVE_MUTATION_CLAIM_STATUSES = frozenset(
    {
        ProposalStatus.DRAFT,
        ProposalStatus.PENDING_APPROVAL,
        ProposalStatus.APPROVED,
        ProposalStatus.APPLYING,
        ProposalStatus.FAILED,
    }
)


class _PrecomputedMutationTarget(NamedTuple):
    identity: tuple[str, str]
    match_value: str


class _StoredMutationClaimOwner(NamedTuple):
    proposal: ProposalRevision
    operations: Mapping[
        tuple[str, str, str, str],
        ProposalOperation,
    ]
    targets: Mapping[int, _PrecomputedMutationTarget]


def _proposal_title_property(
    proposal: ProposalRevision,
    database_name: str,
) -> str | None:
    databases = proposal.notion_binding.get("databases")
    if isinstance(databases, dict):
        entry = databases.get(database_name)
        if isinstance(entry, dict):
            value = entry.get("title_property")
            if isinstance(value, str) and value:
                return value
    # Older and synthetic proposals may not carry a Notion binding. Import
    # lazily to avoid a module import cycle during database initialization.
    try:
        from notion_excel_sync.workflow.notion_writer import (
            NOTION_DEFINITIONS,
        )

        definition = NOTION_DEFINITIONS.get(database_name)
        return definition.title_property if definition is not None else None
    except ImportError:
        return None


def _canonical_mutation_identity(
    connection: sqlite3.Connection,
    proposal: ProposalRevision,
    operation: ProposalOperation,
) -> tuple[str, str]:
    """Resolve the same logical page identity used by the Notion writer.

    A known Notion page ID is strongest. Otherwise the exact title match value
    is used, including a title operation in the same proposal or the writer's
    ``prefix:value -> value`` fallback. Looking up mappings by match value also
    makes a mapped ``case:ABC`` alias collide with an unmapped ``ABC`` alias.
    """

    change = operation.change
    target = proposal.correction_binding.get("target")
    if (
        proposal.purpose is ProposalPurpose.USER_CORRECTION
        and isinstance(target, dict)
    ):
        page_id = target.get("page_id")
        if (
            isinstance(page_id, str)
            and page_id
            and target.get("database") == change.target_database
            and target.get("entity_key") == change.entity_key
            and target.get("property") == change.property_name
        ):
            return "notion_page", page_id
    direct = connection.execute(
        "SELECT notion_page_id FROM entity_mapping "
        "WHERE database_name = ? AND entity_key = ?",
        (change.target_database, change.entity_key),
    ).fetchone()
    if direct is not None and str(direct["notion_page_id"]):
        return "notion_page", str(direct["notion_page_id"])

    match_value = _writer_match_value(proposal, operation)
    mapped_rows = connection.execute(
        "SELECT DISTINCT notion_page_id FROM entity_mapping "
        "WHERE database_name = ? AND match_value = ? "
        "ORDER BY notion_page_id",
        (change.target_database, match_value),
    ).fetchall()
    mapped_page_ids = {
        str(row["notion_page_id"])
        for row in mapped_rows
        if str(row["notion_page_id"])
    }
    if len(mapped_page_ids) == 1:
        return "notion_page", next(iter(mapped_page_ids))
    return "match_value", match_value


def _writer_match_value(
    proposal: ProposalRevision,
    operation: ProposalOperation,
) -> str:
    """Return the title value the writer uses when no mapping is available."""

    change = operation.change
    if proposal.purpose is ProposalPurpose.USER_CORRECTION:
        case_number = proposal.correction_binding.get("case_number")
        if isinstance(case_number, str) and case_number.strip():
            return case_number
    title_property = _proposal_title_property(
        proposal,
        change.target_database,
    )
    title_value = next(
        (
            item.approved_value
            for item in proposal.operations
            if item.action in {ProposalAction.APPLY, ProposalAction.EDIT}
            and item.change.target_database == change.target_database
            and item.change.entity_key == change.entity_key
            and item.change.property_name == title_property
            and item.approved_value not in (None, "")
        ),
        None,
    )
    match_value = (
        str(title_value)
        if title_value not in (None, "")
        else change.entity_key.split(":", 1)[-1]
    )
    return match_value


def _precompute_mutation_targets(
    connection: sqlite3.Connection,
    proposal: ProposalRevision,
    operations: Iterable[ProposalOperation],
) -> dict[int, _PrecomputedMutationTarget]:
    """Resolve proposal-local match values and mappings in linear passes."""

    operation_list = list(operations)
    if not operation_list:
        return {}

    database_names = {
        operation.change.target_database
        for operation in operation_list
    }
    title_properties = {
        database_name: _proposal_title_property(
            proposal,
            database_name,
        )
        for database_name in database_names
    }
    title_values: dict[tuple[str, str], str] = {}
    for item in proposal.operations:
        change = item.change
        if (
            change.target_database not in database_names
            or item.action
            not in {ProposalAction.APPLY, ProposalAction.EDIT}
            or change.property_name
            != title_properties.get(change.target_database)
            or item.approved_value in (None, "")
        ):
            continue
        title_values.setdefault(
            (change.target_database, change.entity_key),
            str(item.approved_value),
        )

    match_values: dict[int, str] = {}
    correction_case_number = proposal.correction_binding.get(
        "case_number"
    )
    correction_match = (
        correction_case_number
        if (
            proposal.purpose is ProposalPurpose.USER_CORRECTION
            and isinstance(correction_case_number, str)
            and correction_case_number.strip()
        )
        else None
    )
    for operation in operation_list:
        change = operation.change
        match_values[id(operation)] = (
            correction_match
            or title_values.get(
                (change.target_database, change.entity_key),
            )
            or change.entity_key.split(":", 1)[-1]
        )

    direct_mappings: dict[tuple[str, str], str] = {}
    pages_by_match: dict[tuple[str, str], set[str]] = {}
    for row in connection.execute(
        "SELECT database_name, entity_key, notion_page_id, match_value "
        "FROM entity_mapping"
    ).fetchall():
        database_name = str(row["database_name"])
        if database_name not in database_names:
            continue
        page_id = str(row["notion_page_id"])
        if not page_id:
            continue
        direct_mappings[
            (database_name, str(row["entity_key"]))
        ] = page_id
        pages_by_match.setdefault(
            (database_name, str(row["match_value"])),
            set(),
        ).add(page_id)

    correction_target = proposal.correction_binding.get("target")
    targets: dict[int, _PrecomputedMutationTarget] = {}
    for operation in operation_list:
        change = operation.change
        operation_key = id(operation)
        match_value = match_values[operation_key]
        page_id: str | None = None
        if (
            proposal.purpose is ProposalPurpose.USER_CORRECTION
            and isinstance(correction_target, dict)
        ):
            bound_page_id = correction_target.get("page_id")
            if (
                isinstance(bound_page_id, str)
                and bound_page_id
                and correction_target.get("database")
                == change.target_database
                and correction_target.get("entity_key")
                == change.entity_key
                and correction_target.get("property")
                == change.property_name
            ):
                page_id = bound_page_id
        if page_id is None:
            page_id = direct_mappings.get(
                (change.target_database, change.entity_key)
            )
        if page_id is None:
            mapped_pages = pages_by_match.get(
                (change.target_database, match_value),
                set(),
            )
            if len(mapped_pages) == 1:
                page_id = next(iter(mapped_pages))
        identity = (
            ("notion_page", page_id)
            if page_id is not None
            else ("match_value", match_value)
        )
        targets[operation_key] = _PrecomputedMutationTarget(
            identity,
            match_value,
        )
    return targets


def _mutation_targets_overlap(
    first_identity: tuple[str, str],
    first_match_value: str,
    second_identity: tuple[str, str],
    second_match_value: str,
) -> bool:
    """Conservatively decide whether two writes may target the same page.

    Two distinct, known Notion page IDs are independent even when their title
    values match. When either side has only the writer's title match value,
    matching titles must be treated as the same target until both page IDs are
    known.
    """

    if first_identity == second_identity:
        return True
    if first_identity[0] == second_identity[0] == "notion_page":
        return False
    return first_match_value == second_match_value


def _stored_mutation_claim_target(
    claim: sqlite3.Row,
    owner: _StoredMutationClaimOwner,
) -> tuple[tuple[str, str], str]:
    """Resolve one persisted claim from its validated, precomputed owner."""

    key = (
        str(claim["operation_id"]),
        str(claim["database_name"]),
        str(claim["entity_key"]),
        str(claim["property_name"]),
    )
    operation = owner.operations.get(key)
    if operation is None:
        raise MutationScopeConflict(
            "Stored mutation-scope claim owner is inconsistent"
        )
    try:
        target = owner.targets[id(operation)]
    except (KeyError, TypeError, ValueError) as exc:
        raise MutationScopeConflict(
            "Stored mutation-scope claim owner is inconsistent"
        ) from exc
    return target.identity, target.match_value


def _precompute_stored_mutation_claim_owners(
    connection: sqlite3.Connection,
    claims: Iterable[sqlite3.Row],
) -> dict[tuple[str, int], _StoredMutationClaimOwner]:
    """Load and index each foreign claim owner exactly once."""

    claims_by_owner: dict[
        tuple[str, int],
        list[sqlite3.Row],
    ] = {}
    try:
        for claim in claims:
            owner_key = (
                str(claim["proposal_id"]),
                int(claim["revision"]),
            )
            claims_by_owner.setdefault(owner_key, []).append(claim)
    except (KeyError, TypeError, ValueError) as exc:
        raise MutationScopeConflict(
            "Stored mutation-scope claim owner is inconsistent"
        ) from exc

    owners: dict[tuple[str, int], _StoredMutationClaimOwner] = {}
    for owner_key, owner_claims in claims_by_owner.items():
        revision = connection.execute(
            "SELECT payload FROM proposal_revision "
            "WHERE proposal_id = ? AND revision = ?",
            owner_key,
        ).fetchone()
        if revision is None:
            raise MutationScopeConflict(
                "Stored mutation-scope claim owner is inconsistent"
            )
        try:
            proposal = _proposal_from_dict(
                json.loads(str(revision["payload"]))
            )
            if (
                proposal.proposal_id != owner_key[0]
                or proposal.revision != owner_key[1]
            ):
                raise MutationScopeConflict(
                    "Stored mutation-scope claim owner is inconsistent"
                )
            expected_keys = {
                (
                    str(claim["operation_id"]),
                    str(claim["database_name"]),
                    str(claim["entity_key"]),
                    str(claim["property_name"]),
                )
                for claim in owner_claims
            }
            operation_index: dict[
                tuple[str, str, str, str],
                ProposalOperation,
            ] = {}
            for operation in proposal.operations:
                change = operation.change
                operation_key = (
                    change.operation_id,
                    change.target_database,
                    change.entity_key,
                    change.property_name,
                )
                if operation_key not in expected_keys:
                    continue
                existing = operation_index.get(operation_key)
                if existing is None:
                    operation_index[operation_key] = operation
                    continue
                if existing.digest_payload() != operation.digest_payload():
                    raise MutationScopeConflict(
                        "Stored mutation-scope claim owner is inconsistent"
                    )
            if operation_index.keys() != expected_keys:
                raise MutationScopeConflict(
                    "Stored mutation-scope claim owner is inconsistent"
                )
            targets = _precompute_mutation_targets(
                connection,
                proposal,
                operation_index.values(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MutationScopeConflict(
                "Stored mutation-scope claim owner is inconsistent"
            ) from exc
        owners[owner_key] = _StoredMutationClaimOwner(
            proposal,
            operation_index,
            targets,
        )
    return owners


def _mutation_scope_digest(
    database_name: str,
    property_name: str,
    identity: tuple[str, str],
) -> str:
    return sha256_json(
        {
            "version": 2,
            "database": database_name,
            "entity_identity": {
                "kind": identity[0],
                "value": identity[1],
            },
            "property": property_name,
        }
    )


def _mutation_scope(
    connection: sqlite3.Connection,
    proposal: ProposalRevision,
    operation: ProposalOperation,
) -> tuple[str, str, str, str, str]:
    change = operation.change
    case_number = str(
        proposal.correction_binding.get("case_number")
        or change.entity_key
    )
    identity_kind, identity_value = _canonical_mutation_identity(
        connection,
        proposal,
        operation,
    )
    digest = _mutation_scope_digest(
        change.target_database,
        change.property_name,
        (identity_kind, identity_value),
    )
    return (
        digest,
        case_number,
        change.target_database,
        change.entity_key,
        change.property_name,
    )


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

    @staticmethod
    def _initialize_schema_and_migrations(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(SCHEMA)
        proposal_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(proposal)"
            ).fetchall()
        }
        if "purpose" not in proposal_columns:
            connection.execute(
                "ALTER TABLE proposal ADD COLUMN purpose TEXT NOT NULL "
                f"DEFAULT '{EXCEL_SYNC_PURPOSE}'"
            )
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

    def initialize(self) -> None:
        """Create/migrate state and fail closed while rebuilding active claims."""

        with self.session() as connection:
            self._initialize_schema_and_migrations(connection)
            self._backfill_active_mutation_scope_claims(connection)

    def initialize_for_legacy_rejection(self) -> None:
        """Create/migrate state without granting or rebuilding mutation claims.

        The trusted gateway may call this only for an exact authenticated
        rejection after normal initialization raised ``MutationScopeConflict``.
        """

        with self.session() as connection:
            self._initialize_schema_and_migrations(connection)

    def _backfill_active_mutation_scope_claims(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Deterministically claim every active legacy proposal head.

        This migration is intentionally fail closed. If two proposal heads
        resolve to the same canonical Notion page/property, initialization
        raises and rolls back the backfill instead of selecting a winner.
        """

        statuses = tuple(
            sorted(status.value for status in _ACTIVE_MUTATION_CLAIM_STATUSES)
        )
        placeholders = ",".join("?" for _ in statuses)
        rows = connection.execute(
            "SELECT p.proposal_id, p.current_revision, p.status, "
            "r.payload FROM proposal AS p "
            "JOIN proposal_revision AS r "
            "ON r.proposal_id = p.proposal_id "
            "AND r.revision = p.current_revision "
            f"WHERE p.status IN ({placeholders}) "
            "ORDER BY p.created_at, p.proposal_id",
            statuses,
        ).fetchall()
        now = datetime.now().astimezone().isoformat()
        for row in rows:
            proposal = _proposal_from_dict(
                json.loads(str(row["payload"]))
            )
            if (
                proposal.proposal_id != str(row["proposal_id"])
                or proposal.revision != int(row["current_revision"])
            ):
                raise MutationScopeConflict(
                    "Stored active proposal head is inconsistent"
                )
            proposal.status = ProposalStatus(str(row["status"]))
            self._synchronize_mutation_scope_claims(
                connection,
                proposal,
                now=now,
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
                        "Another approved synchronization owns this configured source "
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

    def get_approved_override(
        self,
        database_name: str,
        entity_key: str,
        property_name: str,
    ) -> dict[str, Any] | None:
        """Return the active, approval-backed value for one Notion property.

        This lookup is intentionally scoped only by the logical Notion
        mutation target. Whether the override can be reused for a new Excel
        observation is decided separately by comparing its stable
        ``source_basis_digest``.
        """

        change = ProposedChange(
            target_database=database_name,
            entity_key=entity_key,
            property_name=property_name,
            kind=ChangeKind.UPDATE,
            current_value=None,
            proposed_value=None,
            analyzer="approved_override_lookup",
            analyzer_version="1",
            confidence=1.0,
            reason="local override lookup",
            source_refs=[],
        )
        operation = ProposalOperation(change=change)
        proposal = ProposalRevision(
            proposal_id="approved-override-lookup",
            revision=1,
            source_version_id="lookup",
            source_file_hash="lookup",
            requested_by="local",
            chat_id="local",
            operations=[operation],
        )
        return self.get_approved_override_for_operation(
            proposal,
            operation,
        )

    def _stored_override_identity(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> tuple[str, str, str]:
        return self._precompute_stored_override_identities(
            connection,
            [row],
        )[str(row["scope_digest"])]

    def _precompute_stored_override_identities(
        self,
        connection: sqlite3.Connection,
        rows: Iterable[sqlite3.Row],
    ) -> dict[str, tuple[str, str, str]]:
        """Resolve stored override targets once per owner proposal revision."""

        row_list = list(rows)
        identities: dict[str, tuple[str, str, str]] = {}
        rows_by_owner: dict[
            tuple[str, int],
            list[sqlite3.Row],
        ] = {}
        try:
            for row in row_list:
                rows_by_owner.setdefault(
                    (
                        str(row["proposal_id"]),
                        int(row["revision"]),
                    ),
                    [],
                ).append(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise MutationScopeConflict(
                "Stored approved override owner is inconsistent"
            ) from exc

        def use_weak_fallback(owner_rows: Iterable[sqlite3.Row]) -> None:
            for owner_row in owner_rows:
                match_value = str(owner_row["entity_key"]).split(
                    ":",
                    1,
                )[-1]
                identities[str(owner_row["scope_digest"])] = (
                    "match_value",
                    match_value,
                    match_value,
                )

        for owner_key, owner_rows in rows_by_owner.items():
            revision = connection.execute(
                "SELECT payload FROM proposal_revision "
                "WHERE proposal_id = ? AND revision = ?",
                owner_key,
            ).fetchone()
            if revision is None:
                use_weak_fallback(owner_rows)
                continue
            try:
                proposal = _proposal_from_dict(
                    json.loads(str(revision["payload"]))
                )
                if (
                    proposal.proposal_id != owner_key[0]
                    or proposal.revision != owner_key[1]
                ):
                    use_weak_fallback(owner_rows)
                    continue
                operation_ids = {
                    str(owner_row["operation_id"])
                    for owner_row in owner_rows
                }
                operation_index: dict[str, ProposalOperation] = {}
                for operation in proposal.operations:
                    operation_id = operation.change.operation_id
                    if operation_id in operation_ids:
                        operation_index.setdefault(
                            operation_id,
                            operation,
                        )
                relevant_operations = list(operation_index.values())
                targets = _precompute_mutation_targets(
                    connection,
                    proposal,
                    relevant_operations,
                )
                for owner_row in owner_rows:
                    operation = operation_index.get(
                        str(owner_row["operation_id"])
                    )
                    if operation is None:
                        use_weak_fallback([owner_row])
                        continue
                    target = targets[id(operation)]
                    identities[str(owner_row["scope_digest"])] = (
                        target.identity[0],
                        target.identity[1],
                        target.match_value,
                    )
            except (KeyError, TypeError, ValueError):
                use_weak_fallback(owner_rows)
        return identities

    def _find_approved_override(
        self,
        connection: sqlite3.Connection,
        proposal: ProposalRevision,
        operation: ProposalOperation,
        *,
        candidate_rows: Iterable[sqlite3.Row] | None = None,
        current_target: _PrecomputedMutationTarget | None = None,
        stored_identity_cache: (
            dict[str, tuple[str, str, str]] | None
        ) = None,
        mapped_pages_cache: (
            dict[tuple[str, str], set[str]] | None
        ) = None,
    ) -> sqlite3.Row | None:
        change = operation.change
        if current_target is None:
            current_target = _precompute_mutation_targets(
                connection,
                proposal,
                [operation],
            )[id(operation)]
        current_identity = current_target.identity
        current_match_value = current_target.match_value
        scope_digest = _mutation_scope_digest(
            change.target_database,
            change.property_name,
            current_identity,
        )
        rows = (
            list(candidate_rows)
            if candidate_rows is not None
            else connection.execute(
                "SELECT * FROM approved_override "
                "WHERE database_name = ? AND property_name = ? "
                "ORDER BY updated_at DESC, scope_digest",
                (change.target_database, change.property_name),
            ).fetchall()
        )
        candidates: list[sqlite3.Row] = []
        for row in rows:
            if str(row["scope_digest"]) == scope_digest:
                candidates.append(row)
                continue
            row_digest = str(row["scope_digest"])
            stored_identity = (
                stored_identity_cache.get(row_digest)
                if stored_identity_cache is not None
                else None
            )
            if stored_identity is None:
                stored_identity = self._stored_override_identity(
                    connection,
                    row,
                )
                if stored_identity_cache is not None:
                    stored_identity_cache[row_digest] = stored_identity
            stored_kind, stored_value, stored_match = stored_identity
            if stored_match != current_match_value:
                continue
            current_kind, current_value = current_identity
            if current_kind == stored_kind == "notion_page":
                if current_value == stored_value:
                    candidates.append(row)
                continue
            if current_kind == "match_value":
                candidates.append(row)
                continue
            if stored_kind == "match_value":
                mapping_key = (
                    change.target_database,
                    current_match_value,
                )
                mapped_pages = (
                    mapped_pages_cache.get(mapping_key)
                    if mapped_pages_cache is not None
                    else None
                )
                if mapped_pages is None:
                    mapped_pages = {
                        str(mapped["notion_page_id"])
                        for mapped in connection.execute(
                            "SELECT DISTINCT notion_page_id "
                            "FROM entity_mapping WHERE database_name = ? "
                            "AND match_value = ?",
                            mapping_key,
                        ).fetchall()
                    }
                    if mapped_pages_cache is not None:
                        mapped_pages_cache[mapping_key] = mapped_pages
                if mapped_pages == {current_value}:
                    candidates.append(row)
        if not candidates:
            return None
        signatures = {
            (
                str(row["approved_value"]),
                str(row["source_basis_digest"]),
            )
            for row in candidates
        }
        if len(signatures) != 1:
            raise MutationScopeConflict(
                "Conflicting approved overrides resolve to the same "
                "Notion page/property"
            )
        return candidates[0]

    def get_approved_overrides_for_operations(
        self,
        proposal: ProposalRevision,
        operations: Iterable[ProposalOperation],
    ) -> list[dict[str, Any] | None]:
        """Resolve source-value overrides in one state DB read session."""

        operation_list = list(operations)
        if not operation_list:
            return []
        results: list[dict[str, Any] | None] = [
            None
            for _ in operation_list
        ]
        applicable = [
            operation
            for operation in operation_list
            if operation.change.target_database != _HISTORY_DATABASE
        ]
        if not applicable:
            return results
        with self.session() as connection:
            rows = connection.execute(
                "SELECT * FROM approved_override "
                "ORDER BY updated_at DESC, scope_digest"
            ).fetchall()
            if not rows:
                return results
            relevant_buckets = {
                (
                    operation.change.target_database,
                    operation.change.property_name,
                )
                for operation in applicable
            }
            relevant_rows = [
                row
                for row in rows
                if (
                    str(row["database_name"]),
                    str(row["property_name"]),
                )
                in relevant_buckets
            ]
            if not relevant_rows:
                return results
            available_buckets = {
                (
                    str(row["database_name"]),
                    str(row["property_name"]),
                )
                for row in relevant_rows
            }
            relevant = [
                operation
                for operation in applicable
                if (
                    operation.change.target_database,
                    operation.change.property_name,
                )
                in available_buckets
            ]
            targets = _precompute_mutation_targets(
                connection,
                proposal,
                relevant,
            )
            stored_identity_cache = (
                self._precompute_stored_override_identities(
                    connection,
                    relevant_rows,
                )
            )
            row_order = {
                str(row["scope_digest"]): index
                for index, row in enumerate(relevant_rows)
            }
            exact_rows: dict[
                tuple[str, str, str],
                list[sqlite3.Row],
            ] = {}
            all_match_rows: dict[
                tuple[str, str, str],
                list[sqlite3.Row],
            ] = {}
            strong_match_rows: dict[
                tuple[str, str, str, str],
                list[sqlite3.Row],
            ] = {}
            weak_match_rows: dict[
                tuple[str, str, str],
                list[sqlite3.Row],
            ] = {}
            for row in relevant_rows:
                database_name = str(row["database_name"])
                property_name = str(row["property_name"])
                scope_digest = str(row["scope_digest"])
                stored_kind, stored_value, stored_match = (
                    stored_identity_cache[scope_digest]
                )
                exact_rows.setdefault(
                    (database_name, property_name, scope_digest),
                    [],
                ).append(row)
                match_key = (
                    database_name,
                    property_name,
                    stored_match,
                )
                all_match_rows.setdefault(match_key, []).append(row)
                if stored_kind == "notion_page":
                    strong_match_rows.setdefault(
                        (*match_key, stored_value),
                        [],
                    ).append(row)
                else:
                    weak_match_rows.setdefault(match_key, []).append(row)

            mapped_pages_cache: dict[
                tuple[str, str],
                set[str],
            ] = {
                (database_name, match_value): set()
                for database_name, _property_name, match_value
                in weak_match_rows
            }
            if mapped_pages_cache:
                for mapping in connection.execute(
                    "SELECT database_name, match_value, notion_page_id "
                    "FROM entity_mapping"
                ).fetchall():
                    mapping_key = (
                        str(mapping["database_name"]),
                        str(mapping["match_value"]),
                    )
                    if mapping_key in mapped_pages_cache:
                        page_id = str(mapping["notion_page_id"])
                        if page_id:
                            mapped_pages_cache[mapping_key].add(page_id)

            for index, operation in enumerate(operation_list):
                change = operation.change
                if (
                    change.target_database == _HISTORY_DATABASE
                    or (
                        change.target_database,
                        change.property_name,
                    )
                    not in available_buckets
                ):
                    continue
                target = targets[id(operation)]
                scope_digest = _mutation_scope_digest(
                    change.target_database,
                    change.property_name,
                    target.identity,
                )
                match_key = (
                    change.target_database,
                    change.property_name,
                    target.match_value,
                )
                candidate_groups: list[list[sqlite3.Row]] = [
                    exact_rows.get(
                        (
                            change.target_database,
                            change.property_name,
                            scope_digest,
                        ),
                        [],
                    )
                ]
                if target.identity[0] == "notion_page":
                    candidate_groups.extend(
                        (
                            strong_match_rows.get(
                                (*match_key, target.identity[1]),
                                [],
                            ),
                            weak_match_rows.get(match_key, []),
                        )
                    )
                else:
                    candidate_groups.append(
                        all_match_rows.get(match_key, [])
                    )
                candidate_rows_by_digest = {
                    str(candidate["scope_digest"]): candidate
                    for group in candidate_groups
                    for candidate in group
                }
                candidate_rows = sorted(
                    candidate_rows_by_digest.values(),
                    key=lambda candidate: row_order[
                        str(candidate["scope_digest"])
                    ],
                )
                row = self._find_approved_override(
                    connection,
                    proposal,
                    operation,
                    candidate_rows=candidate_rows,
                    current_target=target,
                    stored_identity_cache=stored_identity_cache,
                    mapped_pages_cache=mapped_pages_cache,
                )
                if row is None:
                    continue
                result = dict(row)
                result["approved_value"] = json.loads(
                    str(result["approved_value"])
                )
                results[index] = result
        return results

    def get_approved_override_for_operation(
        self,
        proposal: ProposalRevision,
        operation: ProposalOperation,
    ) -> dict[str, Any] | None:
        """Resolve an override through canonical page and title identities."""

        return self.get_approved_overrides_for_operations(
            proposal,
            [operation],
        )[0]

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

    def claim_correction_gateway_event(
        self,
        *,
        telegram_user_id: str,
        chat_id: str,
        message_id: str,
        update_id: int,
        request_digest: str,
        proposal_id: str,
    ) -> dict[str, Any]:
        """Claim one exact Telegram correction event idempotently.

        A Telegram retry with the same event and canonical request returns the
        existing claim. Reuse of either the update ID or message ID with
        different content fails closed.
        """

        values = {
            "telegram_user_id": str(telegram_user_id).strip(),
            "chat_id": str(chat_id).strip(),
            "message_id": str(message_id).strip(),
            "proposal_id": str(proposal_id).strip(),
        }
        if any(not value or len(value) > 256 for value in values.values()):
            raise ValueError("Correction event identifiers are invalid")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise ValueError("Correction Telegram update_id is invalid")
        digest = str(request_digest).strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Correction request digest must be SHA-256")
        event_key = sha256_json(
            {
                "purpose": USER_CORRECTION_PURPOSE,
                "telegram_user_id": values["telegram_user_id"],
                "chat_id": values["chat_id"],
                "message_id": values["message_id"],
                "update_id": update_id,
            }
        )
        now = datetime.now().astimezone().isoformat()
        expected = {
            "event_key": event_key,
            **values,
            "update_id": update_id,
            "request_digest": digest,
        }
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM correction_gateway_event_claim
                WHERE event_key = ?
                   OR (telegram_user_id = ? AND update_id = ?)
                   OR (chat_id = ? AND message_id = ?)
                """,
                (
                    event_key,
                    values["telegram_user_id"],
                    update_id,
                    values["chat_id"],
                    values["message_id"],
                ),
            ).fetchone()
            if row is not None:
                existing = dict(row)
                if any(
                    str(existing[key]) != str(value)
                    for key, value in expected.items()
                ):
                    raise CorrectionEventClaimError(
                        "Telegram correction event was replayed with changed content"
                    )
                return existing
            connection.execute(
                """
                INSERT INTO correction_gateway_event_claim(
                    event_key, telegram_user_id, chat_id, message_id, update_id,
                    request_digest, proposal_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    values["telegram_user_id"],
                    values["chat_id"],
                    values["message_id"],
                    update_id,
                    digest,
                    values["proposal_id"],
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    "correction_gateway_event_claimed",
                    values["proposal_id"],
                    values["telegram_user_id"],
                    json.dumps(
                        {
                            "event_key": event_key,
                            "request_digest": digest,
                            "telegram_update_id": update_id,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        return {**expected, "created_at": now}

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

    def _desired_mutation_scope_claims(
        self,
        connection: sqlite3.Connection,
        proposal: ProposalRevision,
    ) -> dict[
        str,
        tuple[
            ProposalOperation,
            str,
            str,
            str,
            str,
            tuple[str, str],
            str,
        ],
    ]:
        desired: dict[
            str,
            tuple[
                ProposalOperation,
                str,
                str,
                str,
                str,
                tuple[str, str],
                str,
            ],
        ] = {}
        completed_outbox_operations: set[str] = set()
        if any(
            operation.action is ProposalAction.EXCLUDE
            and operation.user_override
            for operation in proposal.operations
        ):
            completed_outbox_operations = {
                str(row["operation_id"])
                for row in connection.execute(
                    "SELECT operation_id FROM outbox WHERE status = 'done'"
                ).fetchall()
            }

        candidate_operations: list[ProposalOperation] = []
        for operation in proposal.operations:
            change = operation.change
            if change.target_database == _HISTORY_DATABASE:
                continue
            retain_completed_override = (
                operation.action is ProposalAction.EXCLUDE
                and operation.user_override
                and change.operation_id in completed_outbox_operations
            )
            if (
                operation.action is ProposalAction.EXCLUDE
                and not retain_completed_override
            ):
                continue
            candidate_operations.append(operation)

        targets = _precompute_mutation_targets(
            connection,
            proposal,
            candidate_operations,
        )
        all_match_scopes: set[tuple[str, str, str]] = set()
        weak_match_scopes: set[tuple[str, str, str]] = set()
        for operation in candidate_operations:
            change = operation.change
            target = targets[id(operation)]
            identity = target.identity
            match_value = target.match_value
            database_name = change.target_database
            entity_key = change.entity_key
            property_name = change.property_name
            case_number = str(
                proposal.correction_binding.get("case_number")
                or entity_key
            )
            scope_digest = _mutation_scope_digest(
                database_name,
                property_name,
                identity,
            )
            if scope_digest in desired:
                previous = desired[scope_digest][0]
                if (
                    previous.change.operation_id
                    == operation.change.operation_id
                    and previous.digest_payload()
                    == operation.digest_payload()
                ):
                    # An exact deterministic duplicate has one stable outbox
                    # identity and one semantic mutation, so one external
                    # claim is sufficient.
                    continue
                raise ProposalInternalScopeConflict(
                    "One proposal contains multiple non-identical mutations "
                    f"for {database_name}/{entity_key}/{property_name}"
                )
            match_scope = (
                database_name,
                property_name,
                match_value,
            )
            weak_identity = identity[0] != "notion_page"
            overlapping_match = (
                match_scope in all_match_scopes
                if weak_identity
                else match_scope in weak_match_scopes
            )
            if overlapping_match:
                raise ProposalInternalScopeConflict(
                    "One proposal contains multiple non-identical "
                    "mutations for "
                    f"{database_name}/{entity_key}/{property_name}"
                )
            desired[scope_digest] = (
                operation,
                case_number,
                database_name,
                entity_key,
                property_name,
                identity,
                match_value,
            )
            all_match_scopes.add(match_scope)
            if weak_identity:
                weak_match_scopes.add(match_scope)
        return desired

    def _synchronize_mutation_scope_claims(
        self,
        connection: sqlite3.Connection,
        proposal: ProposalRevision,
        *,
        now: str,
    ) -> None:
        if proposal.status not in _ACTIVE_MUTATION_CLAIM_STATUSES:
            connection.execute(
                "DELETE FROM mutation_scope_claim WHERE proposal_id = ?",
                (proposal.proposal_id,),
            )
            return

        desired = self._desired_mutation_scope_claims(
            connection,
            proposal,
        )

        existing_owned = connection.execute(
            "SELECT scope_digest FROM mutation_scope_claim "
            "WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchall()
        stale_digests = [
            str(row["scope_digest"])
            for row in existing_owned
            if str(row["scope_digest"]) not in desired
        ]
        if stale_digests:
            # Do not construct one unbounded ``NOT IN`` expression. Production
            # SQLite builds may retain the legacy 999-variable limit, while an
            # initial full reconcile can legitimately claim many thousands of
            # independent Notion properties.
            connection.executemany(
                "DELETE FROM mutation_scope_claim "
                "WHERE proposal_id = ? AND scope_digest = ?",
                (
                    (proposal.proposal_id, scope_digest)
                    for scope_digest in stale_digests
                ),
            )

        claim_state = (
            "recovery"
            if proposal.status is ProposalStatus.FAILED
            else proposal.status.value
        )
        relevant_buckets = {
            (claim[2], claim[4])
            for claim in desired.values()
        }
        claim_rows = connection.execute(
            "SELECT * FROM mutation_scope_claim ORDER BY scope_digest"
        ).fetchall()
        existing_by_digest = {
            str(row["scope_digest"]): row
            for row in claim_rows
        }
        exact_identity_claims: dict[
            tuple[str, str, tuple[str, str]],
            set[str],
        ] = {}
        all_match_claims: dict[
            tuple[str, str, str],
            set[str],
        ] = {}
        weak_match_claims: dict[
            tuple[str, str, str],
            set[str],
        ] = {}
        foreign_claim_rows = [
            row
            for row in claim_rows
            if str(row["proposal_id"]) != proposal.proposal_id
            and (
                str(row["database_name"]),
                str(row["property_name"]),
            )
            in relevant_buckets
        ]
        foreign_owners = _precompute_stored_mutation_claim_owners(
            connection,
            foreign_claim_rows,
        )
        for row in foreign_claim_rows:
            database_name = str(row["database_name"])
            property_name = str(row["property_name"])
            try:
                owner_key = (
                    str(row["proposal_id"]),
                    int(row["revision"]),
                )
                owner = foreign_owners[owner_key]
            except (KeyError, TypeError, ValueError) as exc:
                raise MutationScopeConflict(
                    "Stored mutation-scope claim owner is inconsistent"
                ) from exc
            identity, match_value = _stored_mutation_claim_target(
                row,
                owner,
            )
            scope_digest = str(row["scope_digest"])
            exact_identity_claims.setdefault(
                (database_name, property_name, identity),
                set(),
            ).add(scope_digest)
            match_key = (
                database_name,
                property_name,
                match_value,
            )
            all_match_claims.setdefault(match_key, set()).add(
                scope_digest
            )
            if identity[0] != "notion_page":
                weak_match_claims.setdefault(match_key, set()).add(
                    scope_digest
                )

        for scope_digest, (
            operation,
            case_number,
            database_name,
            entity_key,
            property_name,
            identity,
            match_value,
        ) in desired.items():
            exact_conflicts = exact_identity_claims.get(
                (database_name, property_name, identity),
                set(),
            )
            match_key = (
                database_name,
                property_name,
                match_value,
            )
            match_conflicts = (
                all_match_claims.get(match_key, set())
                if identity[0] != "notion_page"
                else weak_match_claims.get(match_key, set())
            )
            if (
                exact_conflicts - {scope_digest}
                or match_conflicts - {scope_digest}
            ):
                raise MutationScopeConflict(
                    "Another active proposal already owns "
                    f"{database_name}/{entity_key}/{property_name}"
                )
            existing = existing_by_digest.get(scope_digest)
            if existing is not None:
                same_proposal = (
                    str(existing["proposal_id"]) == proposal.proposal_id
                )
                pending_transfer = (
                    str(existing["claim_state"]) == "pending"
                    and str(existing["operation_id"])
                    == operation.change.operation_id
                )
                if not same_proposal and not pending_transfer:
                    raise MutationScopeConflict(
                        "Another active proposal already owns "
                        f"{database_name}/{entity_key}/{property_name}"
                    )
                connection.execute(
                    """
                    UPDATE mutation_scope_claim SET
                        case_number = ?, database_name = ?, entity_key = ?,
                        property_name = ?, operation_id = ?, proposal_id = ?,
                        revision = ?, claim_state = ?, updated_at = ?
                    WHERE scope_digest = ?
                    """,
                    (
                        case_number,
                        database_name,
                        entity_key,
                        property_name,
                        operation.change.operation_id,
                        proposal.proposal_id,
                        proposal.revision,
                        claim_state,
                        now,
                        scope_digest,
                    ),
                )
                continue
            connection.execute(
                """
                INSERT INTO mutation_scope_claim(
                    scope_digest, case_number, database_name, entity_key,
                    property_name, operation_id, proposal_id, revision,
                    claim_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_digest,
                    case_number,
                    database_name,
                    entity_key,
                    property_name,
                    operation.change.operation_id,
                    proposal.proposal_id,
                    proposal.revision,
                    claim_state,
                    now,
                    now,
                ),
            )

    def save_proposal(self, proposal: ProposalRevision) -> None:
        payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO proposal(
                    proposal_id, current_revision, purpose, requested_by, chat_id,
                    source_version_id, source_file_hash, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    current_revision = excluded.current_revision,
                    purpose = excluded.purpose,
                    source_version_id = excluded.source_version_id,
                    source_file_hash = excluded.source_file_hash,
                    status = excluded.status,
                    expires_at = excluded.expires_at
                """,
                (
                    proposal.proposal_id,
                    proposal.revision,
                    proposal.purpose.value,
                    proposal.requested_by,
                    proposal.chat_id,
                    proposal.source_version_id,
                    proposal.source_file_hash,
                    proposal.status.value,
                    proposal.created_at.isoformat(),
                    proposal.expires_at.isoformat() if proposal.expires_at else None,
                ),
            )
            self._synchronize_mutation_scope_claims(
                connection,
                proposal,
                now=now,
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

    def reject_proposal_atomically(
        self,
        proposal_id: str,
        *,
        expected_revision: int,
        actor: str,
        chat_id: str,
        gateway_audit_details: dict[str, Any] | None = None,
    ) -> ProposalRevision:
        """Reject one exact proposal head without deriving mutation scopes."""

        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            head = connection.execute(
                "SELECT * FROM proposal WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if head is None:
                raise ProposalRejectionError("Proposal was not found")
            current_revision = int(head["current_revision"])
            if current_revision != expected_revision:
                raise ProposalRejectionError(
                    "Only the current proposal revision can be rejected"
                )
            if str(head["requested_by"]) != str(actor):
                raise ProposalRejectionError(
                    "Only the request owner can reject the proposal"
                )
            if str(head["chat_id"]) != str(chat_id):
                raise ProposalRejectionError(
                    "Proposal must be rejected from the original Telegram chat"
                )

            revision_row = connection.execute(
                "SELECT digest, payload FROM proposal_revision "
                "WHERE proposal_id = ? AND revision = ?",
                (proposal_id, current_revision),
            ).fetchone()
            if revision_row is None:
                raise ProposalRejectionError(
                    "Current proposal revision is missing"
                )
            original_payload = str(revision_row["payload"])
            stored_digest = str(revision_row["digest"])
            try:
                proposal = _proposal_from_dict(json.loads(original_payload))
                head_status = ProposalStatus(str(head["status"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProposalRejectionError(
                    "Current proposal state is invalid"
                ) from exc

            expected_expires_at = (
                proposal.expires_at.isoformat()
                if proposal.expires_at is not None
                else None
            )
            consistent = (
                proposal.proposal_id == proposal_id
                and proposal.revision == current_revision
                and proposal.requested_by == str(head["requested_by"])
                and proposal.chat_id == str(head["chat_id"])
                and proposal.source_version_id
                == str(head["source_version_id"])
                and proposal.source_file_hash
                == str(head["source_file_hash"])
                and proposal.purpose.value == str(head["purpose"])
                and proposal.status is head_status
                and expected_expires_at == head["expires_at"]
                and proposal.digest == stored_digest
            )
            if not consistent:
                raise ProposalRejectionError(
                    "Proposal head and current revision are inconsistent"
                )
            if head_status in {
                ProposalStatus.APPLYING,
                ProposalStatus.COMMITTED,
                ProposalStatus.REJECTED,
            }:
                raise ProposalRejectionError(
                    f"Proposal cannot be rejected in {head_status.value}"
                )
            if connection.execute(
                "SELECT 1 FROM outbox WHERE proposal_id = ? LIMIT 1",
                (proposal_id,),
            ).fetchone() is not None:
                raise ProposalRejectionError(
                    "A proposal with a started apply must be recovered and "
                    "finalized before it can close"
                )
            if connection.execute(
                "SELECT 1 FROM source_apply_lease "
                "WHERE proposal_id = ? LIMIT 1",
                (proposal_id,),
            ).fetchone() is not None:
                raise ProposalRejectionError(
                    "A proposal with an active apply lease must be recovered "
                    "and finalized before it can close"
                )
            if connection.execute(
                "SELECT 1 FROM approval_receipt WHERE proposal_id = ? "
                "AND used_at IS NOT NULL LIMIT 1",
                (proposal_id,),
            ).fetchone() is not None:
                raise ProposalRejectionError(
                    "A proposal with a consumed approval must be recovered "
                    "and finalized before it can close"
                )

            proposal.status = ProposalStatus.REJECTED
            rejected_payload = json.dumps(
                dataclass_to_dict(proposal),
                ensure_ascii=False,
            )
            head_cursor = connection.execute(
                "UPDATE proposal SET status = ? WHERE proposal_id = ? "
                "AND current_revision = ? AND status = ? "
                "AND requested_by = ? AND chat_id = ?",
                (
                    ProposalStatus.REJECTED.value,
                    proposal_id,
                    current_revision,
                    head_status.value,
                    str(actor),
                    str(chat_id),
                ),
            )
            revision_cursor = connection.execute(
                "UPDATE proposal_revision SET payload = ? "
                "WHERE proposal_id = ? AND revision = ? "
                "AND digest = ? AND payload = ?",
                (
                    rejected_payload,
                    proposal_id,
                    current_revision,
                    stored_digest,
                    original_payload,
                ),
            )
            if head_cursor.rowcount != 1 or revision_cursor.rowcount != 1:
                raise ProposalRejectionError(
                    "Proposal state changed before rejection completed"
                )
            connection.execute(
                "DELETE FROM mutation_scope_claim WHERE proposal_id = ?",
                (proposal_id,),
            )
            connection.execute(
                "INSERT INTO audit_log("
                "event_type, proposal_id, actor, details, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    "proposal_rejected",
                    proposal_id,
                    str(actor),
                    json.dumps(
                        {"revision": current_revision},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            if gateway_audit_details is not None:
                connection.execute(
                    "INSERT INTO audit_log("
                    "event_type, proposal_id, actor, details, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        "telegram_gateway_command",
                        proposal_id,
                        str(actor),
                        json.dumps(
                            gateway_audit_details,
                            ensure_ascii=False,
                            default=str,
                        ),
                        now,
                    ),
                )
        return proposal

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
            if status not in _ACTIVE_MUTATION_CLAIM_STATUSES:
                connection.execute(
                    "DELETE FROM mutation_scope_claim WHERE proposal_id = ?",
                    (proposal_id,),
                )
            else:
                connection.execute(
                    "UPDATE mutation_scope_claim SET claim_state = ?, "
                    "updated_at = ? WHERE proposal_id = ?",
                    (
                        (
                            "recovery"
                            if status is ProposalStatus.FAILED
                            else status.value
                        ),
                        datetime.now().astimezone().isoformat(),
                        proposal_id,
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

    def _assert_mutation_scope_claims_owned(
        self,
        connection: sqlite3.Connection,
        proposal: ProposalRevision,
    ) -> None:
        """Fail before approval consumption if any write scope lost ownership."""

        desired = self._desired_mutation_scope_claims(
            connection,
            proposal,
        )
        owned_rows = connection.execute(
            "SELECT scope_digest, revision, claim_state "
            "FROM mutation_scope_claim WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchall()
        owned = {str(row["scope_digest"]): row for row in owned_rows}
        if set(owned) != set(desired):
            raise MutationScopeConflict(
                "Proposal mutation-scope ownership is incomplete"
            )
        for row in owned.values():
            if (
                int(row["revision"]) != proposal.revision
                or str(row["claim_state"])
                != ProposalStatus.APPROVED.value
            ):
                raise MutationScopeConflict(
                    "Proposal mutation-scope ownership changed before apply"
                )

    def begin_apply(self, proposal: ProposalRevision, nonce: str) -> None:
        """Atomically consume approval, enter APPLYING, and create the outbox."""

        now = datetime.now().astimezone().isoformat()
        payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        with self.transaction() as connection:
            self._assert_mutation_scope_claims_owned(
                connection,
                proposal,
            )
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
                "UPDATE mutation_scope_claim SET claim_state = 'applying', "
                "revision = ?, updated_at = ? WHERE proposal_id = ?",
                (proposal.revision, now, proposal.proposal_id),
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
                    if (
                        operation.change.target_database
                        != _HISTORY_DATABASE
                    ):
                        connection.execute(
                            "UPDATE mutation_scope_claim SET "
                            "claim_state = 'pending', operation_id = ?, "
                            "revision = ?, updated_at = ? "
                            "WHERE proposal_id = ? AND operation_id = ?",
                            (
                                operation.change.operation_id,
                                proposal.revision,
                                now,
                                proposal.proposal_id,
                                operation.change.operation_id,
                            ),
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

    def outbox_statuses(
        self,
        proposal_id: str,
        revision: int,
    ) -> dict[str, str]:
        """Load one proposal revision's outbox in a single read session."""

        with self.session() as connection:
            rows = connection.execute(
                "SELECT operation_id, status FROM outbox "
                "WHERE proposal_id = ? AND revision = ?",
                (proposal_id, revision),
            ).fetchall()
        return {
            str(row["operation_id"]): str(row["status"])
            for row in rows
        }

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

    @staticmethod
    def _validate_outbox_batch_ids(operation_ids: Iterable[str]) -> tuple[str, ...]:
        items = tuple(operation_ids)
        if not items or any(not item.strip() for item in items):
            raise ValueError("Outbox batch operation IDs must be non-empty")
        if len(set(items)) != len(items):
            raise ValueError("Outbox batch operation IDs must be unique")
        return items

    def mark_outbox_batch(
        self,
        proposal_id: str,
        revision: int,
        operation_ids: Iterable[str],
        status: str,
        error: str | None = None,
        *,
        entity_mapping: Mapping[str, str] | None = None,
        allow_reconciliation: bool = False,
    ) -> None:
        """Atomically transition a page batch and its optional page mapping."""

        if status not in {"done", "failed", "blocked"}:
            raise ValueError("Unsupported outbox batch status")
        items = self._validate_outbox_batch_ids(operation_ids)
        mapping: dict[str, str] | None = None
        if entity_mapping is not None:
            mapping = dict(entity_mapping)
            expected_keys = {
                "database_name",
                "entity_key",
                "notion_page_id",
                "match_value",
            }
            if (
                set(mapping) != expected_keys
                or any(not mapping[key].strip() for key in expected_keys)
                or status != "done"
            ):
                raise ValueError("Entity mapping is invalid for an outbox batch")
        now = datetime.now().astimezone().isoformat()
        placeholders = ",".join("?" for _ in items)
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT operation_id, status FROM outbox "
                "WHERE proposal_id = ? AND revision = ? "
                f"AND operation_id IN ({placeholders})",
                (proposal_id, revision, *items),
            ).fetchall()
            states = {
                str(row["operation_id"]): str(row["status"])
                for row in rows
            }
            if set(states) != set(items):
                raise RuntimeError("Outbox batch does not match the approved revision")
            if status == "done":
                allowed_done_states = (
                    {"pending", "failed", "blocked", "done"}
                    if allow_reconciliation
                    else {"pending", "done"}
                )
                if any(value not in allowed_done_states for value in states.values()):
                    raise RuntimeError("Outbox batch is not eligible for completion")
            elif any(value == "done" for value in states.values()):
                raise RuntimeError("A completed outbox item cannot be failed or blocked")
            connection.execute(
                "UPDATE outbox SET status = ?, attempts = attempts + 1, "
                "last_error = ?, updated_at = ? "
                "WHERE proposal_id = ? AND revision = ? "
                f"AND operation_id IN ({placeholders})",
                (
                    status,
                    error,
                    now,
                    proposal_id,
                    revision,
                    *items,
                ),
            )
            if mapping is not None:
                connection.execute(
                    """
                    INSERT INTO entity_mapping(
                        database_name, entity_key, notion_page_id,
                        match_value, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(database_name, entity_key) DO UPDATE SET
                        notion_page_id = excluded.notion_page_id,
                        match_value = excluded.match_value,
                        updated_at = excluded.updated_at
                    """,
                    (
                        mapping["database_name"],
                        mapping["entity_key"],
                        mapping["notion_page_id"],
                        mapping["match_value"],
                        now,
                    ),
                )

    def mark_outbox_batch_done(
        self,
        proposal_id: str,
        revision: int,
        operation_ids: Iterable[str],
        *,
        entity_mapping: Mapping[str, str] | None = None,
        allow_reconciliation: bool = False,
    ) -> None:
        self.mark_outbox_batch(
            proposal_id,
            revision,
            operation_ids,
            "done",
            entity_mapping=entity_mapping,
            allow_reconciliation=allow_reconciliation,
        )

    def mark_outbox_batch_failed(
        self,
        proposal_id: str,
        revision: int,
        operation_ids: Iterable[str],
        reason_code: str,
    ) -> None:
        self.mark_outbox_batch(
            proposal_id,
            revision,
            operation_ids,
            "failed",
            reason_code,
        )

    def mark_outbox_batch_blocked(
        self,
        proposal_id: str,
        revision: int,
        operation_ids: Iterable[str],
        reason_code: str,
    ) -> None:
        self.mark_outbox_batch(
            proposal_id,
            revision,
            operation_ids,
            "blocked",
            reason_code,
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

    def _finalize_approved_overrides(
        self,
        connection: sqlite3.Connection,
        proposal: ProposalRevision,
        *,
        now: str,
    ) -> None:
        """Persist or retire overrides only inside a successful local commit.

        A normal Excel APPLY is an explicit acceptance of the current source
        value and retires an older override. EXCLUDE and DEFER deliberately do
        not. A recovery revision whose user override was already written keeps
        the original override action even though that revision represents the
        operation as EXCLUDE to prevent a second Notion write.
        """

        for operation in proposal.operations:
            change = operation.change
            if change.target_database == _HISTORY_DATABASE:
                continue

            scope_digest = _mutation_scope(
                connection,
                proposal,
                operation,
            )[0]
            case_number = str(
                proposal.correction_binding.get("case_number")
                or change.entity_key
            )
            database_name = change.target_database
            entity_key = change.entity_key
            property_name = change.property_name
            existing = self._find_approved_override(
                connection,
                proposal,
                operation,
            )
            completed_recovery_override = False
            if (
                operation.user_override
                and operation.action is ProposalAction.EXCLUDE
            ):
                outbox = connection.execute(
                    "SELECT status FROM outbox WHERE operation_id = ?",
                    (change.operation_id,),
                ).fetchone()
                completed_recovery_override = bool(
                    outbox is not None and str(outbox["status"]) == "done"
                )

            successful_override = (
                operation.user_override
                and operation.action
                in {ProposalAction.APPLY, ProposalAction.EDIT}
            ) or completed_recovery_override
            if successful_override:
                observed_basis = (
                    source_basis_digest(change.source_refs)
                    if change.source_refs
                    else ""
                )
                # Independent /nx_correct requests do not infer a row or cell
                # citation. When such a correction supersedes an existing
                # override, preserve the last approval-backed Excel basis. A
                # brand-new unbound correction is stored safely but will be
                # shown as a basis conflict at the next Excel proposal.
                stable_basis = observed_basis or (
                    str(existing["source_basis_digest"])
                    if existing is not None
                    else ""
                )
                created_at = (
                    str(existing["created_at"])
                    if existing is not None
                    else now
                )
                if (
                    existing is not None
                    and str(existing["scope_digest"]) != scope_digest
                ):
                    connection.execute(
                        "DELETE FROM approved_override "
                        "WHERE scope_digest = ?",
                        (str(existing["scope_digest"]),),
                    )
                connection.execute(
                    """
                    INSERT INTO approved_override(
                        scope_digest, case_number, database_name, entity_key,
                        property_name, approved_value, source_basis_digest,
                        observed_basis_digest, source_file_hash, proposal_id,
                        revision, operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_digest) DO UPDATE SET
                        case_number = excluded.case_number,
                        approved_value = excluded.approved_value,
                        source_basis_digest = excluded.source_basis_digest,
                        observed_basis_digest = excluded.observed_basis_digest,
                        source_file_hash = excluded.source_file_hash,
                        proposal_id = excluded.proposal_id,
                        revision = excluded.revision,
                        operation_id = excluded.operation_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        scope_digest,
                        case_number,
                        database_name,
                        entity_key,
                        property_name,
                        json.dumps(
                            operation.approved_value,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        stable_basis,
                        observed_basis,
                        proposal.source_file_hash,
                        proposal.proposal_id,
                        proposal.revision,
                        change.operation_id,
                        created_at,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO audit_log("
                    "event_type, proposal_id, actor, details, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        "approved_override_saved",
                        proposal.proposal_id,
                        proposal.requested_by,
                        json.dumps(
                            {
                                "scope_digest": scope_digest,
                                "source_basis_digest": stable_basis,
                                "observed_basis_digest": observed_basis,
                                "operation_id": change.operation_id,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                continue

            if (
                proposal.purpose is ProposalPurpose.EXCEL_SYNC
                and operation.action is ProposalAction.APPLY
                and not operation.user_override
                and existing is not None
            ):
                retains_existing_value = (
                    bool(change.source_refs)
                    and bool(str(existing["source_basis_digest"]))
                    and source_basis_digest(change.source_refs)
                    == str(existing["source_basis_digest"])
                    and operation.approved_value
                    == json.loads(str(existing["approved_value"]))
                )
                if retains_existing_value:
                    # ProposalService reused the existing override for an
                    # unchanged source basis. It is freshly approved, but is
                    # not a new user edit and must not produce another Wiki
                    # correction event or retire the stored override.
                    continue
                connection.execute(
                    "DELETE FROM approved_override WHERE scope_digest = ?",
                    (str(existing["scope_digest"]),),
                )
                connection.execute(
                    "INSERT INTO audit_log("
                    "event_type, proposal_id, actor, details, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        "approved_override_cleared",
                        proposal.proposal_id,
                        proposal.requested_by,
                        json.dumps(
                            {
                                "scope_digest": scope_digest,
                                "operation_id": change.operation_id,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )

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
        """Atomically finalize a proposal in its own authority domain.

        Ordinary Excel synchronization advances the source checkpoint and may
        retain deferred changes. An independently requested Telegram
        correction is bound to the current source for staleness checks, but
        must never claim that the Excel source itself was synchronized.
        """

        now = datetime.now().astimezone().isoformat()
        payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        advances_source_checkpoint = (
            proposal.purpose is ProposalPurpose.EXCEL_SYNC
        )
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
            if advances_source_checkpoint:
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
            if advances_source_checkpoint:
                connection.execute(
                    """
                    INSERT INTO sync_checkpoint(
                        drive_id, item_id, version_id, file_hash,
                        committed_at, proposal_id
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
            self._finalize_approved_overrides(
                connection,
                proposal,
                now=now,
            )
            for operation in proposal.operations:
                if operation.change.target_database == _HISTORY_DATABASE:
                    continue
                if (
                    advances_source_checkpoint
                    and operation.action is ProposalAction.DEFER
                ):
                    connection.execute(
                        "UPDATE mutation_scope_claim SET "
                        "claim_state = 'pending', revision = ?, "
                        "updated_at = ? WHERE proposal_id = ? "
                        "AND operation_id = ?",
                        (
                            proposal.revision,
                            now,
                            proposal.proposal_id,
                            operation.change.operation_id,
                        ),
                    )
                else:
                    connection.execute(
                        "DELETE FROM mutation_scope_claim "
                        "WHERE proposal_id = ? AND operation_id = ?",
                        (
                            proposal.proposal_id,
                            operation.change.operation_id,
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
                            "purpose": proposal.purpose.value,
                            "checkpoint": (
                                version_id if advances_source_checkpoint else None
                            ),
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
        purpose=ProposalPurpose(
            data.get("purpose", ProposalPurpose.EXCEL_SYNC.value)
        ),
        correction_binding=data.get("correction_binding", {}),
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
