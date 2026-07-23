from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


SCHEMA_APPROVAL_PURPOSE = "notion_schema_create/v1"
SCHEMA_RECOVERY_PURPOSE = "notion_schema_recovery/v1"


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ChangeKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE_CANDIDATE = "delete_candidate"


class ProposalAction(StrEnum):
    APPLY = "apply"
    EDIT = "edit"
    EXCLUDE = "exclude"
    DEFER = "defer"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"
    APPLYING = "applying"
    COMMITTED = "committed"
    FAILED = "failed"


class SchemaProposalStatus(StrEnum):
    """Lifecycle for schema creation proposals.

    This is intentionally distinct from :class:`ProposalStatus`.  A receipt or
    state transition for ordinary page data must never be accepted as authority
    for a Notion schema mutation.
    """

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"
    APPLYING = "applying"
    COMMITTED = "committed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class WritePreconditionState(StrEnum):
    PENDING = "pending"
    ALREADY_APPLIED = "already_applied"
    CONFLICT = "conflict"


class AnalyzerLifecycle(StrEnum):
    DRAFT = "draft"
    SHADOW = "shadow"
    VERIFIED = "verified"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


@dataclass(frozen=True, slots=True)
class SourceRef:
    drive_id: str
    item_id: str
    version_id: str
    file_hash: str
    sheet: str
    row: int
    cells: str
    raw_value: JsonValue = None


@dataclass(frozen=True, slots=True)
class KnowledgeRef:
    """Immutable, minimal citation to a chunk in the local read-only Wiki.

    Wiki evidence is supplementary: it can explain or constrain an analysis,
    but it never replaces the Excel ``SourceRef`` required for a write.
    """

    drive_id: str
    item_id: str
    version_id: str
    content_hash: str
    chunk_locator: str
    chunk_hash: str
    security_classification: str
    display_name: str = ""
    etag: str = ""
    extractor_version: str = ""
    policy_version: str = ""
    effective_from: str | None = None
    effective_to: str | None = None
    authority: str = ""
    status: str = ""


@dataclass(slots=True)
class SourceRecord:
    key: str
    sheet: str
    row: int
    values: dict[str, JsonValue]
    source_refs: list[SourceRef] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return sha256_json({"key": self.key, "sheet": self.sheet, "values": self.values})


@dataclass(slots=True)
class WorkbookSnapshot:
    drive_id: str
    item_id: str
    version_id: str
    file_hash: str
    captured_at: datetime
    records: list[SourceRecord]
    identity_warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RecordChange:
    kind: ChangeKind
    key: str
    before: SourceRecord | None
    after: SourceRecord | None


@dataclass(slots=True)
class ReviewItem:
    review_type: str
    title: str
    reason: str
    current_value: JsonValue = None
    proposed_value: JsonValue = None
    confidence: float = 0.0
    source_refs: list[SourceRef] = field(default_factory=list)
    knowledge_refs: list[KnowledgeRef] = field(default_factory=list)


@dataclass(slots=True)
class ProposedChange:
    target_database: str
    entity_key: str
    property_name: str
    kind: ChangeKind
    current_value: JsonValue
    proposed_value: JsonValue
    analyzer: str
    analyzer_version: str
    confidence: float
    reason: str
    source_refs: list[SourceRef]
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    knowledge_refs: list[KnowledgeRef] = field(default_factory=list)

    def digest_payload(self) -> dict[str, Any]:
        payload = {
            "operation_id": self.operation_id,
            "target_database": self.target_database,
            "entity_key": self.entity_key,
            "property_name": self.property_name,
            "kind": self.kind.value,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "analyzer": self.analyzer,
            "analyzer_version": self.analyzer_version,
            "confidence": self.confidence,
            "reason": self.reason,
            "source_refs": [dataclass_to_dict(ref) for ref in self.source_refs],
        }
        # Preserve the digest of proposals created before Wiki support. Empty
        # supplementary citations must not invalidate an existing approval.
        if self.knowledge_refs:
            payload["knowledge_refs"] = [
                dataclass_to_dict(ref) for ref in self.knowledge_refs
            ]
        return payload


@dataclass(slots=True)
class AnalyzerResult:
    analyzer: str
    version: str
    proposed_changes: list[ProposedChange] = field(default_factory=list)
    review_items: list[ReviewItem] = field(default_factory=list)
    derived: dict[str, JsonValue] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProposalOperation:
    change: ProposedChange
    action: ProposalAction = ProposalAction.APPLY
    approved_value: JsonValue = None
    user_override: bool = False

    def __post_init__(self) -> None:
        if self.action is ProposalAction.APPLY and self.approved_value is None:
            self.approved_value = self.change.proposed_value

    def digest_payload(self) -> dict[str, Any]:
        payload = {
            "change": self.change.digest_payload(),
            "action": self.action.value,
            "approved_value": self.approved_value,
        }
        # ``user_override`` makes it explicit that an EDIT value came from the
        # Telegram approver, not from any cited Wiki passage. Keep false absent
        # for backwards-compatible digests.
        if self.user_override:
            payload["user_override"] = True
        return payload


@dataclass(slots=True)
class ProposalRevision:
    proposal_id: str
    revision: int
    source_version_id: str
    source_file_hash: str
    requested_by: str
    chat_id: str
    operations: list[ProposalOperation]
    notion_binding: dict[str, JsonValue] = field(default_factory=dict)
    status: ProposalStatus = ProposalStatus.DRAFT
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    knowledge_binding: dict[str, JsonValue] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        payload = {
            "proposal_id": self.proposal_id,
            "revision": self.revision,
            "source_version_id": self.source_version_id,
            "source_file_hash": self.source_file_hash,
            "requested_by": self.requested_by,
            "chat_id": self.chat_id,
            "operations": [operation.digest_payload() for operation in self.operations],
            "notion_binding": self.notion_binding,
        }
        # An empty binding is semantically equivalent to a pre-Wiki proposal.
        if self.knowledge_binding:
            payload["knowledge_binding"] = self.knowledge_binding
        return sha256_json(payload)


@dataclass(frozen=True, slots=True)
class ApprovalReceipt:
    proposal_id: str
    revision: int
    proposal_digest: str
    source_version_id: str
    source_file_hash: str
    telegram_user_id: str
    chat_id: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str
    notion_binding_digest: str = ""


def schema_approval_command_hash(
    schema_proposal_id: str,
    revision: int,
    proposal_digest: str,
) -> str:
    """Hash the canonical schema-approval command, independent of whitespace."""

    return sha256_json(
        {
            "purpose": SCHEMA_APPROVAL_PURPOSE,
            "command": "nx_schema_approve",
            "schema_proposal_id": schema_proposal_id,
            "revision": revision,
            "proposal_digest": proposal_digest.lower(),
        }
    )


def schema_recovery_command_hash(
    schema_proposal_id: str,
    revision: int,
    proposal_digest: str,
    *,
    telegram_user_id: str,
    chat_id: str,
    thread_id: str,
    telegram_message_id: str,
    telegram_update_id: int,
) -> str:
    """Hash one exact Telegram recovery event in its own authority domain."""

    return sha256_json(
        {
            "purpose": SCHEMA_RECOVERY_PURPOSE,
            "command": "nx_schema_recover",
            "schema_proposal_id": schema_proposal_id,
            "revision": revision,
            "proposal_digest": proposal_digest.lower(),
            "telegram_user_id": str(telegram_user_id),
            "chat_id": str(chat_id),
            "thread_id": str(thread_id),
            "telegram_message_id": str(telegram_message_id),
            "telegram_update_id": int(telegram_update_id),
        }
    )


def legacy_schema_recovery_command_hash(
    schema_proposal_id: str,
    revision: int,
    proposal_digest: str,
    *,
    telegram_user_id: str,
    chat_id: str,
    thread_id: str,
    telegram_message_id: str,
    telegram_update_id: int,
) -> str:
    """Reproduce pre-recovery-domain event hashes for persisted attestations."""

    return sha256_json(
        {
            "purpose": SCHEMA_APPROVAL_PURPOSE,
            "command": "nx_schema_recover",
            "schema_proposal_id": schema_proposal_id,
            "revision": revision,
            "digest": proposal_digest.lower(),
            "page": 1,
            "template_id": None,
            "parent_page_id": None,
            "telegram_user_id": str(telegram_user_id),
            "chat_id": str(chat_id),
            "thread_id": str(thread_id),
            "message_id": str(telegram_message_id),
            "update_id": int(telegram_update_id),
        }
    )


@dataclass(slots=True)
class SchemaProposalRevision:
    """One immutable revision of a Notion schema-creation proposal.

    Schema creation has no Excel source version.  Its authority is instead
    bound to the exact parent, canonical request body, and read-only remote
    precondition captured before approval.
    """

    schema_proposal_id: str
    revision: int
    requested_by: str
    chat_id: str
    thread_id: str
    operation_id: str
    logical_database_name: str
    parent_page_id: str
    schema_payload: dict[str, JsonValue]
    precondition: dict[str, JsonValue]
    status: SchemaProposalStatus = SchemaProposalStatus.DRAFT
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    purpose: str = field(default=SCHEMA_APPROVAL_PURPOSE, init=False)

    def __post_init__(self) -> None:
        if not self.schema_proposal_id.startswith("SP-"):
            raise ValueError("Schema proposal IDs must start with 'SP-'")
        if self.revision < 1:
            raise ValueError("Schema proposal revision must be positive")
        if not self.requested_by.strip() or not self.chat_id.strip():
            raise ValueError("Schema proposal owner and chat are required")
        if not self.operation_id.strip():
            raise ValueError("Schema proposal operation_id is required")
        if not self.logical_database_name.strip() or not self.parent_page_id.strip():
            raise ValueError("Schema proposal target is incomplete")
        if not isinstance(self.schema_payload, dict) or not isinstance(
            self.precondition, dict
        ):
            raise TypeError("Schema payload and precondition must be JSON objects")

    @property
    def precondition_digest(self) -> str:
        return sha256_json(self.precondition)

    @property
    def digest(self) -> str:
        return sha256_json(
            {
                "purpose": self.purpose,
                "schema_proposal_id": self.schema_proposal_id,
                "revision": self.revision,
                "requested_by": self.requested_by,
                "chat_id": self.chat_id,
                "thread_id": self.thread_id,
                "operation_id": self.operation_id,
                "logical_database_name": self.logical_database_name,
                "parent_page_id": self.parent_page_id,
                "schema_payload": self.schema_payload,
                "precondition": self.precondition,
            }
        )


@dataclass(frozen=True, slots=True)
class SchemaApprovalReceipt:
    """Signed, one-time authority for one exact schema proposal revision."""

    schema_proposal_id: str
    revision: int
    proposal_digest: str
    precondition_digest: str
    telegram_user_id: str
    chat_id: str
    thread_id: str
    telegram_message_id: str
    telegram_update_id: int
    command_hash: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str
    purpose: str = field(default=SCHEMA_APPROVAL_PURPOSE, init=False)


@dataclass(frozen=True, slots=True)
class SchemaRecoveryReceipt:
    """Signed, one-time authority to adopt one exact journaled remote schema.

    This receipt grants no create authority.  It binds a fresh Telegram event
    to immutable local evidence from the single prior create attempt.
    """

    schema_proposal_id: str
    revision: int
    proposal_digest: str
    precondition_digest: str
    operation_id: str
    raw_database_id: str
    raw_data_source_id: str
    canonical_database_id: str
    canonical_data_source_id: str
    attempt_started_at: datetime
    attempt_finished_at: datetime
    approval_receipt_nonce: str
    conflict_telegram_update_id: int
    telegram_user_id: str
    chat_id: str
    thread_id: str
    telegram_message_id: str
    telegram_update_id: int
    command_hash: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str
    purpose: str = field(default=SCHEMA_RECOVERY_PURPOSE, init=False)


def dataclass_to_dict(value: Any) -> dict[str, Any]:
    """Serialize nested dataclasses and enums into JSON-compatible dictionaries."""

    def convert(item: Any) -> Any:
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return item.value
        if isinstance(item, list):
            return [convert(child) for child in item]
        if isinstance(item, dict):
            return {str(key): convert(child) for key, child in item.items()}
        if hasattr(item, "__dataclass_fields__"):
            return convert(asdict(item))
        return item

    return convert(value)
