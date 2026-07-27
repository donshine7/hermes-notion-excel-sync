from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from notion_excel_sync.adapters.excel import ExcelSnapshotReader, SheetSpec
from notion_excel_sync.adapters.local_directory import LocalDirectoryReadOnlyAdapter
from notion_excel_sync.adapters.local_files import HydrationPolicy
from notion_excel_sync.adapters.local_workbook import LocalWorkbookReadOnlyClient
from notion_excel_sync.adapters.notion import HttpNotionGateway, NotionMatch
from notion_excel_sync.adapters.onedrive import OneDriveReadOnlyClient
from notion_excel_sync.adapters.onedrive_auth import build_onedrive_token_provider
from notion_excel_sync.config import AppConfig, load_config
from notion_excel_sync.domain import AnalysisPipeline, build_default_registry
from notion_excel_sync.knowledge.corrections import (
    ApprovedCorrectionEvent,
    CorrectionOverlayStore,
)
from notion_excel_sync.knowledge.index import WikiIndex
from notion_excel_sync.knowledge.projection import WikiOutputStore
from notion_excel_sync.knowledge.provider import AnalyzerKnowledgeProvider
from notion_excel_sync.knowledge.refresh import OneDriveWikiRefreshService
from notion_excel_sync.knowledge.retrieval import WikiRetriever
from notion_excel_sync.models import (
    ApprovalReceipt,
    ChangeKind,
    CORRECTION_OVERLAY_RECOVERY_MODE,
    JsonValue,
    ProposalAction,
    ProposalOperation,
    ProposalPurpose,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    WritePreconditionState,
    dataclass_to_dict,
    sha256_json,
)
from notion_excel_sync.persistence.database import (
    ApplyLeaseError,
    CorrectionEventClaimError,
    MutationScopeConflict,
    ProposalInternalScopeConflict,
    ProposalRejectionError,
    StateDatabase,
)
from notion_excel_sync.security.approval import ApprovalError, ApprovalService
from notion_excel_sync.security.receipt_io import (
    receipt_reference,
    resolve_receipt_reference,
    save_receipt,
)
from notion_excel_sync.workflow.apply import (
    ApplyReport,
    ApprovalGatedApplyService,
    public_apply_failure_code,
)
from notion_excel_sync.workflow.corrections import (
    CORRECTION_COMMAND,
    CorrectionRequest,
    CorrectionRequestError,
    parse_correction_command,
)
from notion_excel_sync.workflow.data_sources import configured_data_sources
from notion_excel_sync.workflow.local_wiki import (
    LOCAL_WIKI_CHECKPOINT_KEY,
    LOCAL_WIKI_DRIVE_ID,
    LocalWikiRefreshOrchestrator,
)
from notion_excel_sync.workflow.mail_ingestion import (
    HiworksMailIngestionService,
    HiworksMailIngestionStatus,
)
from notion_excel_sync.workflow.notion_writer import (
    NOTION_DEFINITIONS,
    ApprovedNotionWriter,
    ExactNotionMutationVerifier,
    NotionCurrentValueReader,
    NotionSchemaError,
    build_correction_target_binding,
    build_notion_binding,
    decode_property,
)
from notion_excel_sync.workflow.proposals import ProposalError, ProposalService
from notion_excel_sync.workflow.review_cards import (
    HISTORY_DATABASE,
    ReviewCardPageError,
    build_review_cards,
    render_review_card_page,
    unmerged_history_operation_ids,
)
from notion_excel_sync.workflow.sync import PreparationResult, SyncPreparationService


logger = logging.getLogger(__name__)

CONFIG_ENV = "NOTION_EXCEL_SYNC_CONFIG"
APPROVAL_COMMAND = "/nx_approve"
SYNC_COMMAND = "/nx_sync"
SHOW_COMMAND = "/nx_show"
SET_COMMAND = "/nx_set"
REJECT_COMMAND = "/nx_reject"
RECOVER_COMMAND = "/nx_recover"
CORRECT_COMMAND = CORRECTION_COMMAND
SCHEMA_PLAN_COMMAND = "/nx_schema_plan"
SCHEMA_SHOW_COMMAND = "/nx_schema_show"
SCHEMA_REJECT_COMMAND = "/nx_schema_reject"
SCHEMA_APPROVE_COMMAND = "/nx_schema_approve"
SCHEMA_RECOVER_COMMAND = "/nx_schema_recover"
SCHEMA_COMMANDS = frozenset(
    {
        SCHEMA_PLAN_COMMAND,
        SCHEMA_SHOW_COMMAND,
        SCHEMA_REJECT_COMMAND,
        SCHEMA_APPROVE_COMMAND,
        SCHEMA_RECOVER_COMMAND,
    }
)
RESERVED_COMMANDS = frozenset(
    {
        APPROVAL_COMMAND,
        SYNC_COMMAND,
        SHOW_COMMAND,
        SET_COMMAND,
        REJECT_COMMAND,
        RECOVER_COMMAND,
        CORRECT_COMMAND,
        *SCHEMA_COMMANDS,
    }
)
_IDENTIFIER = r"[A-Za-z0-9._:-]{1,128}"
_COMMAND_RE = re.compile(
    rf"/nx_approve ({_IDENTIFIER}) ([1-9][0-9]*) ([0-9A-Fa-f]{{64}})"
)
_SHOW_RE = re.compile(
    rf"/nx_show ({_IDENTIFIER}) ([1-9][0-9]*)"
    r"(?: ([1-9][0-9]*))?(?: detail ([1-5]))?"
)
_SET_RE = re.compile(
    rf"/nx_set ({_IDENTIFIER}) ([1-9][0-9]*) ({_IDENTIFIER}) "
    r"(apply|edit|exclude|defer)(?:\s+(.+))?",
    re.DOTALL,
)
_REJECT_RE = re.compile(rf"/nx_reject ({_IDENTIFIER}) ([1-9][0-9]*)")
_RECOVER_RE = re.compile(rf"/nx_recover ({_IDENTIFIER}) ([1-9][0-9]*)")
_MAX_EDIT_JSON_CHARS = 2048
_WIKI_BOOTSTRAP_CHECKPOINT = "wiki-bootstrap"
_WIKI_EXCEL_CHECKPOINT = "wiki-excel"
_WIKI_DATA_CHECKPOINT = LOCAL_WIKI_CHECKPOINT_KEY


class HermesGatewayApprovalError(PermissionError):
    """Raised when a Telegram event cannot authorize a proposal."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class ParsedApprovalCommand:
    proposal_id: str
    revision: int
    digest: str


@dataclass(frozen=True, slots=True)
class ParsedProposalCommand:
    proposal_id: str
    revision: int
    page: int = 1
    detail_item: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedSetCommand:
    proposal_id: str
    revision: int
    operation_id: str
    action: ProposalAction
    approved_value: object = None


@dataclass(frozen=True, slots=True)
class TelegramEventIdentity:
    user_id: str
    chat_id: str
    chat_type: str
    thread_id: str
    message_id: str
    update_id: int
    event_timestamp: str


def is_recognized_approval_command(text: object) -> bool:
    """Return True for the reserved command prefix, including malformed uses."""

    return isinstance(text, str) and text.strip().startswith(APPROVAL_COMMAND)


def recognized_gateway_command(text: object) -> str | None:
    """Return the exact reserved command token, including malformed invocations."""

    if not isinstance(text, str):
        return None
    tokens = text.strip().split(maxsplit=1)
    if not tokens:
        return None
    return tokens[0] if tokens[0] in RESERVED_COMMANDS else None


def _denied(code: str, message: str) -> str:
    return (
        f"[NX_APPROVAL_DENIED code={code}] {message} "
        "승인 영수증은 발급되지 않았고 Notion 변경도 허용되지 않았습니다."
    )


def _parse_command(text: str) -> ParsedApprovalCommand:
    normalized = text.strip()
    match = _COMMAND_RE.fullmatch(normalized)
    if match is None:
        raise HermesGatewayApprovalError(
            "Use exactly: /nx_approve <proposal_id> <revision> <64hex_digest>"
        )
    return ParsedApprovalCommand(
        proposal_id=match.group(1),
        revision=int(match.group(2)),
        digest=match.group(3).lower(),
    )


def _parse_proposal_command(text: str, pattern: re.Pattern[str]) -> ParsedProposalCommand:
    match = pattern.fullmatch(text.strip())
    if match is None:
        command = text.strip().split(maxsplit=1)[0]
        suffix = " [page [detail 1-5]]" if command == SHOW_COMMAND else ""
        raise HermesGatewayApprovalError(
            f"Use exactly: {command} <proposal_id> <revision>{suffix}"
        )
    page = (
        int(match.group(3))
        if match.lastindex is not None
        and match.lastindex >= 3
        and match.group(3)
        else 1
    )
    detail_item = (
        int(match.group(4))
        if match.lastindex is not None
        and match.lastindex >= 4
        and match.group(4)
        else None
    )
    return ParsedProposalCommand(
        match.group(1),
        int(match.group(2)),
        page,
        detail_item,
    )


def _validate_json_shape(value: object, *, depth: int = 0) -> None:
    if depth > 20:
        raise HermesGatewayApprovalError("Edit JSON nesting exceeds 20 levels")
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        if len(value) > 500:
            raise HermesGatewayApprovalError("Edit JSON contains too many list items")
        for item in value:
            _validate_json_shape(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 500:
            raise HermesGatewayApprovalError("Edit JSON contains too many object fields")
        for item in value.values():
            _validate_json_shape(item, depth=depth + 1)
        return
    raise HermesGatewayApprovalError("Edit value must be a JSON value")


def _reject_non_finite_json(token: str) -> object:
    raise ValueError(f"non-finite number {token}")


def _parse_set_command(text: str) -> ParsedSetCommand:
    match = _SET_RE.fullmatch(text.strip())
    if match is None:
        raise HermesGatewayApprovalError(
            "Use exactly: /nx_set <proposal_id> <revision> <operation_id> "
            "<apply|exclude|defer|edit> [JSON]"
        )
    action = ProposalAction(match.group(4))
    raw_value = match.group(5)
    if action is ProposalAction.EDIT:
        if raw_value is None:
            raise HermesGatewayApprovalError("The edit action requires a JSON value")
        if len(raw_value) > _MAX_EDIT_JSON_CHARS:
            raise HermesGatewayApprovalError("Edit JSON exceeds 2048 characters")
        try:
            value = json.loads(raw_value, parse_constant=_reject_non_finite_json)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HermesGatewayApprovalError(
                "Edit value is not strict JSON"
            ) from exc
        _validate_json_shape(value)
    else:
        if raw_value is not None:
            raise HermesGatewayApprovalError(
                "Only the edit action may include a JSON value"
            )
        value = None
    return ParsedSetCommand(
        proposal_id=match.group(1),
        revision=int(match.group(2)),
        operation_id=match.group(3),
        action=action,
        approved_value=value,
    )


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _event_identity(event: Any) -> TelegramEventIdentity:
    source = getattr(event, "source", None)
    if source is None or _platform_name(source) != "telegram":
        raise HermesGatewayApprovalError(
            "Trusted synchronization commands are accepted only from Telegram updates"
        )
    if bool(getattr(source, "is_bot", False)):
        raise HermesGatewayApprovalError("Bot-authored synchronization commands are not accepted")

    user_id = str(getattr(source, "user_id", "") or "").strip()
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    message_id = str(
        getattr(source, "message_id", "")
        or getattr(event, "message_id", "")
        or ""
    ).strip()
    raw_update_id = getattr(event, "platform_update_id", None)
    if not user_id or not chat_id:
        raise HermesGatewayApprovalError("Telegram user and chat identity are required")
    if not message_id or raw_update_id is None:
        raise HermesGatewayApprovalError(
            "Telegram message_id and update_id are required for the approval audit"
        )
    try:
        update_id = int(raw_update_id)
    except (TypeError, ValueError) as exc:
        raise HermesGatewayApprovalError("Telegram update_id is invalid") from exc

    timestamp = getattr(event, "timestamp", None)
    if isinstance(timestamp, datetime):
        event_timestamp = timestamp.astimezone(UTC).isoformat()
    else:
        event_timestamp = str(timestamp or "")
    return TelegramEventIdentity(
        user_id=user_id,
        chat_id=chat_id,
        chat_type=str(getattr(source, "chat_type", "") or ""),
        thread_id=str(getattr(source, "thread_id", "") or ""),
        message_id=message_id,
        update_id=update_id,
        event_timestamp=event_timestamp,
    )


def _resolve_config_path(project_root: str | Path) -> Path:
    root = Path(project_root).resolve()
    configured = os.environ.get(CONFIG_ENV, "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else root / "config" / "sync.local.json"
    )
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _approval_service(config: AppConfig, database: StateDatabase) -> ApprovalService:
    secret = config.secret(config.approval.secret_env)
    assert secret is not None
    return ApprovalService(
        secret,
        config.approval.allowed_telegram_users,
        ttl_seconds=config.approval.ttl_seconds,
        nonce_used=database.nonce_used,
        mark_nonce_used=database.mark_nonce_used,
    )


def _configured_data_sources(
    config: AppConfig,
    database: StateDatabase | None = None,
) -> dict[str, str]:
    return configured_data_sources(config, database)


def _notion_read_gateway(config: AppConfig) -> HttpNotionGateway:
    token = config.secret(config.notion.read_token_env)
    assert token is not None
    return HttpNotionGateway(
        token,
        lambda *_: False,
        notion_version=config.notion.api_version,
    )


def _proposal_service(config: AppConfig, database: StateDatabase) -> ProposalService:
    return ProposalService(database, ttl_seconds=config.approval.ttl_seconds)


def _excel_reader(config: AppConfig) -> ExcelSnapshotReader:
    keys = tuple(config.excel.key_columns)
    return ExcelSnapshotReader(
        SheetSpec(
            name=sheet,
            key_columns=(keys if sheet not in config.excel.key_groups else ()),
            key_groups=tuple(
                tuple(group) for group in config.excel.key_groups.get(sheet, [])
            ),
            header_row=config.excel.header_row,
        )
        for sheet in config.excel.sheets
    )


def _validate_revision_binding(
    identity: TelegramEventIdentity,
    proposal: ProposalRevision,
    revision: int,
) -> None:
    if identity.user_id != proposal.requested_by:
        raise HermesGatewayApprovalError("Only the proposal owner may use this command")
    if identity.chat_id != proposal.chat_id:
        raise HermesGatewayApprovalError(
            "The command must come from the proposal's original Telegram chat"
        )
    if revision != proposal.revision:
        raise HermesGatewayApprovalError(
            f"Revision {revision} is stale; current revision is {proposal.revision}"
        )


def _audit_gateway_command(
    database: StateDatabase,
    command: str,
    identity: TelegramEventIdentity,
    proposal_id: str | None = None,
    **details: object,
) -> None:
    database.audit(
        "telegram_gateway_command",
        {
            "command": command,
            "telegram_message_id": identity.message_id,
            "telegram_update_id": identity.update_id,
            "telegram_chat_id": identity.chat_id,
            "telegram_chat_type": identity.chat_type,
            "telegram_thread_id": identity.thread_id,
            "telegram_event_timestamp": identity.event_timestamp,
            **details,
        },
        proposal_id,
        identity.user_id,
    )


def _proposal_screen(proposal: ProposalRevision, *, prefix: str) -> str:
    return f"[{prefix}]\n{render_review_card_page(proposal, 1)}"


def _is_overlay_only_correction_recovery(
    proposal: ProposalRevision,
) -> bool:
    recovery = proposal.correction_binding.get("recovery")
    return (
        proposal.purpose is ProposalPurpose.USER_CORRECTION
        and isinstance(recovery, dict)
        and recovery.get("mode") == CORRECTION_OVERLAY_RECOVERY_MODE
    )


def _proposal_page(
    proposal: ProposalRevision,
    page: int,
    detail_item: int | None = None,
) -> str:
    try:
        return render_review_card_page(
            proposal,
            page,
            detail_item=detail_item,
        )
    except ReviewCardPageError as exc:
        raise HermesGatewayApprovalError(
            "The requested proposal review page is unavailable"
        ) from exc


def _configured_knowledge_provider(
    config: AppConfig,
) -> AnalyzerKnowledgeProvider | None:
    """Return the read-only Wiki provider only for an installed index."""

    if not config.wiki.enabled or not config.wiki.index_db.is_file():
        return None
    return AnalyzerKnowledgeProvider(
        WikiRetriever(WikiIndex(config.wiki.index_db), config.wiki)
    )


def _ensure_local_wiki_baseline(
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
) -> dict[str, Any] | None:
    if config.source.mode != "local_filesystem" or not config.wiki.enabled:
        return None
    return database.initialize_ingestion_checkpoint(
        _WIKI_BOOTSTRAP_CHECKPOINT,
        {
            "schema_version": 1,
            "phase": "started",
            "t0": datetime.now(UTC).isoformat(),
            "requested_by": identity.user_id,
            "telegram_update_id": identity.update_id,
            "telegram_message_id": identity.message_id,
        },
        actor=identity.user_id,
    )


def _local_snapshot_observer(
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
    baseline: Mapping[str, Any] | None,
) -> Callable[[Any, bool], None] | None:
    if baseline is None:
        return None
    if config.wiki.output_root is None or config.wiki.source_root is None:
        raise HermesGatewayApprovalError("Local Wiki output paths are not configured")
    baseline_at = str(baseline.get("t0", "")).strip()
    if not baseline_at:
        raise HermesGatewayApprovalError("Local Wiki baseline is invalid")
    store = WikiOutputStore(
        config.wiki.output_root,
        source_root=config.wiki.source_root,
    )

    def observe(snapshot: Any, full_reconcile: bool) -> None:
        projection = store.publish_excel(snapshot, baseline_at=baseline_at)
        store.publish_root_readme(updated_at=datetime.now(UTC))
        database.save_ingestion_checkpoint(
            _WIKI_EXCEL_CHECKPOINT,
            {
                "schema_version": 1,
                "generation": projection.generation,
                "source_version_id": snapshot.version_id,
                "source_file_hash": snapshot.file_hash,
                "record_count": projection.document_count,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            actor=identity.user_id,
        )
        data_checkpoint = database.get_ingestion_checkpoint(_WIKI_DATA_CHECKPOINT)
        if full_reconcile and data_checkpoint is not None:
            completed = {
                **dict(baseline),
                "phase": "complete",
                "completed_at": datetime.now(UTC).isoformat(),
                "g0": sha256_json(
                    {
                        "t0": baseline_at,
                        "excel_generation": projection.generation,
                        "data_generation": data_checkpoint.get("generation"),
                    }
                ),
            }
            database.save_ingestion_checkpoint(
                _WIKI_BOOTSTRAP_CHECKPOINT,
                completed,
                actor=identity.user_id,
            )

    return observe


def _refresh_local_wiki_data(
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
    baseline: Mapping[str, Any] | None,
) -> None:
    """Refresh the immutable local data folder before it can inform analysis."""

    if baseline is None:
        return
    baseline_at = str(baseline.get("t0", "")).strip()
    if not baseline_at:
        raise HermesGatewayApprovalError("Local Wiki baseline is invalid")
    try:
        result = LocalWikiRefreshOrchestrator(
            source=config.source,
            wiki=config.wiki,
            checkpoints=database,
            runtime_root=config.wiki.index_db.parent,
        ).refresh(
            baseline_at=baseline_at,
            actor=identity.user_id,
        )
        if config.wiki.output_root is None or config.wiki.source_root is None:
            raise HermesGatewayApprovalError(
                "Local Wiki output paths are not configured"
            )
        WikiOutputStore(
            config.wiki.output_root,
            source_root=config.wiki.source_root,
        ).publish_root_readme(updated_at=datetime.now(UTC))
        logger.info(
            "Local Wiki data generation ready: generation=%s documents=%s initial=%s",
            result.projection.generation,
            result.projection.document_count,
            result.initial,
        )
    except HermesGatewayApprovalError:
        raise
    except Exception as exc:
        raise HermesGatewayApprovalError(
            "Local data Wiki refresh failed; Excel and Notion processing did not start"
        ) from exc


def _refresh_hiworks_mail(
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
    baseline: Mapping[str, Any] | None,
) -> None:
    """Publish only provably post-T0 Hiworks mail before Excel analysis starts."""

    if baseline is None:
        return
    try:
        result = HiworksMailIngestionService(
            email=config.email,
            wiki=config.wiki,
            checkpoints=database,
            secret_resolver=lambda name: config.secret(name),
        ).ingest(actor=identity.user_id)
        if result.status is HiworksMailIngestionStatus.COMPLETED:
            logger.info(
                "Hiworks Wiki generation ready: generation=%s messages=%s remaining=%s",
                result.generation,
                result.new_count,
                result.remaining,
            )
    except Exception as exc:
        raise HermesGatewayApprovalError(
            "Hiworks Wiki refresh failed; Excel and Notion processing did not start"
        ) from exc


def _source_client(
    config: AppConfig,
    database: StateDatabase,
) -> OneDriveReadOnlyClient | LocalWorkbookReadOnlyClient:
    if config.source.mode != "local_filesystem":
        return OneDriveReadOnlyClient(build_onedrive_token_provider(config.onedrive))
    if config.source.excel_path is None or config.source.root_dir is None:
        raise HermesGatewayApprovalError("Local workbook source is not configured")
    first_run = (
        database.get_checkpoint(
            config.onedrive.drive_id,
            config.onedrive.item_id,
        )
        is None
    )
    hydration_policy = (
        HydrationPolicy.READ_ONLY
        if first_run and config.source.initial_auto_hydrate
        else HydrationPolicy.DENY
    )
    return LocalWorkbookReadOnlyClient(
        config.source.excel_path,
        config.source.snapshot_dir,
        drive_id=config.onedrive.drive_id,
        item_id=config.onedrive.item_id,
        source_root=config.source.root_dir,
        hydration_policy=hydration_policy,
    )


def _wiki_live_binding_guard(
    config: AppConfig,
    onedrive: Any,
    index: WikiIndex | None = None,
    proposal: ProposalRevision | None = None,
) -> Callable[[], Mapping[str, JsonValue]]:
    """Build a GET-only guard for the exact Wiki generation and source root."""

    wiki_config = config.wiki
    wiki_source: Any = onedrive
    default_drive_id = config.onedrive.drive_id
    source_mode = getattr(getattr(config, "source", None), "mode", "onedrive")
    if source_mode == "local_filesystem":
        if config.wiki.source_root is None:
            raise HermesGatewayApprovalError("Local Wiki source root is not configured")
        wiki_source = LocalDirectoryReadOnlyAdapter(
            config.wiki.source_root,
            config.wiki.index_db.parent / "directory-manifests",
            drive_id=LOCAL_WIKI_DRIVE_ID,
            hydration_policy=HydrationPolicy.DENY,
        )
        wiki_config = replace(
            config.wiki,
            drive_id=LOCAL_WIKI_DRIVE_ID,
            root_item_id=wiki_source.root_item_id,
            root_path="",
        )
        default_drive_id = LOCAL_WIKI_DRIVE_ID
    service = OneDriveWikiRefreshService(
        config=wiki_config,
        onedrive=wiki_source,
        index=index or WikiIndex(config.wiki.index_db),
        default_drive_id=default_drive_id,
    )
    # Preserve every approved citation for exact index validation.  Collapsing
    # citations by a partial key could discard one of two conflicting refs that
    # share a locator/chunk hash but disagree on version, etag, or content hash.
    refs = tuple(
        ref
        for operation in proposal.operations if proposal is not None
        for ref in operation.change.knowledge_refs
    )
    content_verified = False

    def guard() -> Mapping[str, JsonValue]:
        nonlocal content_verified
        binding = service.live_binding(refs, verify_content=not content_verified)
        content_verified = True
        return binding

    return guard


def _assert_recovery_knowledge_is_current(
    proposal: ProposalRevision,
    guard: Callable[[], Mapping[str, JsonValue]],
) -> None:
    """Fail closed before recovery inspects any Notion value."""

    has_citations = any(
        operation.change.knowledge_refs for operation in proposal.operations
    )
    if has_citations and not proposal.knowledge_binding:
        raise HermesGatewayApprovalError(
            "The failed proposal has Wiki citations without an immutable binding; "
            "send /nx_sync for a fresh proposal"
        )
    if not proposal.knowledge_binding:
        return
    try:
        current_binding = dict(guard())
        current_digest = sha256_json(current_binding)
        approved_digest = sha256_json(proposal.knowledge_binding)
    except Exception as exc:
        raise HermesGatewayApprovalError(
            "Wiki sources cannot be revalidated; refresh the Wiki and send /nx_sync"
        ) from exc
    if current_digest != approved_digest:
        raise HermesGatewayApprovalError(
            "Wiki sources changed after the failed run; send /nx_sync for a fresh proposal"
        )


def _prepare_authenticated_sync(
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
) -> PreparationResult:
    wiki_baseline = _ensure_local_wiki_baseline(config, database, identity)
    _refresh_local_wiki_data(config, database, identity, wiki_baseline)
    _refresh_hiworks_mail(config, database, identity, wiki_baseline)
    try:
        source = _source_client(config, database)
        item = source.get_item(config.onedrive.drive_id, config.onedrive.item_id)
    except HermesGatewayApprovalError:
        raise
    except Exception as exc:
        logger.exception(
            "Authenticated sync preparation failed closed "
            "(stage=local_workbook_capture)"
        )
        raise HermesGatewayApprovalError(
            "Local Excel capture failed; Notion was not changed"
        ) from exc
    try:
        data_sources = _configured_data_sources(config, database)
    except HermesGatewayApprovalError:
        raise
    except Exception as exc:
        logger.exception(
            "Authenticated sync preparation failed closed "
            "(stage=notion_target_binding)"
        )
        raise HermesGatewayApprovalError(
            "Notion target verification failed; Notion was not changed"
        ) from exc
    try:
        read_gateway = _notion_read_gateway(config)
    except HermesGatewayApprovalError:
        raise
    except Exception as exc:
        logger.exception(
            "Authenticated sync preparation failed closed "
            "(stage=notion_read_setup)"
        )
        raise HermesGatewayApprovalError(
            "Notion read setup failed; Notion was not changed"
        ) from exc
    service = SyncPreparationService(
        database=database,
        onedrive=source,
        excel=_excel_reader(config),
        pipeline=AnalysisPipeline(build_default_registry()),
        proposals=_proposal_service(config, database),
        notion_reader=NotionCurrentValueReader(
            read_gateway,
            data_sources,
            database=database,
        ),
        available_data_sources=set(data_sources),
        data_source_ids=data_sources,
        notion_schema_provider=read_gateway.get_data_source,
        knowledge_provider=_configured_knowledge_provider(config),
        first_run_full_reconcile=config.source.mode == "local_filesystem",
        snapshot_observer=_local_snapshot_observer(
            config,
            database,
            identity,
            wiki_baseline,
        ),
    )
    try:
        return service.prepare(
            drive_id=config.onedrive.drive_id,
            item_id=config.onedrive.item_id,
            initial_cutoff=config.initial_cutoff,
            requested_by=identity.user_id,
            chat_id=identity.chat_id,
            source_web_url=item.web_url,
        )
    except HermesGatewayApprovalError:
        raise
    except Exception as exc:
        logger.exception(
            "Authenticated sync preparation failed closed "
            "(stage=excel_notion_analysis)"
        )
        raise HermesGatewayApprovalError(
            "Excel and Notion comparison failed; Notion was not changed"
        ) from exc


def _latest_version(
    client: OneDriveReadOnlyClient | LocalWorkbookReadOnlyClient,
    drive_id: str,
    item_id: str,
):
    versions = client.list_versions(drive_id, item_id)
    if not versions:
        raise HermesGatewayApprovalError(
            "The configured source returned no retained workbook versions"
        )
    latest_time = max(item.last_modified_at for item in versions)
    candidates = [item for item in versions if item.last_modified_at == latest_time]
    if len(candidates) != 1:
        raise HermesGatewayApprovalError(
            "The configured source returned an ambiguous latest workbook version"
        )
    return candidates[0]


def _capture_current_source(
    client: OneDriveReadOnlyClient | LocalWorkbookReadOnlyClient,
    drive_id: str,
    item_id: str,
) -> tuple[str, str]:
    before = _latest_version(client, drive_id, item_id)
    content = client.download_current(drive_id, item_id)
    after = _latest_version(client, drive_id, item_id)
    if before.id != after.id:
        raise HermesGatewayApprovalError(
            "The configured source changed while it was being revalidated"
        )
    return before.id, hashlib.sha256(content).hexdigest()


def _handle_sync_command(
    text: str,
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
) -> str:
    if text.strip() != SYNC_COMMAND:
        raise HermesGatewayApprovalError("Use exactly: /nx_sync")
    _audit_gateway_command(database, SYNC_COMMAND, identity)
    result = _prepare_authenticated_sync(config, database, identity)
    if result.proposal is None:
        payload = {
            "baseline_version_id": result.baseline_version_id,
            "current_version_id": result.current_version_id,
            "changed_rows": result.changes_detected,
            "pending_reintroduced": result.pending_reintroduced,
            "checkpoint_advanced": result.checkpoint_advanced,
            "notion_mutated": False,
        }
        return "[NX_SYNC_NO_CHANGES] " + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    _audit_gateway_command(
        database,
        "nx_sync_prepared",
        identity,
        result.proposal.proposal_id,
        revision=result.proposal.revision,
        digest=result.proposal.digest,
    )
    screen = _proposal_screen(result.proposal, prefix="NX_SYNC_PROPOSAL_READY")
    if result.pending_staged:
        screen += (
            "\n[NX_SYNC_BATCH] "
            f"review_operations={len(result.proposal.operations)} "
            f"remaining_operations={result.pending_staged}"
        )
    return screen


def _correction_proposal_id(
    identity: TelegramEventIdentity,
    request: CorrectionRequest,
) -> str:
    return "PC-" + sha256_json(
        {
            "purpose": ProposalPurpose.USER_CORRECTION.value,
            "telegram_user_id": identity.user_id,
            "chat_id": identity.chat_id,
            "message_id": identity.message_id,
            "update_id": identity.update_id,
            "request_digest": request.digest,
        }
    )[:32]


def _prepare_correction_proposal(
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
    request: CorrectionRequest,
) -> ProposalRevision:
    """Create one read-only, source-bound correction proposal."""

    proposal_id = _correction_proposal_id(identity, request)
    claim = database.claim_correction_gateway_event(
        telegram_user_id=identity.user_id,
        chat_id=identity.chat_id,
        message_id=identity.message_id,
        update_id=identity.update_id,
        request_digest=request.digest,
        proposal_id=proposal_id,
    )
    try:
        existing = database.load_proposal(proposal_id)
    except KeyError:
        existing = None
    if existing is not None:
        if (
            existing.purpose is not ProposalPurpose.USER_CORRECTION
            or existing.requested_by != identity.user_id
            or existing.chat_id != identity.chat_id
            or existing.correction_binding.get("request_digest")
            != request.digest
            or existing.correction_binding.get("event_key")
            != claim["event_key"]
        ):
            raise HermesGatewayApprovalError(
                "Stored correction proposal does not match the Telegram request"
            )
        return existing

    data_sources = _configured_data_sources(config, database)
    definition = NOTION_DEFINITIONS.get(request.database)
    data_source_id = data_sources.get(request.database)
    if definition is None or data_source_id is None:
        raise HermesGatewayApprovalError(
            "Correction target database is not configured"
        )
    property_type = definition.properties.get(request.property_name)
    if property_type is None:
        raise HermesGatewayApprovalError(
            "Correction target property is not configured"
        )

    source = _source_client(config, database)
    version_id, file_hash = _capture_current_source(
        source,
        config.onedrive.drive_id,
        config.onedrive.item_id,
    )
    notion = _notion_read_gateway(config)
    mapping = database.get_entity_mapping(
        request.database,
        request.entity_key,
    )
    if mapping is not None:
        page = notion.get_page(mapping["notion_page_id"])
    else:
        page = notion.find_page(
            data_source_id,
            NotionMatch(definition.title_property, "title"),
            request.case_number or request.entity_key,
        )
    if not page:
        raise HermesGatewayApprovalError(
            "Correction target Notion item was not found"
        )
    target_binding = build_correction_target_binding(
        page,
        database_name=request.database,
        data_source_id=data_source_id,
        entity_key=request.entity_key,
        property_name=request.property_name,
    )
    current_value = decode_property(
        page.get("properties", {}).get(request.property_name),
        property_type,
    )
    operation_id = "corr-" + sha256_json(
        {
            "proposal_id": proposal_id,
            "request_digest": request.digest,
        }
    )[:32]
    change = ProposedChange(
        target_database=request.database,
        entity_key=request.entity_key,
        property_name=request.property_name,
        kind=ChangeKind.UPDATE,
        current_value=current_value,
        proposed_value=request.value,
        analyzer="user_correction",
        analyzer_version="1.0.0",
        confidence=1.0,
        reason=request.reason,
        source_refs=[],
        operation_id=operation_id,
    )
    notion_binding = build_notion_binding(
        [change],
        data_sources,
        schema_provider=notion.get_data_source,
    )
    correction_binding: dict[str, JsonValue] = {
        "version": 1,
        "request_digest": request.digest,
        "event_key": str(claim["event_key"]),
        "case_number": request.case_number or request.entity_key,
        "mutation_scope": list(request.mutation_scope),
        "target": target_binding,
    }
    preview = ProposalRevision(
        proposal_id=proposal_id,
        revision=1,
        source_version_id=version_id,
        source_file_hash=file_hash,
        requested_by=identity.user_id,
        chat_id=identity.chat_id,
        operations=[
            ProposalOperation(
                change=change,
                action=ProposalAction.EDIT,
                approved_value=request.value,
                user_override=True,
            )
        ],
        notion_binding=notion_binding,
        purpose=ProposalPurpose.USER_CORRECTION,
        correction_binding=correction_binding,
    )
    ProposalService._validate_approved_value(
        preview.operations[0],
        request.value,
    )
    inspection = ApprovedNotionWriter(
        notion,
        database,
        preview,
        data_sources,
        require_exact_schema=True,
    ).inspect(preview.operations[0])
    if inspection is WritePreconditionState.CONFLICT:
        raise HermesGatewayApprovalError(
            "Correction target changed while the proposal was prepared"
        )

    proposals = _proposal_service(config, database)
    proposal = proposals.create(
        source_version_id=version_id,
        source_file_hash=file_hash,
        requested_by=identity.user_id,
        chat_id=identity.chat_id,
        changes=[change],
        notion_binding=notion_binding,
        purpose=ProposalPurpose.USER_CORRECTION,
        proposal_id=proposal_id,
        correction_binding=correction_binding,
    )
    return proposals.request_approval(proposal.proposal_id)


def _handle_correction_command(
    text: str,
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
) -> str:
    request = parse_correction_command(text)
    proposal = _prepare_correction_proposal(
        config,
        database,
        identity,
        request,
    )
    _audit_gateway_command(
        database,
        CORRECT_COMMAND,
        identity,
        proposal.proposal_id,
        revision=proposal.revision,
        digest=proposal.digest,
        request_digest=request.digest,
    )
    return _proposal_screen(
        proposal,
        prefix="NX_CORRECTION_PROPOSAL_READY",
    )


def _handle_show_command(
    text: str,
    database: StateDatabase,
    identity: TelegramEventIdentity,
) -> str:
    command = _parse_proposal_command(text, _SHOW_RE)
    proposal = database.load_proposal(command.proposal_id)
    _validate_revision_binding(identity, proposal, command.revision)
    _audit_gateway_command(
        database,
        SHOW_COMMAND,
        identity,
        proposal.proposal_id,
        revision=proposal.revision,
        page=command.page,
        detail_item=command.detail_item,
    )
    return _proposal_page(
        proposal,
        command.page,
        command.detail_item,
    )


def _handle_set_command(
    text: str,
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
) -> str:
    command = _parse_set_command(text)
    proposal = database.load_proposal(command.proposal_id)
    _validate_revision_binding(identity, proposal, command.revision)
    if _is_overlay_only_correction_recovery(proposal):
        raise HermesGatewayApprovalError(
            "Overlay-only correction recovery cannot be revised; "
            "send only its exact approval command"
        )
    target = next(
        (
            operation
            for operation in proposal.operations
            if operation.change.operation_id == command.operation_id
        ),
        None,
    )
    if (
        target is not None
        and target.change.target_database == HISTORY_DATABASE
    ):
        raise HermesGatewayApprovalError(
            "Generated history operations cannot be edited directly"
        )
    if (
        proposal.purpose is ProposalPurpose.USER_CORRECTION
        and command.action
        not in {ProposalAction.EDIT, ProposalAction.EXCLUDE}
    ):
        raise HermesGatewayApprovalError(
            "Independent corrections support only edit or exclude; "
            "apply and defer would break the required Wiki correction record"
        )
    proposals = _proposal_service(config, database)
    if command.action is ProposalAction.EDIT:
        revised = proposals.edit(
            command.proposal_id,
            command.operation_id,
            command.action,
            command.approved_value,
        )
    else:
        revised = proposals.edit(
            command.proposal_id,
            command.operation_id,
            command.action,
        )
    revised = proposals.request_approval(revised.proposal_id)
    _audit_gateway_command(
        database,
        SET_COMMAND,
        identity,
        revised.proposal_id,
        previous_revision=command.revision,
        revision=revised.revision,
        operation_id=command.operation_id,
        action=command.action.value,
        digest=revised.digest,
    )
    return _proposal_screen(revised, prefix="NX_PROPOSAL_REVISED")


def _handle_reject_command(
    text: str,
    database: StateDatabase,
    identity: TelegramEventIdentity,
    command: ParsedProposalCommand | None = None,
) -> str:
    command = command or _parse_proposal_command(text, _REJECT_RE)
    proposal = database.load_proposal(command.proposal_id)
    _validate_revision_binding(identity, proposal, command.revision)
    database.reject_proposal_atomically(
        proposal.proposal_id,
        expected_revision=command.revision,
        actor=identity.user_id,
        chat_id=identity.chat_id,
        gateway_audit_details={
            "command": REJECT_COMMAND,
            "telegram_message_id": identity.message_id,
            "telegram_update_id": identity.update_id,
            "telegram_chat_id": identity.chat_id,
            "telegram_chat_type": identity.chat_type,
            "telegram_thread_id": identity.thread_id,
            "telegram_event_timestamp": identity.event_timestamp,
            "revision": proposal.revision,
        },
    )
    return (
        f"[NX_PROPOSAL_REJECTED] {proposal.proposal_id} r{proposal.revision}. "
        "Notion was not changed and the checkpoint was not advanced."
    )


def _assert_started_apply_recovery_evidence(
    database: StateDatabase,
    proposal: ProposalRevision,
) -> None:
    """Adopt only an exact, durably started apply after signer rotation."""

    try:
        receipt = database.load_latest_receipt(
            proposal.proposal_id,
            proposal.revision,
        )
    except Exception as exc:
        raise HermesGatewayApprovalError(
            "The started apply has no valid stored approval receipt"
        ) from exc
    text_fields = (
        receipt.proposal_digest,
        receipt.source_version_id,
        receipt.source_file_hash,
        receipt.telegram_user_id,
        receipt.chat_id,
        receipt.nonce,
        receipt.signature,
        receipt.notion_binding_digest,
    )
    if any(not isinstance(value, str) or not value.strip() for value in text_fields):
        raise HermesGatewayApprovalError(
            "The started apply approval evidence is incomplete"
        )
    if (
        receipt.proposal_id != proposal.proposal_id
        or receipt.revision != proposal.revision
        or not hmac.compare_digest(receipt.proposal_digest, proposal.digest)
        or not hmac.compare_digest(
            receipt.notion_binding_digest,
            sha256_json(proposal.notion_binding),
        )
        or receipt.source_version_id != proposal.source_version_id
        or receipt.source_file_hash != proposal.source_file_hash
        or receipt.telegram_user_id != proposal.requested_by
        or receipt.chat_id != proposal.chat_id
        or not database.nonce_used(receipt.nonce)
    ):
        raise HermesGatewayApprovalError(
            "The started apply approval evidence does not match the proposal"
        )
    write_operation_ids = [
        operation.change.operation_id
        for operation in proposal.operations
        if operation.action in {ProposalAction.APPLY, ProposalAction.EDIT}
    ]
    if (
        not write_operation_ids
        or len(set(write_operation_ids)) != len(write_operation_ids)
    ):
        raise HermesGatewayApprovalError(
            "The started apply has no exact write-operation scope"
        )
    outbox = database.outbox_statuses(
        proposal.proposal_id,
        proposal.revision,
    )
    if (
        set(outbox) != set(write_operation_ids)
        or any(
            status not in {"pending", "failed", "blocked", "done"}
            for status in outbox.values()
        )
    ):
        raise HermesGatewayApprovalError(
            "The started apply outbox does not match the approved scope"
        )


def _handle_recover_command(
    text: str,
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
) -> str:
    command = _parse_proposal_command(text, _RECOVER_RE)
    proposal = database.load_proposal(command.proposal_id)
    _validate_revision_binding(identity, proposal, command.revision)
    if proposal.status is ProposalStatus.APPLYING:
        try:
            with database.apply_lease(
                drive_id=config.onedrive.drive_id,
                item_id=config.onedrive.item_id,
                proposal_id=proposal.proposal_id,
                revision=proposal.revision,
            ):
                current = database.load_proposal(
                    proposal.proposal_id,
                    proposal.revision,
                )
                _validate_revision_binding(
                    identity,
                    current,
                    command.revision,
                )
                if current.status is ProposalStatus.APPLYING:
                    _assert_started_apply_recovery_evidence(
                        database,
                        current,
                    )
                    current.status = ProposalStatus.FAILED
                    database.save_proposal(current)
                    database.audit(
                        "apply_failed",
                        {
                            "recovery": "started_apply_adopted",
                            "revision": current.revision,
                        },
                        current.proposal_id,
                        identity.user_id,
                    )
                elif current.status is not ProposalStatus.FAILED:
                    raise HermesGatewayApprovalError(
                        "The proposal changed before recovery could start"
                    )
                # Re-enter the existing FAILED-only reconciliation while the
                # exact source lease remains held. This path performs Notion
                # reads only and prepares a new revision for separate approval.
                return _handle_recover_command(
                    text,
                    config,
                    database,
                    identity,
                )
        except ApplyLeaseError as exc:
            raise HermesGatewayApprovalError(
                "A live apply worker still owns this recovery scope"
            ) from exc
    if proposal.status is not ProposalStatus.FAILED:
        raise HermesGatewayApprovalError("Only a failed proposal can be recovered")
    proposals = _proposal_service(config, database)
    overlay_only_recovery = proposals.correction_remote_writes_complete(
        proposal
    )
    configured_source = _source_client(config, database)
    version_id, file_hash = _capture_current_source(
        configured_source,
        config.onedrive.drive_id,
        config.onedrive.item_id,
    )
    if not overlay_only_recovery:
        if (
            version_id != proposal.source_version_id
            or file_hash != proposal.source_file_hash
        ):
            raise HermesGatewayApprovalError(
                "The configured source changed after the failed run; "
                "send /nx_sync for a fresh proposal"
            )
        _assert_recovery_knowledge_is_current(
            proposal,
            _wiki_live_binding_guard(
                config,
                configured_source,
                proposal=proposal,
            ),
        )
    reconciled: list[str] = []
    reconciled_batches: list[list[ProposalOperation]] = []
    conflicts: list[str] = []
    if overlay_only_recovery:
        reconciled = [
            operation.change.operation_id
            for operation in proposal.operations
            if (
                (
                    state := database.operation_outbox_state(
                        operation.change.operation_id
                    )
                )
                is not None
                and state["status"] == "done"
            )
        ]
    else:
        writer = ApprovedNotionWriter(
            _notion_read_gateway(config),
            database,
            proposal,
            _configured_data_sources(config, database),
            require_exact_schema=True,
        )
        write_operations = [
            operation
            for operation in proposal.operations
            if operation.action
            in {ProposalAction.APPLY, ProposalAction.EDIT}
        ]
        page_batches: list[list[ProposalOperation]] = []
        for operation in write_operations:
            target = (
                operation.change.target_database,
                operation.change.entity_key,
            )
            if page_batches:
                previous = page_batches[-1][0].change
                if target == (
                    previous.target_database,
                    previous.entity_key,
                ):
                    page_batches[-1].append(operation)
                    continue
            page_batches.append([operation])
        for page_batch in page_batches:
            candidates = [
                operation
                for operation in page_batch
                if (
                    (
                        state := database.operation_outbox_state(
                            operation.change.operation_id
                        )
                    )
                    is None
                    or state["status"] != "done"
                )
            ]
            if not candidates:
                continue
            inspections = writer.inspect_batch(candidates)
            already = [
                operation
                for operation in candidates
                if inspections[operation.change.operation_id]
                is WritePreconditionState.ALREADY_APPLIED
            ]
            reconciled.extend(
                operation.change.operation_id for operation in already
            )
            if already:
                reconciled_batches.append(already)
            conflicts.extend(
                operation.change.operation_id
                for operation in candidates
                if inspections[operation.change.operation_id]
                is WritePreconditionState.CONFLICT
            )
    if conflicts:
        raise HermesGatewayApprovalError(
            "Recovery has Notion conflicts for operations: " + ", ".join(conflicts)
        )
    if not overlay_only_recovery:
        for batch in reconciled_batches:
            writer.complete_batch(batch)
    recovery = proposals.create_recovery_revision(
        proposal.proposal_id,
        source_rebind=(
            (version_id, file_hash)
            if overlay_only_recovery
            else None
        ),
    )
    recovery = proposals.request_approval(recovery.proposal_id)
    _audit_gateway_command(
        database,
        RECOVER_COMMAND,
        identity,
        recovery.proposal_id,
        previous_revision=command.revision,
        revision=recovery.revision,
        reconciled_already_applied=reconciled,
        recovery_mode=(
            "wiki_overlay_only"
            if overlay_only_recovery
            else "notion_reconciliation"
        ),
        digest=recovery.digest,
    )
    return _proposal_screen(recovery, prefix="NX_RECOVERY_PROPOSAL_READY")


def _handle_nonapproval_command(
    command_name: str,
    text: str,
    config: AppConfig,
    database: StateDatabase,
    identity: TelegramEventIdentity,
    *,
    reject_command: ParsedProposalCommand | None = None,
) -> str:
    if command_name == SYNC_COMMAND:
        return _handle_sync_command(text, config, database, identity)
    if command_name == SHOW_COMMAND:
        return _handle_show_command(text, database, identity)
    if command_name == SET_COMMAND:
        return _handle_set_command(text, config, database, identity)
    if command_name == REJECT_COMMAND:
        return _handle_reject_command(
            text,
            database,
            identity,
            reject_command,
        )
    if command_name == RECOVER_COMMAND:
        return _handle_recover_command(text, config, database, identity)
    if command_name == CORRECT_COMMAND:
        return _handle_correction_command(
            text,
            config,
            database,
            identity,
        )
    raise AssertionError(f"Unsupported non-approval command: {command_name}")


def _correction_overlay_publisher(
    config: AppConfig,
    database: StateDatabase,
) -> Callable[[ProposalRevision, ApprovalReceipt], None]:
    """Build the post-Notion publisher for user-approved value overrides."""

    def publish(
        proposal: ProposalRevision,
        receipt: ApprovalReceipt,
    ) -> None:
        selected = []
        for operation in proposal.operations:
            if not operation.user_override:
                continue
            if operation.change.entity_key.startswith("history:"):
                continue
            state = database.operation_outbox_state(
                operation.change.operation_id
            )
            if (
                operation.action
                not in {ProposalAction.APPLY, ProposalAction.EDIT}
                and (state is None or state.get("status") != "done")
            ):
                continue
            selected.append(operation)
        if not selected:
            return
        if not config.wiki.enabled or config.wiki.output_root is None:
            raise RuntimeError(
                "A user-approved correction requires an enabled Wiki output"
            )
        cards = {
            card.operation_id: card
            for card in build_review_cards(proposal)
        }
        store = CorrectionOverlayStore(config.wiki.output_root)
        for operation in selected:
            change = operation.change
            event_id = "correction-" + sha256_json(
                {
                    "proposal_id": proposal.proposal_id,
                    "revision": proposal.revision,
                    "operation_id": change.operation_id,
                    "digest": proposal.digest,
                }
            )[:40]
            card = cards.get(change.operation_id)
            store.publish(
                ApprovedCorrectionEvent(
                    event_id=event_id,
                    proposal_id=proposal.proposal_id,
                    proposal_revision=proposal.revision,
                    proposal_digest=proposal.digest,
                    operation_id=change.operation_id,
                    case_number=(
                        str(
                            proposal.correction_binding.get("case_number")
                            or (
                                card.case_number
                                if card is not None
                                else change.entity_key
                            )
                        )
                    ),
                    target_database=change.target_database,
                    entity_key=change.entity_key,
                    property_name=change.property_name,
                    current_value=change.current_value,
                    approved_value=operation.approved_value,
                    reason=change.reason,
                    approved_by_user_id=receipt.telegram_user_id,
                    approved_in_chat_id=receipt.chat_id,
                    approved_at=receipt.issued_at,
                    source_refs_digest=sha256_json(
                        [
                            dataclass_to_dict(source_ref)
                            for source_ref in change.source_refs
                        ]
                    ),
                )
            )

    return publish


def _apply_gateway_receipt(
    config: AppConfig,
    database: StateDatabase,
    approval: ApprovalService,
    receipt: ApprovalReceipt,
) -> ApplyReport:
    """Apply inside the credential-bearing gateway process, never a model child."""

    write_token = config.secret(config.notion.write_token_env)
    assert write_token is not None
    data_sources = _configured_data_sources(config, database)
    verifier = ExactNotionMutationVerifier(
        approval,
        database,
        data_sources,
    )
    notion = HttpNotionGateway(
        write_token,
        verifier,
        notion_version=config.notion.api_version,
    )
    proposal = database.load_proposal(receipt.proposal_id, receipt.revision)
    writer = ApprovedNotionWriter(
        notion,
        database,
        proposal,
        data_sources,
        require_exact_schema=True,
    )
    onedrive = _source_client(config, database)
    version_id, file_hash = _capture_current_source(
        onedrive,
        config.onedrive.drive_id,
        config.onedrive.item_id,
    )
    wiki_index = WikiIndex(config.wiki.index_db)
    knowledge_guard = _wiki_live_binding_guard(
        config,
        onedrive,
        wiki_index,
        proposal,
    )

    def apply_under_current_generation() -> ApplyReport:
        return ApprovalGatedApplyService(
            database,
            approval,
            writer,
            correction_overlay_publisher=_correction_overlay_publisher(
                config,
                database,
            ),
        ).apply(
            receipt=receipt,
            drive_id=config.onedrive.drive_id,
            item_id=config.onedrive.item_id,
            current_source_version_id=version_id,
            current_source_file_hash=file_hash,
            source_version_guard=lambda: _latest_version(
                onedrive,
                config.onedrive.drive_id,
                config.onedrive.item_id,
            ).id,
            knowledge_binding_guard=knowledge_guard,
        )

    wiki_bound = bool(proposal.knowledge_binding) or any(
        operation.change.knowledge_refs for operation in proposal.operations
    )
    if not wiki_bound:
        return apply_under_current_generation()
    # The exact generation checked by the guard remains immutable until every
    # approved Notion operation has finished. A concurrent refresh waits and
    # publishes only after the apply lease has completed.
    with wiki_index.generation_lock(timeout_seconds=30.0):
        return apply_under_current_generation()


def _assert_gateway_authorized(gateway: Any, source: Any) -> None:
    authorization_check = getattr(gateway, "_is_user_authorized", None)
    if not callable(authorization_check):
        raise HermesGatewayApprovalError(
            "Hermes gateway authorization callback is unavailable"
        )
    try:
        authorized = bool(authorization_check(source))
    except Exception as exc:
        raise HermesGatewayApprovalError(
            "Hermes gateway authorization check failed"
        ) from exc
    if not authorized:
        raise HermesGatewayApprovalError("Telegram sender is not authorized by Hermes")


def _assert_project_authorized(
    identity: TelegramEventIdentity,
    config: AppConfig,
) -> None:
    if identity.user_id not in config.approval.allowed_telegram_users:
        raise HermesGatewayApprovalError(
            "Telegram sender is not in the project's approval allowlist"
        )


def _validate_target_binding(
    command: ParsedApprovalCommand,
    identity: TelegramEventIdentity,
    proposal: ProposalRevision,
) -> None:
    if identity.user_id != proposal.requested_by:
        raise HermesGatewayApprovalError("Only the proposal owner may approve it")
    if identity.chat_id != proposal.chat_id:
        raise HermesGatewayApprovalError(
            "Approval must come from the proposal's original Telegram chat"
        )
    if command.revision != proposal.revision:
        raise HermesGatewayApprovalError(
            "Only the current final proposal revision may be approved"
        )
    if not hmac.compare_digest(command.digest, proposal.digest):
        raise HermesGatewayApprovalError(
            "The Telegram digest does not match the current proposal"
        )


def _validate_pending_scope(
    command: ParsedApprovalCommand,
    identity: TelegramEventIdentity,
    proposal: ProposalRevision,
    *,
    now: datetime,
) -> None:
    _validate_target_binding(command, identity, proposal)
    if proposal.status is not ProposalStatus.PENDING_APPROVAL:
        raise HermesGatewayApprovalError("Proposal is not awaiting final approval")
    if unmerged_history_operation_ids(proposal.operations):
        raise HermesGatewayApprovalError(
            "Proposal contains generated history operations that were not "
            "displayed on a review card"
        )
    if proposal.expires_at is None or now >= proposal.expires_at:
        raise HermesGatewayApprovalError("Proposal approval window has expired")


def _receipt_directory(config: AppConfig) -> Path:
    return (config.work_dir.parent / "receipts").resolve()


def _receipt_path(config: AppConfig, receipt: ApprovalReceipt) -> Path:
    return resolve_receipt_reference(
        _receipt_directory(config),
        receipt_reference(receipt),
    )


_PUBLIC_OUTBOX_STATUSES = frozenset(
    {"blocked", "done", "failed", "pending", "unknown"}
)


def _public_outbox_state(
    operation_id: str,
    state: Mapping[str, object],
) -> dict[str, object]:
    """Remove internal paths and adapter errors from a Telegram/audit payload."""

    raw_status = state.get("status")
    status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in _PUBLIC_OUTBOX_STATUSES
        else "unknown"
    )
    raw_attempts = state.get("attempts")
    attempts = (
        raw_attempts
        if isinstance(raw_attempts, int) and raw_attempts >= 0
        else 0
    )
    result: dict[str, object] = {
        "operation_id": operation_id,
        "status": status,
        "attempts": attempts,
    }
    if status in {"blocked", "failed", "unknown"}:
        result["error_code"] = public_apply_failure_code(
            state.get("last_error")
        )
    return result


def _proposal_public_outbox(
    proposal: ProposalRevision,
    database: StateDatabase,
) -> list[dict[str, object]]:
    public: list[dict[str, object]] = []
    for operation in proposal.operations:
        operation_id = operation.change.operation_id
        state = database.operation_outbox_state(operation_id)
        if state is not None:
            public.append(_public_outbox_state(operation_id, state))
    return public


def _public_failure_codes(report: ApplyReport) -> dict[str, str]:
    return {
        operation_id: public_apply_failure_code(reason)
        for operation_id, reason in report.failed.items()
    }


def _apply_error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ApprovalError):
        return (
            "approval_state_invalid",
            "The approved synchronization state changed, so the protected apply stopped.",
        )
    if isinstance(exc, ProposalInternalScopeConflict):
        return (
            "proposal_duplicate_scope",
            "The prepared proposal contains multiple changes for the same "
            "Notion mutation scope.",
        )
    if isinstance(exc, MutationScopeConflict):
        return (
            "mutation_scope_conflict",
            "Another active proposal owns the same Notion mutation scope.",
        )
    return (
        "apply_worker_failed",
        "The protected apply could not be completed; use the recovery command after review.",
    )


def _public_denial_message(exc: Exception) -> str:
    """Map trusted workflow failures to non-sensitive Telegram text."""

    if isinstance(exc, HermesGatewayApprovalError):
        return exc.public_message
    if isinstance(exc, ApprovalError):
        return "Approval state or source bindings are no longer valid."
    if isinstance(exc, CorrectionRequestError):
        return "The correction command is invalid."
    if isinstance(exc, CorrectionEventClaimError):
        return "The Telegram correction event conflicts with an earlier request."
    if isinstance(exc, ProposalInternalScopeConflict):
        return (
            "The prepared proposal contains multiple changes for the same "
            "Notion mutation scope."
        )
    if isinstance(exc, MutationScopeConflict):
        return "Another active proposal owns the same Notion mutation scope."
    if isinstance(exc, ProposalRejectionError):
        return "The proposal state transition was rejected."
    if isinstance(exc, NotionSchemaError):
        return "The Notion target or schema did not match the approved scope."
    if isinstance(exc, ProposalError):
        return "The proposal state transition was rejected."
    return "The trusted workflow rejected the request."


def _public_denial_code(command_name: str, exc: Exception) -> str:
    """Return a stable, non-sensitive reason code for one rejected command."""

    if isinstance(exc, ProposalInternalScopeConflict):
        return "proposal_duplicate_scope"
    if isinstance(exc, MutationScopeConflict):
        return "active_proposal_scope_conflict"
    if command_name == APPROVAL_COMMAND:
        return "approval_rejected"
    return "command_rejected"


def _apply_result_marker(
    receipt: ApprovalReceipt,
    receipt_path: Path,
    report: ApplyReport,
    database: StateDatabase,
    config: AppConfig,
    *,
    redelivered: bool = False,
) -> str:
    reference = receipt_reference(receipt)
    if (
        resolve_receipt_reference(_receipt_directory(config), reference)
        != receipt_path.resolve()
    ):
        raise HermesGatewayApprovalError("Stored approval receipt reference is invalid")
    proposal = database.load_proposal(
        receipt.proposal_id,
        receipt.revision,
    )
    is_correction = (
        proposal.purpose is ProposalPurpose.USER_CORRECTION
    )
    changed = bool(report.applied or report.already_applied)
    mutation_state = (
        "confirmed" if changed else ("unknown" if report.failed else "none")
    )
    payload = json.dumps(
        {
            "proposal_id": receipt.proposal_id,
            "revision": receipt.revision,
            "digest": receipt.proposal_digest,
            "receipt_ref": reference,
            "redelivered": redelivered,
            "committed": report.committed,
            "applied": report.applied,
            "already_applied": report.already_applied,
            "excluded": report.excluded,
            "deferred": report.deferred,
            "failed_operations": sorted(report.failed),
            "failure_codes": _public_failure_codes(report),
            "failure_message": (
                "One or more approved operations could not be completed."
                if report.failed
                else None
            ),
            "notion_mutated": changed if not report.failed else (True if changed else None),
            "mutation_state": mutation_state,
            "purpose": proposal.purpose.value,
            "checkpoint_advanced": bool(
                report.committed and not is_correction
            ),
            "checkpoint": (
                database.get_checkpoint(
                    config.onedrive.drive_id,
                    config.onedrive.item_id,
                )
                if report.committed and not is_correction
                else None
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if is_correction:
        marker = (
            "NX_CORRECTION_COMMITTED"
            if report.committed
            else "NX_CORRECTION_FAILED"
        )
    else:
        marker = "NX_SYNC_COMMITTED" if report.committed else "NX_SYNC_FAILED"
    return (
        f"[{marker}] {payload} "
        "신뢰된 gateway가 Telegram 최종 승인을 인증하고 로컬 원본·Notion을 "
        "재검증한 뒤 승인 범위의 적용 시도를 완료했습니다."
    )


def _apply_error_marker(
    database: StateDatabase,
    receipt: ApprovalReceipt,
    exc: Exception,
) -> str:
    purpose = ProposalPurpose.EXCEL_SYNC
    try:
        proposal = database.load_proposal(receipt.proposal_id, receipt.revision)
        purpose = proposal.purpose
        outbox = _proposal_public_outbox(proposal, database)
        confirmed = any(state["status"] == "done" for state in outbox)
        unknown = bool(outbox) and proposal.status in {
            ProposalStatus.APPLYING,
            ProposalStatus.FAILED,
        }
        status = proposal.status.value
    except Exception:
        outbox = []
        confirmed = False
        unknown = True
        status = "unknown"
    error_code, message = _apply_error_details(exc)
    payload = json.dumps(
        {
            "proposal_id": receipt.proposal_id,
            "revision": receipt.revision,
            "digest": receipt.proposal_digest,
            "proposal_status": status,
            "error_code": error_code,
            "message": message,
            "outbox": outbox,
            "notion_mutated": True if confirmed else (None if unknown else False),
            "mutation_state": (
                "confirmed" if confirmed else ("unknown" if unknown else "none")
            ),
            "purpose": purpose.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    marker = (
        "NX_CORRECTION_ERROR"
        if purpose is ProposalPurpose.USER_CORRECTION
        else "NX_SYNC_ERROR"
    )
    return (
        f"[{marker}] {payload} "
        "승인 영수증은 존재하지만 보호된 적용 절차가 완료되지 않았습니다."
    )


def _committed_redelivery_marker(
    database: StateDatabase,
    config: AppConfig,
    proposal: ProposalRevision,
    identity: TelegramEventIdentity,
) -> str:
    is_correction = (
        proposal.purpose is ProposalPurpose.USER_CORRECTION
    )
    operations = _proposal_public_outbox(proposal, database)
    payload = {
        "proposal_id": proposal.proposal_id,
        "revision": proposal.revision,
        "digest": proposal.digest,
        "redelivered": True,
        "committed": True,
        "purpose": proposal.purpose.value,
        "checkpoint_advanced": not is_correction,
        "outbox": operations,
        "checkpoint": (
            None
            if is_correction
            else database.get_checkpoint(
                config.onedrive.drive_id,
                config.onedrive.item_id,
            )
        ),
    }
    database.audit(
        "sync_result_redelivered",
        {
            **payload,
            "telegram_message_id": identity.message_id,
            "telegram_update_id": identity.update_id,
        },
        proposal.proposal_id,
        identity.user_id,
    )
    marker = (
        "NX_CORRECTION_COMMITTED"
        if is_correction
        else "NX_SYNC_COMMITTED"
    )
    return (
        f"[{marker}] "
        + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + " 이 제안은 이미 완료되어 쓰기를 다시 실행하지 않았습니다."
    )


def _commit_approval(
    database: StateDatabase,
    proposal: ProposalRevision,
    receipt: ApprovalReceipt,
    identity: TelegramEventIdentity,
    receipt_ref: str,
) -> None:
    """Atomically save the receipt, approve the exact head, and write its audit."""

    receipt_payload = json.dumps(dataclass_to_dict(receipt), ensure_ascii=False)
    with database.transaction() as connection:
        committed_at = datetime.now(UTC)
        if proposal.expires_at is None or committed_at >= proposal.expires_at:
            raise HermesGatewayApprovalError(
                "Proposal approval window expired before the approval commit"
            )
        head = connection.execute(
            "SELECT current_revision, requested_by, chat_id, source_version_id, "
            "source_file_hash, status, expires_at FROM proposal WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()
        revision = connection.execute(
            "SELECT digest FROM proposal_revision WHERE proposal_id = ? AND revision = ?",
            (proposal.proposal_id, proposal.revision),
        ).fetchone()
        if head is None or revision is None:
            raise HermesGatewayApprovalError("Proposal disappeared before approval commit")

        expected_expiry = proposal.expires_at.isoformat() if proposal.expires_at else None
        if (
            int(head["current_revision"]) != proposal.revision
            or str(head["requested_by"]) != proposal.requested_by
            or str(head["chat_id"]) != proposal.chat_id
            or str(head["source_version_id"]) != proposal.source_version_id
            or str(head["source_file_hash"]) != proposal.source_file_hash
            or str(head["status"]) != ProposalStatus.PENDING_APPROVAL.value
            or head["expires_at"] != expected_expiry
            or not hmac.compare_digest(str(revision["digest"]), proposal.digest)
        ):
            raise HermesGatewayApprovalError(
                "Proposal changed while Telegram approval was being committed"
            )

        proposal.status = ProposalStatus.APPROVED
        proposal_payload = json.dumps(dataclass_to_dict(proposal), ensure_ascii=False)
        connection.execute(
            "INSERT INTO approval_receipt("
            "nonce, proposal_id, revision, payload, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                receipt.nonce,
                receipt.proposal_id,
                receipt.revision,
                receipt_payload,
                receipt.issued_at.isoformat(),
            ),
        )
        proposal_update = connection.execute(
            "UPDATE proposal SET status = ? WHERE proposal_id = ? "
            "AND current_revision = ? AND status = ?",
            (
                ProposalStatus.APPROVED.value,
                proposal.proposal_id,
                proposal.revision,
                ProposalStatus.PENDING_APPROVAL.value,
            ),
        )
        revision_update = connection.execute(
            "UPDATE proposal_revision SET payload = ? WHERE proposal_id = ? "
            "AND revision = ? AND digest = ?",
            (
                proposal_payload,
                proposal.proposal_id,
                proposal.revision,
                receipt.proposal_digest,
            ),
        )
        if proposal_update.rowcount != 1 or revision_update.rowcount != 1:
            raise HermesGatewayApprovalError(
                "Proposal head changed before approval could be finalized"
            )

        connection.execute(
            "INSERT INTO audit_log("
            "event_type, proposal_id, actor, details, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "proposal_approved",
                proposal.proposal_id,
                identity.user_id,
                json.dumps(
                    {
                        "revision": proposal.revision,
                        "digest": proposal.digest,
                        "nonce": receipt.nonce,
                        "approval_channel": "hermes_telegram_gateway",
                        "telegram_message_id": identity.message_id,
                        "telegram_update_id": identity.update_id,
                        "telegram_chat_id": identity.chat_id,
                        "telegram_chat_type": identity.chat_type,
                        "telegram_thread_id": identity.thread_id,
                        "telegram_event_timestamp": identity.event_timestamp,
                        "receipt_ref": receipt_ref,
                    },
                    ensure_ascii=False,
                ),
                committed_at.isoformat(),
            ),
        )


def _commit_reissued_approval(
    database: StateDatabase,
    proposal: ProposalRevision,
    previous: ApprovalReceipt,
    replacement: ApprovalReceipt,
    identity: TelegramEventIdentity,
    receipt_ref: str,
) -> None:
    """Atomically revoke an expired unused receipt and store its replacement."""

    replacement_payload = json.dumps(
        dataclass_to_dict(replacement),
        ensure_ascii=False,
    )
    with database.transaction() as connection:
        committed_at = datetime.now(UTC)
        head = connection.execute(
            "SELECT current_revision, requested_by, chat_id, source_version_id, "
            "source_file_hash, status FROM proposal WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()
        revision = connection.execute(
            "SELECT digest FROM proposal_revision WHERE proposal_id = ? AND revision = ?",
            (proposal.proposal_id, proposal.revision),
        ).fetchone()
        latest = connection.execute(
            "SELECT nonce, used_at FROM approval_receipt WHERE proposal_id = ? "
            "AND revision = ? ORDER BY created_at DESC LIMIT 1",
            (proposal.proposal_id, proposal.revision),
        ).fetchone()
        if head is None or revision is None or latest is None:
            raise HermesGatewayApprovalError(
                "Approved proposal changed before receipt reissue"
            )
        if (
            int(head["current_revision"]) != proposal.revision
            or str(head["requested_by"]) != proposal.requested_by
            or str(head["chat_id"]) != proposal.chat_id
            or str(head["source_version_id"]) != proposal.source_version_id
            or str(head["source_file_hash"]) != proposal.source_file_hash
            or str(head["status"]) != ProposalStatus.APPROVED.value
            or not hmac.compare_digest(str(revision["digest"]), proposal.digest)
            or str(latest["nonce"]) != previous.nonce
            or latest["used_at"] is not None
        ):
            raise HermesGatewayApprovalError(
                "Approved proposal or existing receipt changed before reissue"
            )
        revoked = connection.execute(
            "UPDATE approval_receipt SET used_at = ? WHERE nonce = ? AND used_at IS NULL",
            (committed_at.isoformat(), previous.nonce),
        )
        if revoked.rowcount != 1:
            raise HermesGatewayApprovalError("Existing receipt could not be revoked")
        connection.execute(
            "INSERT INTO approval_receipt("
            "nonce, proposal_id, revision, payload, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                replacement.nonce,
                replacement.proposal_id,
                replacement.revision,
                replacement_payload,
                replacement.issued_at.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO audit_log("
            "event_type, proposal_id, actor, details, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                "approval_receipt_reissued",
                proposal.proposal_id,
                identity.user_id,
                json.dumps(
                    {
                        "revision": proposal.revision,
                        "digest": proposal.digest,
                        "revoked_nonce": previous.nonce,
                        "replacement_nonce": replacement.nonce,
                        "telegram_message_id": identity.message_id,
                        "telegram_update_id": identity.update_id,
                        "telegram_chat_id": identity.chat_id,
                        "receipt_ref": receipt_ref,
                    },
                    ensure_ascii=False,
                ),
                committed_at.isoformat(),
            ),
        )


def _redeliver_existing_receipt(
    database: StateDatabase,
    approval: ApprovalService,
    config: AppConfig,
    command: ParsedApprovalCommand,
    identity: TelegramEventIdentity,
    proposal: ProposalRevision,
    *,
    now: datetime,
) -> tuple[ApprovalReceipt, Path]:
    """Return the existing exact receipt after a lost-marker retry."""

    _validate_target_binding(command, identity, proposal)
    if proposal.status not in {ProposalStatus.APPROVED, ProposalStatus.APPLYING}:
        raise HermesGatewayApprovalError("Proposal has no redeliverable approval receipt")
    try:
        receipt = database.load_latest_receipt(
            proposal.proposal_id,
            proposal.revision,
        )
    except KeyError as exc:
        raise HermesGatewayApprovalError(
            "The approved proposal has no stored receipt"
        ) from exc

    reissued = False
    if proposal.status is ProposalStatus.APPROVED and now >= receipt.expires_at:
        replacement = approval.reissue_expired_approved(
            receipt,
            proposal,
            telegram_user_id=identity.user_id,
            chat_id=identity.chat_id,
            confirmed_digest=command.digest,
            now=now,
        )
        _commit_reissued_approval(
            database,
            proposal,
            receipt,
            replacement,
            identity,
            receipt_reference(replacement),
        )
        receipt = replacement
        reissued = True
    else:
        approval.verify(
            receipt,
            proposal,
            current_source_version_id=proposal.source_version_id,
            current_source_file_hash=proposal.source_file_hash,
            now=now,
            require_used=proposal.status is ProposalStatus.APPLYING,
        )
    receipt_path = _receipt_path(config, receipt)
    save_receipt(receipt, receipt_path)
    database.audit(
        "approval_receipt_redelivered",
        {
            "revision": receipt.revision,
            "digest": receipt.proposal_digest,
            "nonce": receipt.nonce,
            "reissued": reissued,
            "approval_channel": "hermes_telegram_gateway",
            "telegram_message_id": identity.message_id,
            "telegram_update_id": identity.update_id,
            "telegram_chat_id": identity.chat_id,
            "telegram_chat_type": identity.chat_type,
            "telegram_thread_id": identity.thread_id,
            "telegram_event_timestamp": identity.event_timestamp,
            "receipt_ref": receipt_reference(receipt),
        },
        proposal.proposal_id,
        identity.user_id,
    )
    return receipt, receipt_path


_SYNC_PROGRESS_STAGES = frozenset({"authenticating", "preparing"})


def authenticated_sync_progress_marker(
    *,
    event: Any,
    gateway: Any,
    project_root: str | Path,
    stage: str = "preparing",
    elapsed_seconds: int = 0,
) -> str:
    """Return a safe progress marker after repeating the read-only auth checks.

    This path deliberately does not initialize the state database or inspect
    any source, Wiki, or Notion data. It is used only when an in-process sync
    worker already owns the local execution slot.
    """

    text_value = getattr(event, "text", None)
    if (
        recognized_gateway_command(text_value) != SYNC_COMMAND
        or not isinstance(text_value, str)
        or text_value.strip() != SYNC_COMMAND
    ):
        return _denied("invalid_syntax", "Use exactly: /nx_sync")
    try:
        source = getattr(event, "source", None)
        identity = _event_identity(event)
        _assert_gateway_authorized(gateway, source)
        config = load_config(_resolve_config_path(project_root))
        _assert_project_authorized(identity, config)
    except HermesGatewayApprovalError as exc:
        return _denied("command_rejected", _public_denial_message(exc))
    except Exception:
        logger.error("Hermes synchronization progress authentication failed closed")
        return _denied(
            "internal_error",
            "The trusted workflow worker encountered an internal error",
        )

    safe_stage = stage if stage in _SYNC_PROGRESS_STAGES else "preparing"
    if isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, int):
        safe_elapsed = 0
    else:
        safe_elapsed = min(max(elapsed_seconds, 0), 86_400)
    progress_text = (
        "A synchronization request is currently being authenticated."
        if safe_stage == "authenticating"
        else "An authenticated synchronization request is still running."
    )
    return (
        f"[NX_SYNC_IN_PROGRESS] stage={safe_stage} "
        f"elapsed_seconds={safe_elapsed}. "
        f"{progress_text} "
        "No additional worker was started, and this message does not approve "
        "a Notion change."
    )


def handle_pre_gateway_dispatch(
    *,
    event: Any,
    gateway: Any,
    project_root: str | Path,
    on_authenticated_sync_start: Callable[[], None] | None = None,
) -> str | None:
    """Consume and execute authenticated Telegram workflow commands.

    Unrelated events return ``None``. Every reserved command is consumed and
    returns a controlled marker, including malformed or unauthorized input, so
    none of these commands reaches the model as an ordinary instruction. Only
    the data-approval and schema-approval branches can access the gateway-only
    signing and write secrets under separate receipt domains.
    """

    text_value = getattr(event, "text", None)
    command_name = recognized_gateway_command(text_value)
    if command_name is None:
        return None
    text = str(text_value)
    approval_command: ParsedApprovalCommand | None = None
    reject_command: ParsedProposalCommand | None = None
    if command_name == APPROVAL_COMMAND:
        try:
            approval_command = _parse_command(text)
        except HermesGatewayApprovalError as exc:
            return _denied("invalid_syntax", exc.public_message)
    elif command_name == REJECT_COMMAND:
        try:
            reject_command = _parse_proposal_command(text, _REJECT_RE)
        except HermesGatewayApprovalError as exc:
            return _denied("invalid_syntax", exc.public_message)

    try:
        source = getattr(event, "source", None)
        identity = _event_identity(event)
        _assert_gateway_authorized(gateway, source)

        config_path = _resolve_config_path(project_root)
        config = load_config(config_path)
        _assert_project_authorized(identity, config)
        if (
            command_name == SYNC_COMMAND
            and text.strip() == SYNC_COMMAND
            and on_authenticated_sync_start is not None
        ):
            try:
                on_authenticated_sync_start()
            except Exception:
                logger.error(
                    "Authenticated synchronization start notification failed"
                )
        database = StateDatabase(config.state_db)
        try:
            database.initialize()
        except MutationScopeConflict:
            if reject_command is None:
                raise
            database.initialize_for_legacy_rejection()
        if command_name in SCHEMA_COMMANDS:
            from notion_excel_sync.security.hermes_schema_gateway import (
                handle_schema_command,
            )

            return handle_schema_command(event, text, config, database)
        if command_name != APPROVAL_COMMAND:
            return _handle_nonapproval_command(
                command_name,
                text,
                config,
                database,
                identity,
                reject_command=reject_command,
            )

        assert approval_command is not None
        command = approval_command
        proposal = database.load_proposal(command.proposal_id)
        now = datetime.now(UTC)
        if proposal.status is ProposalStatus.COMMITTED:
            _validate_target_binding(command, identity, proposal)
            return _committed_redelivery_marker(
                database,
                config,
                proposal,
                identity,
            )
        approval = _approval_service(config, database)
        redelivered = False
        if proposal.status in {ProposalStatus.APPROVED, ProposalStatus.APPLYING}:
            receipt, receipt_path = _redeliver_existing_receipt(
                database,
                approval,
                config,
                command,
                identity,
                proposal,
                now=now,
            )
            redelivered = True
        else:
            _validate_pending_scope(command, identity, proposal, now=now)
            receipt = approval.issue(
                proposal,
                telegram_user_id=identity.user_id,
                chat_id=identity.chat_id,
                confirmed_digest=command.digest,
                now=now,
            )
            receipt_path = _receipt_path(config, receipt)
            save_receipt(receipt, receipt_path)
            try:
                _commit_approval(
                    database,
                    proposal,
                    receipt,
                    identity,
                    receipt_reference(receipt),
                )
            except Exception:
                receipt_path.unlink(missing_ok=True)
                raise

        try:
            report = _apply_gateway_receipt(
                config,
                database,
                approval,
                receipt,
            )
        except Exception as exc:
            error_code, _ = _apply_error_details(exc)
            logger.error(
                "Trusted Hermes gateway apply failed closed (code=%s)",
                error_code,
            )
            return _apply_error_marker(database, receipt, exc)
        return _apply_result_marker(
            receipt,
            receipt_path,
            report,
            database,
            config,
            redelivered=redelivered,
        )
    except KeyError:
        return _denied("proposal_not_found", "Proposal was not found")
    except (
        ApprovalError,
        CorrectionEventClaimError,
        CorrectionRequestError,
        HermesGatewayApprovalError,
        MutationScopeConflict,
        NotionSchemaError,
        ProposalError,
        ProposalRejectionError,
    ) as exc:
        code = _public_denial_code(command_name, exc)
        return _denied(code, _public_denial_message(exc))
    except Exception:
        logger.exception("Hermes Telegram workflow command failed closed")
        return _denied(
            "internal_error",
            "The trusted workflow worker encountered an internal error",
        )

    raise AssertionError("recognized gateway command reached an impossible state")
