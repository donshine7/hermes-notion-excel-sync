from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator, Protocol

from notion_excel_sync.adapters.notion import (
    ApprovalRequiredError,
    HttpNotionGateway,
    NotionDatabaseCreateResult,
    NotionError,
    SchemaApprovalVerifier,
)
from notion_excel_sync.config import AppConfig
from notion_excel_sync.models import (
    SCHEMA_APPROVAL_PURPOSE,
    SchemaApprovalReceipt,
    SchemaProposalRevision,
    SchemaProposalStatus,
    SchemaRecoveryReceipt,
    dataclass_to_dict,
    schema_approval_command_hash,
    schema_recovery_command_hash,
    sha256_json,
)
from notion_excel_sync.persistence.database import (
    ApplyLeaseError,
    SchemaEventClaimError,
    SchemaStateError,
    StateDatabase,
)
from notion_excel_sync.security.schema_approval import (
    SchemaApprovalError,
    SchemaApprovalService,
)
from notion_excel_sync.security.schema_recovery import (
    SchemaRecoveryError,
    SchemaRecoveryService,
)
from notion_excel_sync.workflow.notion_schema import (
    REMOTE_MARKER_PREFIX,
    SCHEMA_API_VERSION,
    SCHEMA_TEMPLATES,
    CreatedSchemaBinding,
    DirectChildIdentity,
    NotionSchemaSecurityError,
    ParentPageBinding,
    ReconciliationDisposition,
    RelationLiveBinding,
    RelationTarget,
    RemoteSchemaCandidate,
    SchemaCreatePlan,
    SchemaLiveBinding,
    WriteBotBinding,
    assert_schema_live_binding_matches,
    build_schema_create_plan,
    capture_schema_live_binding,
    decide_schema_reconciliation,
    normalize_notion_id,
    schema_template,
    verify_created_schema,
)


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

_PROPOSAL_ID = r"SP-[A-Za-z0-9._:-]{1,125}"
_NOTION_ID = (
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)
_TEMPLATE_PATTERN = "|".join(
    re.escape(template_id) for template_id in sorted(SCHEMA_TEMPLATES)
)
_PLAN_RE = re.compile(rf"/nx_schema_plan ({_TEMPLATE_PATTERN}) ({_NOTION_ID})")
_SHOW_RE = re.compile(rf"/nx_schema_show ({_PROPOSAL_ID}) ([1-9][0-9]*)(?: ([1-9][0-9]*))?")
_REJECT_RE = re.compile(rf"/nx_schema_reject ({_PROPOSAL_ID}) ([1-9][0-9]*)")
_APPROVE_RE = re.compile(
    rf"/nx_schema_approve ({_PROPOSAL_ID}) ([1-9][0-9]*) ([0-9A-Fa-f]{{64}})"
)
_RECOVER_RE = re.compile(
    rf"/nx_schema_recover ({_PROPOSAL_ID}) ([1-9][0-9]*) ([0-9A-Fa-f]{{64}})"
)
_SHOW_PAGE_SIZE = 7
_REMOTE_CLOCK_SKEW = timedelta(seconds=5)


class HermesSchemaGatewayError(PermissionError):
    """A safe, user-correctable denial in the schema command coordinator."""


class _SchemaRemoteReader(Protocol):
    def get_self(self) -> Mapping[str, Any]: ...

    def get_page(self, page_id: str) -> Mapping[str, Any]: ...

    def list_block_children(
        self, block_id: str, *, page_size: int = 100
    ) -> list[Mapping[str, Any]]: ...

    def get_database(self, database_id: str) -> Mapping[str, Any]: ...

    def get_data_source(self, data_source_id: str) -> Mapping[str, Any]: ...


class _SchemaRemoteGateway(_SchemaRemoteReader, Protocol):
    def create_database(
        self,
        expected_body: Mapping[str, Any],
        *,
        operation_id: str,
        approval: SchemaApprovalReceipt,
        precondition: Mapping[str, Any],
    ) -> NotionDatabaseCreateResult: ...


class _GetOnlySchemaReader:
    """Expose only Notion GET operations to reconciliation code."""

    def __init__(self, source: _SchemaRemoteReader) -> None:
        self._source = source

    def get_self(self) -> Mapping[str, Any]:
        return self._source.get_self()

    def get_page(self, page_id: str) -> Mapping[str, Any]:
        return self._source.get_page(page_id)

    def list_block_children(
        self,
        block_id: str,
        *,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        return self._source.list_block_children(block_id, page_size=page_size)

    def get_database(self, database_id: str) -> Mapping[str, Any]:
        return self._source.get_database(database_id)

    def get_data_source(self, data_source_id: str) -> Mapping[str, Any]:
        return self._source.get_data_source(data_source_id)


@dataclass(frozen=True, slots=True)
class TelegramSchemaEventIdentity:
    user_id: str
    chat_id: str
    thread_id: str
    message_id: str
    update_id: int


@dataclass(frozen=True, slots=True)
class ParsedSchemaCommand:
    command_name: str
    schema_proposal_id: str | None = None
    revision: int | None = None
    digest: str | None = None
    page: int = 1
    template_id: str | None = None
    parent_page_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RemoteSnapshot:
    parent_page: Mapping[str, Any]
    write_bot: Mapping[str, Any]
    direct_child_databases: tuple[Mapping[str, Any], ...]
    relation_ids: Mapping[str, str]
    relation_data_sources: Mapping[str, Mapping[str, Any]]
    live_binding: SchemaLiveBinding


GatewayFactory = Callable[
    [AppConfig, SchemaApprovalVerifier | None],
    _SchemaRemoteGateway,
]


def _default_gateway_factory(
    config: AppConfig,
    verifier: SchemaApprovalVerifier | None,
) -> _SchemaRemoteGateway:
    token = config.secret(config.notion.write_token_env)
    assert token is not None
    return HttpNotionGateway(
        token,
        lambda *_: False,
        schema_approval_verifier=verifier,
        notion_version=config.notion.api_version,
    )


_gateway_factory: GatewayFactory = _default_gateway_factory


def _configured_schema_parent_page_id(config: AppConfig) -> str:
    raw_parent = getattr(config.notion, "schema_parent_page_id", None)
    if not isinstance(raw_parent, str) or not raw_parent.strip():
        raise HermesSchemaGatewayError(
            "notion.schema_parent_page_id 설정이 필요합니다."
        )
    try:
        return normalize_notion_id(raw_parent)
    except NotionSchemaSecurityError as exc:
        raise HermesSchemaGatewayError(
            "notion.schema_parent_page_id 설정이 올바른 Notion ID가 아닙니다."
        ) from exc


def is_schema_command(text: object) -> bool:
    """Recognize reserved schema tokens, including malformed invocations."""

    if not isinstance(text, str):
        return False
    tokens = text.strip().split(maxsplit=1)
    return bool(tokens and tokens[0] in SCHEMA_COMMANDS)


def _denied(code: str, message: str) -> str:
    return (
        f"[NX_SCHEMA_DENIED code={code}] {message} "
        "스키마 승인 영수증은 발급되지 않았고 Notion도 변경되지 않았습니다."
    )


def _safe_failure(code: str, message: str) -> str:
    return f"[NX_SCHEMA_{code}] {message}"


def _event_identity(event: Any) -> TelegramSchemaEventIdentity:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", "") if source is not None else ""
    platform_name = str(getattr(platform, "value", platform) or "").strip().lower()
    if source is None or platform_name != "telegram":
        raise HermesSchemaGatewayError("스키마 명령은 Telegram 업데이트에서만 허용됩니다.")
    if bool(getattr(source, "is_bot", False)):
        raise HermesSchemaGatewayError("봇이 작성한 스키마 명령은 허용되지 않습니다.")
    user_id = str(getattr(source, "user_id", "") or "").strip()
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    message_id = str(
        getattr(source, "message_id", "")
        or getattr(event, "message_id", "")
        or ""
    ).strip()
    raw_update_id = getattr(event, "platform_update_id", None)
    if not user_id or not chat_id or not message_id or raw_update_id is None:
        raise HermesSchemaGatewayError(
            "Telegram 사용자·채팅·메시지·업데이트 식별자가 모두 필요합니다."
        )
    try:
        update_id = int(raw_update_id)
    except (TypeError, ValueError) as exc:
        raise HermesSchemaGatewayError("Telegram update_id가 올바르지 않습니다.") from exc
    if isinstance(raw_update_id, bool) or update_id < 0:
        raise HermesSchemaGatewayError("Telegram update_id가 올바르지 않습니다.")
    return TelegramSchemaEventIdentity(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        update_id=update_id,
    )


def _parse_command(text: str) -> ParsedSchemaCommand:
    normalized = text.strip()
    command = normalized.split(maxsplit=1)[0]
    if command == SCHEMA_PLAN_COMMAND:
        match = _PLAN_RE.fullmatch(normalized)
        if match is None:
            raise HermesSchemaGatewayError(
                "정확한 형식: /nx_schema_plan "
                "<government-support-evidence|case-history> <parent_page_id>"
            )
        return ParsedSchemaCommand(
            command_name="nx_schema_plan",
            template_id=match.group(1),
            parent_page_id=normalize_notion_id(match.group(2)),
        )
    if command == SCHEMA_SHOW_COMMAND:
        match = _SHOW_RE.fullmatch(normalized)
        if match is None:
            raise HermesSchemaGatewayError(
                "정확한 형식: /nx_schema_show <SP-id> <revision> [page]"
            )
        return ParsedSchemaCommand(
            command_name="nx_schema_show",
            schema_proposal_id=match.group(1),
            revision=int(match.group(2)),
            page=int(match.group(3) or 1),
        )
    if command == SCHEMA_REJECT_COMMAND:
        match = _REJECT_RE.fullmatch(normalized)
        if match is None:
            raise HermesSchemaGatewayError(
                "정확한 형식: /nx_schema_reject <SP-id> <revision>"
            )
        return ParsedSchemaCommand(
            command_name="nx_schema_reject",
            schema_proposal_id=match.group(1),
            revision=int(match.group(2)),
        )
    if command == SCHEMA_APPROVE_COMMAND:
        match = _APPROVE_RE.fullmatch(normalized)
        if match is None:
            raise HermesSchemaGatewayError(
                "정확한 형식: /nx_schema_approve <SP-id> <revision> <64hex_digest>"
            )
        return ParsedSchemaCommand(
            command_name="nx_schema_approve",
            schema_proposal_id=match.group(1),
            revision=int(match.group(2)),
            digest=match.group(3).lower(),
        )
    if command == SCHEMA_RECOVER_COMMAND:
        match = _RECOVER_RE.fullmatch(normalized)
        if match is None:
            raise HermesSchemaGatewayError(
                "정확한 형식: /nx_schema_recover <SP-id> <revision> <64hex_digest>"
            )
        return ParsedSchemaCommand(
            command_name="nx_schema_recover",
            schema_proposal_id=match.group(1),
            revision=int(match.group(2)),
            digest=match.group(3).lower(),
        )
    raise HermesSchemaGatewayError("지원하지 않는 스키마 명령입니다.")


def _command_hash(
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
) -> str:
    if parsed.command_name == "nx_schema_approve":
        assert parsed.schema_proposal_id is not None
        assert parsed.revision is not None
        assert parsed.digest is not None
        return schema_approval_command_hash(
            parsed.schema_proposal_id,
            parsed.revision,
            parsed.digest,
        )
    if parsed.command_name == "nx_schema_recover":
        assert parsed.schema_proposal_id is not None
        assert parsed.revision is not None
        assert parsed.digest is not None
        return schema_recovery_command_hash(
            parsed.schema_proposal_id,
            parsed.revision,
            parsed.digest,
            telegram_user_id=identity.user_id,
            chat_id=identity.chat_id,
            thread_id=identity.thread_id,
            telegram_message_id=identity.message_id,
            telegram_update_id=identity.update_id,
        )
    return sha256_json(
        {
            "purpose": SCHEMA_APPROVAL_PURPOSE,
            "command": parsed.command_name,
            "schema_proposal_id": parsed.schema_proposal_id,
            "revision": parsed.revision,
            "digest": parsed.digest,
            "page": parsed.page,
            "template_id": parsed.template_id,
            "parent_page_id": parsed.parent_page_id,
            "telegram_user_id": identity.user_id,
            "chat_id": identity.chat_id,
            "thread_id": identity.thread_id,
            "message_id": identity.message_id,
            "update_id": identity.update_id,
        }
    )


def _malformed_command_hash(text: str, identity: TelegramSchemaEventIdentity) -> str:
    return sha256_json(
        {
            "purpose": SCHEMA_APPROVAL_PURPOSE,
            "command_text": text.strip(),
            "telegram_user_id": identity.user_id,
            "chat_id": identity.chat_id,
            "thread_id": identity.thread_id,
            "message_id": identity.message_id,
            "update_id": identity.update_id,
        }
    )


def _plan_seed(
    command_hash: str,
    identity: TelegramSchemaEventIdentity,
) -> str:
    return sha256_json(
        {
            "purpose": SCHEMA_APPROVAL_PURPOSE,
            "command_hash": command_hash,
            "chat_id": identity.chat_id,
            "thread_id": identity.thread_id,
            "message_id": identity.message_id,
            "update_id": identity.update_id,
        }
    )


def _approval_service(config: AppConfig, database: StateDatabase) -> SchemaApprovalService:
    secret = config.secret(config.approval.secret_env)
    assert secret is not None
    return SchemaApprovalService(
        secret,
        config.approval.allowed_telegram_users,
        ttl_seconds=config.approval.ttl_seconds,
        nonce_used=database.schema_nonce_used,
    )


def _recovery_service(config: AppConfig, database: StateDatabase) -> SchemaRecoveryService:
    secret = config.secret(config.approval.secret_env)
    assert secret is not None
    return SchemaRecoveryService(
        secret,
        config.approval.allowed_telegram_users,
        ttl_seconds=config.approval.ttl_seconds,
        nonce_used=database.schema_recovery_nonce_used,
    )


def _validate_owner(
    identity: TelegramSchemaEventIdentity,
    proposal: SchemaProposalRevision,
    revision: int,
) -> None:
    if identity.user_id != proposal.requested_by:
        raise HermesSchemaGatewayError("이 스키마 제안을 만든 사용자만 명령할 수 있습니다.")
    if identity.chat_id != proposal.chat_id or identity.thread_id != proposal.thread_id:
        raise HermesSchemaGatewayError(
            "원래 스키마 제안을 만든 Telegram 채팅과 스레드에서만 명령할 수 있습니다."
        )
    if proposal.revision != revision:
        raise HermesSchemaGatewayError("요청한 스키마 제안 revision이 최신 revision과 다릅니다.")


def _relation_ids(config: AppConfig, template_id: str) -> dict[str, str]:
    result: dict[str, str] = {}
    logical_names = tuple(
        dict.fromkeys(
            logical_name
            for _, logical_name in schema_template(template_id).relations
        )
    )
    for logical_name in logical_names:
        value = str(config.notion.databases.get(logical_name, "")).strip()
        if not value or value.upper().startswith("REPLACE_"):
            raise HermesSchemaGatewayError(
                f"관계 대상 '{logical_name}'의 Notion data_source_id가 준비되지 않았습니다."
            )
        result[logical_name] = normalize_notion_id(value)
    return result


def _direct_child_databases(
    gateway: _SchemaRemoteReader,
    parent_page_id: str,
) -> tuple[Mapping[str, Any], ...]:
    databases: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for child in gateway.list_block_children(parent_page_id, page_size=100):
        if child.get("object") != "block" or child.get("type") != "child_database":
            continue
        database_id = normalize_notion_id(child.get("id"))
        if database_id in seen:
            raise HermesSchemaGatewayError("Notion 최상위 하위 DB 목록에 중복 ID가 있습니다.")
        seen.add(database_id)
        databases.append(gateway.get_database(database_id))
    return tuple(databases)


def _capture_snapshot(
    gateway: _SchemaRemoteReader,
    config: AppConfig,
    *,
    template_id: str,
    parent_page_id: str,
    expected_write_bot_id: str | None = None,
) -> _RemoteSnapshot:
    relation_ids = _relation_ids(config, template_id)
    parent_page = gateway.get_page(parent_page_id)
    write_bot = gateway.get_self()
    children = _direct_child_databases(gateway, parent_page_id)
    relation_sources = {
        name: gateway.get_data_source(data_source_id)
        for name, data_source_id in relation_ids.items()
    }
    binding = capture_schema_live_binding(
        template_id=template_id,
        parent_page=parent_page,
        write_bot=write_bot,
        direct_child_databases=children,
        relation_data_source_ids=relation_ids,
        relation_data_sources=relation_sources,
        expected_write_bot_id=expected_write_bot_id,
        expected_parent_page_id=parent_page_id,
    )
    return _RemoteSnapshot(
        parent_page=parent_page,
        write_bot=write_bot,
        direct_child_databases=children,
        relation_ids=relation_ids,
        relation_data_sources=relation_sources,
        live_binding=binding,
    )


def _binding_from_dict(raw: object) -> SchemaLiveBinding:
    if not isinstance(raw, Mapping):
        raise HermesSchemaGatewayError("저장된 스키마 live binding이 올바르지 않습니다.")
    parent = raw.get("parent")
    bot = raw.get("write_bot")
    relations = raw.get("relations")
    conflicts = raw.get("direct_child_conflicts", [])
    if (
        not isinstance(parent, Mapping)
        or not isinstance(bot, Mapping)
        or not isinstance(relations, Sequence)
        or isinstance(relations, (str, bytes, bytearray))
        or not isinstance(conflicts, Sequence)
        or isinstance(conflicts, (str, bytes, bytearray))
    ):
        raise HermesSchemaGatewayError("저장된 스키마 live binding이 올바르지 않습니다.")
    return SchemaLiveBinding(
        parent=ParentPageBinding(page_id=str(parent.get("page_id", ""))),
        write_bot=WriteBotBinding(bot_id=str(bot.get("bot_id", ""))),
        relations=tuple(
            RelationLiveBinding(
                logical_name=str(item.get("logical_name", "")),
                data_source_id=str(item.get("data_source_id", "")),
                parent_database_id=str(item.get("parent_database_id", "")),
                display_name=str(item.get("display_name", "")),
            )
            for item in relations
            if isinstance(item, Mapping)
        ),
        direct_child_conflicts=tuple(
            DirectChildIdentity(
                database_id=str(item.get("database_id", "")),
                title=str(item.get("title", "")),
                marker=str(item.get("marker", "")),
                archived=bool(item.get("archived", False)),
                in_trash=bool(item.get("in_trash", False)),
            )
            for item in conflicts
            if isinstance(item, Mapping)
        ),
    )


def _plan_from_proposal(
    proposal: SchemaProposalRevision,
    expected_parent_page_id: str,
) -> SchemaCreatePlan:
    raw = proposal.schema_payload
    try:
        relation_rows = raw["relation_targets"]
        if not isinstance(relation_rows, Sequence) or isinstance(
            relation_rows, (str, bytes, bytearray)
        ):
            raise TypeError
        relation_targets = tuple(
            RelationTarget(
                property_name=str(item["property_name"]),
                logical_name=str(item["logical_name"]),
                data_source_id=str(item["data_source_id"]),
            )
            for item in relation_rows
            if isinstance(item, Mapping)
        )
        binding = _binding_from_dict(raw["approved_live_binding"])
        plan = SchemaCreatePlan(
            template_id=str(raw["template_id"]),
            template_version=int(raw["template_version"]),
            logical_name=str(raw["logical_name"]),
            purpose=str(raw["purpose"]),
            api_version=str(raw["api_version"]),
            parent_page_id=str(raw["parent_page_id"]),
            write_bot_id=str(raw["write_bot_id"]),
            remote_marker=str(raw["remote_marker"]),
            relation_targets=relation_targets,
            approved_live_binding=binding,
        )
    except (KeyError, TypeError, ValueError, NotionSchemaSecurityError) as exc:
        raise HermesSchemaGatewayError("저장된 스키마 계획을 안전하게 복원할 수 없습니다.") from exc
    if not hmac.compare_digest(
        sha256_json(plan.digest_payload()),
        sha256_json(raw),
    ):
        raise HermesSchemaGatewayError("저장된 스키마 계획의 내용이 변경되었습니다.")
    if not hmac.compare_digest(
        sha256_json(asdict(binding)),
        sha256_json(proposal.precondition),
    ):
        raise HermesSchemaGatewayError("저장된 스키마 사전조건이 계획과 다릅니다.")
    if (
        proposal.purpose != SCHEMA_APPROVAL_PURPOSE
        or proposal.logical_database_name != plan.logical_name
        or normalize_notion_id(proposal.parent_page_id) != plan.parent_page_id
        or plan.parent_page_id != expected_parent_page_id
    ):
        raise HermesSchemaGatewayError("스키마 제안의 승인 범위가 올바르지 않습니다.")
    return plan


def _proposal_screen(proposal: SchemaProposalRevision) -> str:
    return "\n".join(
        [
            "[NX_SCHEMA_PROPOSAL]",
            f"Proposal: {proposal.schema_proposal_id}",
            f"Revision: {proposal.revision}",
            f"Status: {proposal.status.value}",
            f"Database: {proposal.logical_database_name}",
            f"Parent: {proposal.parent_page_id}",
            f"Digest: {proposal.digest}",
            f"검토: /nx_schema_show {proposal.schema_proposal_id} {proposal.revision}",
            "최종 승인(전체 digest를 그대로 사용):",
            (
                f"/nx_schema_approve {proposal.schema_proposal_id} "
                f"{proposal.revision} {proposal.digest}"
            ),
            "아직 Notion은 변경되지 않았습니다.",
        ]
    )


def _proposal_page(
    proposal: SchemaProposalRevision,
    page: int,
    expected_parent_page_id: str,
) -> str:
    plan = _plan_from_proposal(proposal, expected_parent_page_id)
    properties = list(plan.create_body["initial_data_source"]["properties"].items())
    total_pages = max(1, (len(properties) + _SHOW_PAGE_SIZE - 1) // _SHOW_PAGE_SIZE)
    if page > total_pages:
        raise HermesSchemaGatewayError(f"page는 1-{total_pages} 범위여야 합니다.")
    start = (page - 1) * _SHOW_PAGE_SIZE
    selected = properties[start : start + _SHOW_PAGE_SIZE]
    lines = [
        f"[NX_SCHEMA {proposal.schema_proposal_id} r{proposal.revision}]",
        f"Status: {proposal.status.value}",
        f"Digest: {proposal.digest}",
        f"Template: {plan.template_id} v{plan.template_version}",
        f"API: {plan.api_version}",
        f"Parent: {plan.parent_page_id}",
        f"Write bot: {plan.write_bot_id}",
        f"Remote marker: {plan.remote_marker}",
        "Layout: full-page child database (is_inline=false)",
        "Approved direct-child conflicts: 0",
        f"Page: {page}/{total_pages}",
    ]
    for index, (name, definition) in enumerate(selected, start=start + 1):
        property_type = next(iter(definition))
        detail = ""
        if property_type == "relation":
            detail = f" -> {definition['relation']['data_source_id']}"
        elif property_type == "select":
            options = definition["select"]["options"]
            detail = " [" + ", ".join(
                f"{option['name']}:{option['color']}" for option in options
            ) + "]"
        elif property_type == "number":
            detail = f" ({definition['number']['format']})"
        lines.append(f"{index}. {name}: {property_type}{detail}")
    if page < total_pages:
        lines.append(
            f"다음: /nx_schema_show {proposal.schema_proposal_id} {proposal.revision} {page + 1}"
        )
    lines.extend(
        [
            f"거절: /nx_schema_reject {proposal.schema_proposal_id} {proposal.revision}",
            "최종 승인(전체 digest를 그대로 사용):",
            (
                f"/nx_schema_approve {proposal.schema_proposal_id} "
                f"{proposal.revision} {proposal.digest}"
            ),
            "아직 Notion은 변경되지 않았습니다.",
        ]
    )
    return "\n".join(lines)


def _status_copy(
    proposal: SchemaProposalRevision,
    status: SchemaProposalStatus,
) -> SchemaProposalRevision:
    return replace(proposal, status=status)


def _mark_preapply_stale(
    database: StateDatabase,
    proposal: SchemaProposalRevision,
    *,
    reason_code: str,
) -> None:
    current = database.load_schema_proposal(
        proposal.schema_proposal_id,
        proposal.revision,
    )
    if current.status is SchemaProposalStatus.PENDING_APPROVAL:
        database.save_schema_proposal(
            _status_copy(current, SchemaProposalStatus.STALE)
        )
    elif current.status is SchemaProposalStatus.APPROVED:
        database.mark_schema_proposal_stale(
            current,
            reason_code=reason_code,
        )


def _target_is_unconfigured(config: AppConfig, logical_name: str) -> bool:
    configured = str(config.notion.databases.get(logical_name, "")).strip()
    return not configured or configured.upper().startswith("REPLACE_")


def _has_active_schema_target(
    database: StateDatabase,
    *,
    logical_name: str,
    exclude_proposal_id: str | None = None,
    include_pending: bool,
) -> bool:
    active = database.load_active_schema_proposal(logical_name)
    if active is None or active.schema_proposal_id == exclude_proposal_id:
        return False
    if not include_pending and active.status in {
        SchemaProposalStatus.DRAFT,
        SchemaProposalStatus.PENDING_APPROVAL,
    }:
        return False
    return True


@contextmanager
def _schema_target_lease(
    database: StateDatabase,
    proposal: SchemaProposalRevision,
) -> Iterator[None]:
    # The keys are domain-separated from OneDrive source leases. This fallback
    # provides an atomic, cross-process target lock until StateDatabase exposes
    # a schema-specific lease with the same semantics.
    with database.apply_lease(
        drive_id=SCHEMA_APPROVAL_PURPOSE,
        item_id=proposal.parent_page_id,
        proposal_id=proposal.schema_proposal_id,
        revision=proposal.revision,
    ):
        yield


def _dispatch_plan(
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
    config: AppConfig,
    database: StateDatabase,
    command_hash: str,
    expected_parent_page_id: str,
) -> str:
    assert parsed.template_id is not None and parsed.parent_page_id is not None
    contract = schema_template(parsed.template_id)
    if parsed.parent_page_id != expected_parent_page_id:
        raise HermesSchemaGatewayError("이 템플릿의 허용된 최상위 Notion 페이지만 사용할 수 있습니다.")
    if config.notion.api_version != SCHEMA_API_VERSION:
        raise HermesSchemaGatewayError("고정 스키마 템플릿과 Notion API 버전이 다릅니다.")
    if not _target_is_unconfigured(config, contract.logical_name):
        raise HermesSchemaGatewayError(
            f"{contract.logical_name} data source가 이미 설정되어 있습니다."
        )
    if database.get_schema_installation(contract.logical_name) is not None:
        raise HermesSchemaGatewayError(
            f"{contract.logical_name} 스키마가 이미 설치되어 있습니다."
        )

    seed = _plan_seed(command_hash, identity)
    proposal_id = f"SP-{seed[:32]}"
    try:
        existing = database.load_schema_proposal(proposal_id, 1)
    except KeyError:
        existing = None
    if existing is not None:
        _validate_owner(identity, existing, 1)
        return _proposal_screen(existing)
    if _has_active_schema_target(
        database,
        logical_name=contract.logical_name,
        include_pending=True,
    ):
        raise HermesSchemaGatewayError(
            "같은 대상의 미완료 스키마 제안이 있습니다. 먼저 기존 제안을 처리해야 합니다."
        )

    gateway = _gateway_factory(config, None)
    snapshot = _capture_snapshot(
        gateway,
        config,
        template_id=parsed.template_id,
        parent_page_id=parsed.parent_page_id,
    )
    plan = build_schema_create_plan(
        parsed.template_id,
        parent_page=snapshot.parent_page,
        write_bot=snapshot.write_bot,
        direct_child_databases=snapshot.direct_child_databases,
        relation_data_source_ids=snapshot.relation_ids,
        relation_data_sources=snapshot.relation_data_sources,
        expected_parent_page_id=expected_parent_page_id,
        api_version=config.notion.api_version,
    )
    plan = replace(plan, remote_marker=f"{REMOTE_MARKER_PREFIX}{seed[32:64]}")
    now = datetime.now(UTC)
    proposal = SchemaProposalRevision(
        schema_proposal_id=proposal_id,
        revision=1,
        requested_by=identity.user_id,
        chat_id=identity.chat_id,
        thread_id=identity.thread_id,
        operation_id=f"schema-create-{seed[:32]}",
        logical_database_name=contract.logical_name,
        parent_page_id=expected_parent_page_id,
        schema_payload=plan.digest_payload(),
        precondition=dataclass_to_dict(plan.approved_live_binding),
        status=SchemaProposalStatus.PENDING_APPROVAL,
        created_at=now,
        expires_at=now + timedelta(seconds=config.approval.ttl_seconds),
    )
    database.save_schema_proposal(proposal)
    return _proposal_screen(proposal)


def _dispatch_show(
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
    database: StateDatabase,
    expected_parent_page_id: str,
) -> str:
    assert parsed.schema_proposal_id is not None and parsed.revision is not None
    proposal = database.load_schema_proposal(parsed.schema_proposal_id)
    _validate_owner(identity, proposal, parsed.revision)
    return _proposal_page(proposal, parsed.page, expected_parent_page_id)


def _dispatch_reject(
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
    database: StateDatabase,
    expected_parent_page_id: str,
) -> str:
    assert parsed.schema_proposal_id is not None and parsed.revision is not None
    proposal = database.load_schema_proposal(parsed.schema_proposal_id)
    _validate_owner(identity, proposal, parsed.revision)
    _plan_from_proposal(proposal, expected_parent_page_id)
    if proposal.status is SchemaProposalStatus.REJECTED:
        return _safe_failure("REJECTED", "스키마 제안은 이미 거절되었습니다. Notion은 변경되지 않았습니다.")
    if proposal.status is not SchemaProposalStatus.PENDING_APPROVAL:
        raise HermesSchemaGatewayError("승인 대기 중인 스키마 제안만 거절할 수 있습니다.")
    database.save_schema_proposal(_status_copy(proposal, SchemaProposalStatus.REJECTED))
    return _safe_failure("REJECTED", "스키마 제안을 거절했습니다. Notion은 변경되지 않았습니다.")


class _ExactCreateVerifier:
    allows_expired_started_receipt = True

    def __init__(
        self,
        *,
        database: StateDatabase,
        approval_service: SchemaApprovalService,
        proposal: SchemaProposalRevision,
        plan: SchemaCreatePlan,
    ) -> None:
        self._database = database
        self._approval_service = approval_service
        self._proposal = proposal
        self._plan = plan

    def __call__(
        self,
        receipt: SchemaApprovalReceipt,
        operation_id: str,
        expected_body: Mapping[str, Any],
        precondition: Mapping[str, Any],
        notion_version: str,
    ) -> bool:
        try:
            current = self._database.load_schema_proposal(
                self._proposal.schema_proposal_id,
                self._proposal.revision,
            )
            journal = self._database.schema_apply_journal_state(operation_id)
        except (KeyError, SchemaStateError):
            return False
        return (
            current.status is SchemaProposalStatus.APPLYING
            and hmac.compare_digest(current.digest, self._proposal.digest)
            and self._approval_service.verify_operation_scope(
                receipt,
                current,
                operation_id,
            )
            and journal is not None
            and journal.get("status") == "pending"
            and int(journal.get("attempts") or 0) >= 1
            and notion_version == self._plan.api_version
            and hmac.compare_digest(
                sha256_json(expected_body),
                sha256_json(self._plan.create_body),
            )
            and hmac.compare_digest(
                sha256_json(precondition),
                sha256_json(current.precondition),
            )
        )


def _unknown_result(proposal: SchemaProposalRevision) -> str:
    return "\n".join(
        [
            "[NX_SCHEMA_UNKNOWN] 생성 요청의 원격 결과를 확정할 수 없습니다.",
            "같은 승인으로 생성 요청을 다시 보내지 않습니다.",
            "읽기 전용 확인 명령:",
            (
                f"/nx_schema_recover {proposal.schema_proposal_id} "
                f"{proposal.revision} {proposal.digest}"
            ),
        ]
    )


def _recovered_result() -> str:
    return "\n".join(
        [
            "[NX_SCHEMA_RECOVERED] 기존 원격 DB를 읽기 전용 검증으로 확정해 등록했습니다.",
            "복구 과정에서는 Notion 쓰기 요청을 보내지 않았습니다.",
            "데이터 동기화에는 새로운 /nx_sync 요청과 별도 승인이 필요합니다.",
        ]
    )


def _mark_unknown(database: StateDatabase, proposal: SchemaProposalRevision) -> None:
    database.mark_schema_apply_journal(
        proposal.operation_id,
        "unknown",
        error="outcome_unknown",
    )


def _unknown_after_attempt(
    database: StateDatabase,
    proposal: SchemaProposalRevision,
) -> str:
    """Return UNKNOWN even if the local terminal-state write itself fails.

    Once the create call can have reached Notion, a local persistence error can
    never downgrade the user-facing result to a claim that no mutation occurred.
    Durable event completion still prevents the same Telegram update from
    issuing a second POST.
    """

    try:
        _mark_unknown(database, proposal)
    except Exception:
        pass
    return _unknown_result(proposal)


def _journal_timestamp(
    journal: Mapping[str, Any],
    field: str,
    *,
    label: str,
) -> datetime:
    raw = journal.get(field)
    if not isinstance(raw, str) or not raw:
        raise HermesSchemaGatewayError(f"스키마 생성 시도 {label} 시각을 확인할 수 없습니다.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HermesSchemaGatewayError(
            f"스키마 생성 시도 {label} 시각이 올바르지 않습니다."
        ) from exc
    if parsed.tzinfo is None:
        raise HermesSchemaGatewayError(
            f"스키마 생성 시도 {label} 시각에 시간대가 없습니다."
        )
    return parsed.astimezone(UTC)


def _journal_attempt_window(
    journal: Mapping[str, Any],
) -> tuple[datetime, datetime]:
    started = _journal_timestamp(journal, "attempt_started_at", label="시작")
    finished = _journal_timestamp(journal, "attempt_finished_at", label="종료")
    if finished < started:
        raise HermesSchemaGatewayError("스키마 생성 시도 시간 범위가 올바르지 않습니다.")
    return started - _REMOTE_CLOCK_SKEW, finished + _REMOTE_CLOCK_SKEW


def _dispatch_approve(
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
    config: AppConfig,
    database: StateDatabase,
    expected_parent_page_id: str,
) -> str:
    assert parsed.schema_proposal_id is not None
    assert parsed.revision is not None
    assert parsed.digest is not None
    proposal = database.load_schema_proposal(parsed.schema_proposal_id)
    _validate_owner(identity, proposal, parsed.revision)
    if not hmac.compare_digest(parsed.digest, proposal.digest):
        raise HermesSchemaGatewayError("전체 스키마 digest가 최신 제안과 일치하지 않습니다.")
    plan = _plan_from_proposal(proposal, expected_parent_page_id)
    if database.get_schema_installation(plan.logical_name) is not None:
        if proposal.status is SchemaProposalStatus.COMMITTED:
            return _safe_failure("COMMITTED", "스키마가 이미 정확히 설치되어 있습니다.")
        raise HermesSchemaGatewayError("동일한 논리 DB 설치가 이미 등록되어 있습니다.")
    if proposal.status in {SchemaProposalStatus.APPLYING, SchemaProposalStatus.UNKNOWN}:
        return _unknown_result(proposal)
    if proposal.status is SchemaProposalStatus.COMMITTED:
        return _safe_failure("COMMITTED", "스키마가 이미 정확히 설치되어 있습니다.")
    if proposal.status not in {
        SchemaProposalStatus.PENDING_APPROVAL,
        SchemaProposalStatus.APPROVED,
    }:
        raise HermesSchemaGatewayError("이 스키마 제안은 더 이상 승인할 수 없습니다.")
    if (
        proposal.status is SchemaProposalStatus.PENDING_APPROVAL
        and proposal.expires_at is not None
        and datetime.now(UTC) >= proposal.expires_at.astimezone(UTC)
    ):
        database.save_schema_proposal(
            _status_copy(proposal, SchemaProposalStatus.EXPIRED)
        )
        raise HermesSchemaGatewayError("스키마 제안의 승인 시간이 만료되었습니다.")

    service = _approval_service(config, database)
    with _schema_target_lease(database, proposal):
        if (
            config.notion.api_version != plan.api_version
            or plan.api_version != SCHEMA_API_VERSION
        ):
            _mark_preapply_stale(
                database,
                proposal,
                reason_code="api_version_changed",
            )
            raise HermesSchemaGatewayError(
                "승인 전 Notion API 버전이 고정 계획과 달라졌습니다."
            )
        if not _target_is_unconfigured(config, plan.logical_name):
            _mark_preapply_stale(
                database,
                proposal,
                reason_code="target_configuration_changed",
            )
            raise HermesSchemaGatewayError(
                f"승인 전 {plan.logical_name} data source 설정이 달라졌습니다."
            )
        if _has_active_schema_target(
            database,
            logical_name=plan.logical_name,
            exclude_proposal_id=proposal.schema_proposal_id,
            include_pending=False,
        ):
            raise HermesSchemaGatewayError("다른 승인된 스키마 작업이 같은 대상을 점유하고 있습니다.")
        read_gateway = _gateway_factory(config, None)
        try:
            current = _capture_snapshot(
                read_gateway,
                config,
                template_id=plan.template_id,
                parent_page_id=plan.parent_page_id,
                expected_write_bot_id=plan.write_bot_id,
            )
            assert_schema_live_binding_matches(plan.approved_live_binding, current.live_binding)
        except NotionSchemaSecurityError:
            _mark_preapply_stale(
                database,
                proposal,
                reason_code="live_binding_drift",
            )
            raise HermesSchemaGatewayError(
                "승인 후 Notion 상위 페이지·관계 DB·작성 봇 상태가 달라졌습니다. "
                "안전을 위해 이 승인으로 생성하지 않습니다."
            )

        try:
            if proposal.status is SchemaProposalStatus.PENDING_APPROVAL:
                receipt = service.issue(
                    proposal,
                    telegram_user_id=identity.user_id,
                    chat_id=identity.chat_id,
                    thread_id=identity.thread_id,
                    telegram_message_id=identity.message_id,
                    telegram_update_id=identity.update_id,
                    confirmed_digest=parsed.digest,
                )
                receipt = database.commit_schema_approval(proposal, receipt)
                proposal = database.load_schema_proposal(
                    proposal.schema_proposal_id,
                    proposal.revision,
                )
            else:
                try:
                    receipt = database.load_schema_receipt_for_event(
                        schema_proposal_id=proposal.schema_proposal_id,
                        revision=proposal.revision,
                        proposal_digest=proposal.digest,
                        telegram_user_id=identity.user_id,
                        chat_id=identity.chat_id,
                        thread_id=identity.thread_id,
                        telegram_message_id=identity.message_id,
                        telegram_update_id=identity.update_id,
                    )
                except SchemaEventClaimError as exc:
                    database.mark_schema_proposal_stale(
                        proposal,
                        reason_code="approval_event_changed",
                    )
                    raise HermesSchemaGatewayError(
                        "이전 승인 이벤트는 사용을 시작하지 못했습니다. "
                        "새 계획과 새 승인이 필요합니다."
                    ) from exc
                if receipt is None:
                    raise HermesSchemaGatewayError(
                        "현재 Telegram 이벤트에 결속된 스키마 승인이 없습니다."
                    )
                if datetime.now(UTC) >= receipt.expires_at.astimezone(UTC):
                    database.mark_schema_proposal_stale(
                        proposal,
                        reason_code="unused_receipt_expired",
                    )
                    raise HermesSchemaGatewayError(
                        "사용을 시작하지 않은 스키마 승인 영수증이 만료되었습니다."
                    )

            service.verify(
                receipt,
                proposal,
                telegram_user_id=identity.user_id,
                chat_id=identity.chat_id,
                thread_id=identity.thread_id,
                telegram_message_id=identity.message_id,
                telegram_update_id=identity.update_id,
            )
            database.begin_schema_apply(proposal, receipt.nonce)
        except Exception:
            current_proposal = database.load_schema_proposal(
                proposal.schema_proposal_id,
                proposal.revision,
            )
            if current_proposal.status is SchemaProposalStatus.APPROVED:
                database.mark_schema_proposal_stale(
                    current_proposal,
                    reason_code="preapply_aborted",
                )
            raise
        proposal = database.load_schema_proposal(
            proposal.schema_proposal_id,
            proposal.revision,
        )
        verifier = _ExactCreateVerifier(
            database=database,
            approval_service=service,
            proposal=proposal,
            plan=plan,
        )
        try:
            gateway = _gateway_factory(config, verifier)
            second = _capture_snapshot(
                gateway,
                config,
                template_id=plan.template_id,
                parent_page_id=plan.parent_page_id,
                expected_write_bot_id=plan.write_bot_id,
            )
            assert_schema_live_binding_matches(
                plan.approved_live_binding,
                second.live_binding,
            )
        except Exception as exc:
            database.fail_schema_apply(
                proposal,
                reason_code="precondition_drift",
            )
            raise HermesSchemaGatewayError(
                "생성 직전 Notion 상태 재검증에 실패했습니다. 원격 생성은 보내지 않았습니다."
            ) from exc

        database.mark_schema_apply_journal(proposal.operation_id, "pending")
        attempt_journal = database.schema_apply_journal_state(proposal.operation_id)
        if attempt_journal is None:
            raise HermesSchemaGatewayError("스키마 생성 시도 저널을 확인할 수 없습니다.")
        try:
            created = gateway.create_database(
                plan.create_body,
                operation_id=proposal.operation_id,
                approval=receipt,
                precondition=proposal.precondition,
            )
        except ApprovalRequiredError:
            database.fail_schema_apply(
                proposal,
                reason_code="approval_verifier_rejected",
                attempted_outcome_known_absent=True,
            )
            return _safe_failure(
                "CREATE_BLOCKED",
                "생성 요청이 원격 전송 전에 승인 검증 단계에서 차단되었습니다.",
            )
        except NotionError as exc:
            if not exc.mutation_outcome_unknown:
                database.fail_schema_apply(
                    proposal,
                    reason_code="remote_rejected_no_create",
                    attempted_outcome_known_absent=True,
                )
                return _safe_failure(
                    "CREATE_REJECTED",
                    "Notion이 생성 요청을 명확히 거절하여 DB가 생성되지 않았습니다.",
                )
            return _unknown_after_attempt(database, proposal)
        except Exception:
            return _unknown_after_attempt(database, proposal)

        try:
            created_database_id = normalize_notion_id(created.database_id)
            created_data_source_id = normalize_notion_id(created.data_source_id)
            database.mark_schema_apply_journal(
                proposal.operation_id,
                "pending",
                remote_database_id=created_database_id,
                remote_data_source_id=created_data_source_id,
            )
            finished_journal = database.schema_apply_journal_state(
                proposal.operation_id
            )
            if finished_journal is None:
                raise HermesSchemaGatewayError(
                    "스키마 생성 완료 시간 범위를 확인할 수 없습니다."
                )
            attempt_started_at, attempt_finished_at = _journal_attempt_window(
                finished_journal
            )
            journal_remote_ids = (
                str(finished_journal.get("remote_database_id") or ""),
                str(finished_journal.get("remote_data_source_id") or ""),
            )
            remote_database = gateway.get_database(created_database_id)
            remote_data_source = gateway.get_data_source(created_data_source_id)
            binding = verify_created_schema(
                plan,
                remote_database,
                remote_data_source,
                attempt_started_at=attempt_started_at,
                observed_at=attempt_finished_at,
                journal_remote_ids=journal_remote_ids,
            )
            if (
                binding.database_id != created_database_id
                or binding.data_source_id != created_data_source_id
            ):
                raise HermesSchemaGatewayError("생성 응답과 검증된 원격 ID가 다릅니다.")
            database.mark_schema_apply_journal(
                proposal.operation_id,
                "done",
                remote_database_id=binding.database_id,
                remote_data_source_id=binding.data_source_id,
            )
            database.finalize_schema_installation(
                proposal,
                database_id=binding.database_id,
                data_source_id=binding.data_source_id,
            )
        except Exception:
            return _unknown_after_attempt(database, proposal)

    return "\n".join(
        [
            (
                f"[NX_SCHEMA_COMMITTED] {plan.logical_name} DB를 "
                "정확히 검증하고 등록했습니다."
            ),
            f"Proposal: {proposal.schema_proposal_id}",
            f"Revision: {proposal.revision}",
            "Excel 체크포인트와 데이터 변경 대기열은 변경하지 않았습니다.",
            "데이터 동기화에는 새로운 /nx_sync 요청과 별도 승인이 필요합니다.",
        ]
    )


def _plain_rich_text(value: object) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ""
    parts: list[str] = []
    for row in value:
        if not isinstance(row, Mapping):
            return ""
        plain = row.get("plain_text")
        if isinstance(plain, str):
            parts.append(plain)
            continue
        text = row.get("text")
        content = text.get("content") if isinstance(text, Mapping) else None
        if not isinstance(content, str):
            return ""
        parts.append(content)
    return "".join(parts)


def _recovery_candidates(
    gateway: _SchemaRemoteReader,
    plan: SchemaCreatePlan,
    children: Sequence[Mapping[str, Any]],
) -> tuple[RemoteSchemaCandidate, ...]:
    candidates: list[RemoteSchemaCandidate] = []
    for remote_database in children:
        title = _plain_rich_text(remote_database.get("title"))
        marker = _plain_rich_text(remote_database.get("description"))
        if title != plan.logical_name and marker != plan.remote_marker:
            continue
        rows = remote_database.get("data_sources")
        if (
            not isinstance(rows, Sequence)
            or isinstance(rows, (str, bytes, bytearray))
            or len(rows) != 1
            or not isinstance(rows[0], Mapping)
        ):
            raise HermesSchemaGatewayError("관련 Notion DB의 data source 구성이 정확하지 않습니다.")
        data_source_id = normalize_notion_id(rows[0].get("id"))
        candidates.append(
            RemoteSchemaCandidate(
                database=remote_database,
                data_source=gateway.get_data_source(data_source_id),
            )
        )
    return tuple(candidates)


def _dispatch_recover(
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
    config: AppConfig,
    database: StateDatabase,
    command_hash: str,
    expected_parent_page_id: str,
) -> str:
    assert parsed.schema_proposal_id is not None
    assert parsed.revision is not None
    assert parsed.digest is not None
    proposal = database.load_schema_proposal(parsed.schema_proposal_id)
    _validate_owner(identity, proposal, parsed.revision)
    if not hmac.compare_digest(parsed.digest, proposal.digest):
        raise HermesSchemaGatewayError("전체 스키마 digest가 최신 제안과 일치하지 않습니다.")
    plan = _plan_from_proposal(proposal, expected_parent_page_id)
    if proposal.status is SchemaProposalStatus.COMMITTED:
        recovery_receipt = database.load_schema_recovery_receipt_for_event(
            schema_proposal_id=proposal.schema_proposal_id,
            revision=proposal.revision,
            proposal_digest=proposal.digest,
            telegram_user_id=identity.user_id,
            chat_id=identity.chat_id,
            thread_id=identity.thread_id,
            telegram_message_id=identity.message_id,
            telegram_update_id=identity.update_id,
        )
        if recovery_receipt is not None and database.schema_recovery_nonce_used(
            recovery_receipt.nonce
        ):
            return _recovered_result()
        return _safe_failure("COMMITTED", "스키마가 이미 정확히 설치되어 있습니다.")
    if proposal.status not in {SchemaProposalStatus.APPLYING, SchemaProposalStatus.UNKNOWN}:
        raise HermesSchemaGatewayError("읽기 전용 복구가 필요한 상태가 아닙니다.")
    receipt = database.load_latest_schema_receipt(
        proposal.schema_proposal_id,
        proposal.revision,
    )
    service = _approval_service(config, database)
    applying_scope = _status_copy(proposal, SchemaProposalStatus.APPLYING)
    legacy_authority = service.verify_operation_scope(
        receipt,
        applying_scope,
        proposal.operation_id,
    )
    journal = database.schema_apply_journal_state(proposal.operation_id)
    if journal is None:
        raise HermesSchemaGatewayError("스키마 생성 저널이 없습니다.")
    recovery_receipt: SchemaRecoveryReceipt | None = None
    recovery_service: SchemaRecoveryService | None = None
    if not legacy_authority:
        if proposal.status is not SchemaProposalStatus.UNKNOWN:
            raise HermesSchemaGatewayError(
                "재시작 복구 권한은 UNKNOWN 생성 시도에만 사용할 수 있습니다."
            )
        recovery_claim = database.claim_schema_recovery_event(
            proposal,
            telegram_user_id=identity.user_id,
            chat_id=identity.chat_id,
            thread_id=identity.thread_id,
            telegram_message_id=identity.message_id,
            telegram_update_id=identity.update_id,
            command_hash=command_hash,
        )
        if recovery_claim.get("status") == "completed":
            return str(recovery_claim.get("safe_result") or "")
        recovery_service = _recovery_service(config, database)
        recovery_receipt = database.load_schema_recovery_receipt_for_event(
            schema_proposal_id=proposal.schema_proposal_id,
            revision=proposal.revision,
            proposal_digest=proposal.digest,
            telegram_user_id=identity.user_id,
            chat_id=identity.chat_id,
            thread_id=identity.thread_id,
            telegram_message_id=identity.message_id,
            telegram_update_id=identity.update_id,
        )
        if recovery_receipt is None:
            evidence = database.schema_recovery_evidence(
                proposal,
                telegram_user_id=identity.user_id,
                chat_id=identity.chat_id,
                thread_id=identity.thread_id,
                telegram_message_id=identity.message_id,
                telegram_update_id=identity.update_id,
                command_hash=command_hash,
            )
            recovery_receipt = recovery_service.issue(
                proposal,
                telegram_user_id=identity.user_id,
                chat_id=identity.chat_id,
                thread_id=identity.thread_id,
                telegram_message_id=identity.message_id,
                telegram_update_id=identity.update_id,
                confirmed_digest=parsed.digest,
                raw_database_id=str(evidence["raw_database_id"]),
                raw_data_source_id=str(evidence["raw_data_source_id"]),
                canonical_database_id=str(evidence["canonical_database_id"]),
                canonical_data_source_id=str(evidence["canonical_data_source_id"]),
                attempt_started_at=evidence["attempt_started_at"],
                attempt_finished_at=evidence["attempt_finished_at"],
                approval_receipt_nonce=str(evidence["approval_receipt_nonce"]),
                conflict_telegram_update_id=int(
                    evidence["conflict_telegram_update_id"]
                ),
            )
            recovery_receipt = database.commit_schema_recovery_authority(
                proposal,
                recovery_receipt,
            )
        recovery_service.verify(
            recovery_receipt,
            proposal,
            telegram_user_id=identity.user_id,
            chat_id=identity.chat_id,
            thread_id=identity.thread_id,
            telegram_message_id=identity.message_id,
            telegram_update_id=identity.update_id,
            raw_database_id=str(journal.get("remote_database_id") or ""),
            raw_data_source_id=str(journal.get("remote_data_source_id") or ""),
            attempt_started_at=_journal_timestamp(
                journal,
                "attempt_started_at",
                label="시작",
            ),
            attempt_finished_at=_journal_timestamp(
                journal,
                "attempt_finished_at",
                label="종료",
            ),
        )

    with _schema_target_lease(database, proposal):
        gateway = _GetOnlySchemaReader(_gateway_factory(config, None))
        snapshot = _capture_snapshot(
            gateway,
            config,
            template_id=plan.template_id,
            parent_page_id=plan.parent_page_id,
            expected_write_bot_id=plan.write_bot_id,
        )
        try:
            candidates = _recovery_candidates(
                gateway,
                plan,
                snapshot.direct_child_databases,
            )
        except HermesSchemaGatewayError:
            return _safe_failure(
                "RECOVERY_CONFLICT",
                "읽기 전용 검사에서 관련 DB 구성이 계획과 다름을 확인했습니다. 자동 채택하지 않습니다.",
            )
        attempts = int(journal.get("attempts") or 0)
        if attempts > 0:
            try:
                attempt_started_at, attempt_finished_at = _journal_attempt_window(
                    journal
                )
            except HermesSchemaGatewayError:
                return _unknown_result(proposal)
        else:
            attempt_started_at = datetime.now(UTC)
            attempt_finished_at = attempt_started_at
        journal_database_id = str(journal.get("remote_database_id") or "")
        journal_data_source_id = str(journal.get("remote_data_source_id") or "")
        if bool(journal_database_id) != bool(journal_data_source_id):
            return _safe_failure(
                "RECOVERY_CONFLICT",
                "저널의 원격 DB·data source ID 쌍이 불완전하여 자동 채택하지 않습니다.",
            )
        journal_remote_ids = (
            (journal_database_id, journal_data_source_id)
            if journal_database_id and journal_data_source_id
            else None
        )
        decision = decide_schema_reconciliation(
            plan,
            current_live_binding=snapshot.live_binding,
            candidates=candidates,
            create_was_attempted=attempts > 0,
            attempt_started_at=attempt_started_at,
            observed_at=attempt_finished_at,
            journal_remote_ids=journal_remote_ids,
        )
        if decision.disposition is not ReconciliationDisposition.ALREADY_CREATED:
            if decision.disposition is ReconciliationDisposition.SAFE_TO_CREATE:
                database.fail_schema_apply(
                    proposal,
                    reason_code="no_create_attempt",
                )
                return _safe_failure(
                    "RECOVERY_EMPTY",
                    "생성 시도와 원격 DB가 모두 확인되지 않았습니다. 자동 생성하지 않습니다.",
                )
            if decision.disposition is ReconciliationDisposition.CONFLICT:
                return _safe_failure(
                    "RECOVERY_CONFLICT",
                    "원격 DB가 승인된 marker·작성자·기간·전체 스키마와 정확히 일치하지 않아 자동 채택하지 않습니다.",
                )
            return _unknown_result(proposal)

        binding: CreatedSchemaBinding | None = decision.binding
        if binding is None:
            raise HermesSchemaGatewayError("복구 결과에 검증된 원격 binding이 없습니다.")
        if (
            journal_database_id
            and normalize_notion_id(journal_database_id) != binding.database_id
        ) or (
            journal_data_source_id
            and normalize_notion_id(journal_data_source_id) != binding.data_source_id
        ):
            return _safe_failure(
                "RECOVERY_CONFLICT",
                "저널의 원격 ID와 검증된 DB가 다르므로 자동 채택하지 않습니다.",
            )
        if recovery_receipt is not None:
            assert recovery_service is not None
            if (
                binding.database_id != recovery_receipt.canonical_database_id
                or binding.data_source_id
                != recovery_receipt.canonical_data_source_id
            ):
                return _safe_failure(
                    "RECOVERY_CONFLICT",
                    "검증된 원격 ID가 새 복구 권한의 저널 ID와 다릅니다.",
                )
            recovery_service.verify(
                recovery_receipt,
                proposal,
                telegram_user_id=identity.user_id,
                chat_id=identity.chat_id,
                thread_id=identity.thread_id,
                telegram_message_id=identity.message_id,
                telegram_update_id=identity.update_id,
                raw_database_id=journal_database_id,
                raw_data_source_id=journal_data_source_id,
                attempt_started_at=_journal_timestamp(
                    journal,
                    "attempt_started_at",
                    label="시작",
                ),
                attempt_finished_at=_journal_timestamp(
                    journal,
                    "attempt_finished_at",
                    label="종료",
                ),
            )
            database.finalize_schema_recovery_installation(
                proposal,
                recovery_receipt,
                database_id=binding.database_id,
                data_source_id=binding.data_source_id,
            )
        else:
            journal_status = (
                "unknown"
                if proposal.status is SchemaProposalStatus.UNKNOWN
                else "done"
            )
            committed_database_id = journal_database_id or binding.database_id
            committed_data_source_id = journal_data_source_id or binding.data_source_id
            database.mark_schema_apply_journal(
                proposal.operation_id,
                journal_status,
                remote_database_id=committed_database_id,
                remote_data_source_id=committed_data_source_id,
            )
            database.finalize_schema_installation(
                proposal,
                database_id=committed_database_id,
                data_source_id=committed_data_source_id,
            )
    return _recovered_result()


def _dispatch(
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
    config: AppConfig,
    database: StateDatabase,
    command_hash: str,
) -> str:
    expected_parent_page_id = _configured_schema_parent_page_id(config)
    if parsed.command_name == "nx_schema_plan":
        return _dispatch_plan(
            parsed,
            identity,
            config,
            database,
            command_hash,
            expected_parent_page_id,
        )
    if parsed.command_name == "nx_schema_show":
        return _dispatch_show(
            parsed,
            identity,
            database,
            expected_parent_page_id,
        )
    if parsed.command_name == "nx_schema_reject":
        return _dispatch_reject(
            parsed,
            identity,
            database,
            expected_parent_page_id,
        )
    if parsed.command_name == "nx_schema_approve":
        return _dispatch_approve(
            parsed,
            identity,
            config,
            database,
            expected_parent_page_id,
        )
    if parsed.command_name == "nx_schema_recover":
        return _dispatch_recover(
            parsed,
            identity,
            config,
            database,
            command_hash,
            expected_parent_page_id,
        )
    raise HermesSchemaGatewayError("지원하지 않는 스키마 명령입니다.")


def _complete_result(
    database: StateDatabase,
    *,
    parsed: ParsedSchemaCommand,
    identity: TelegramSchemaEventIdentity,
    command_hash: str,
    result: str,
) -> str:
    if (
        parsed.command_name == "nx_schema_recover"
        and parsed.schema_proposal_id is not None
        and parsed.revision is not None
    ):
        try:
            proposal = database.load_schema_proposal(
                parsed.schema_proposal_id,
                parsed.revision,
            )
            recovery_claim = database.complete_schema_recovery_event(
                proposal,
                telegram_user_id=identity.user_id,
                chat_id=identity.chat_id,
                thread_id=identity.thread_id,
                telegram_message_id=identity.message_id,
                telegram_update_id=identity.update_id,
                command_hash=command_hash,
                safe_result=result,
            )
            if recovery_claim is not None:
                result = str(recovery_claim.get("safe_result") or result)
        except KeyError:
            pass
    claim = database.complete_schema_gateway_event(
        command_name=parsed.command_name,
        telegram_user_id=identity.user_id,
        chat_id=identity.chat_id,
        thread_id=identity.thread_id,
        message_id=identity.message_id,
        update_id=identity.update_id,
        command_hash=command_hash,
        schema_proposal_id=parsed.schema_proposal_id,
        safe_result=result,
    )
    return str(claim.get("safe_result") or result)


def handle_schema_command(
    event: Any,
    text: str,
    config: AppConfig,
    database: StateDatabase,
) -> str:
    """Handle one reserved schema command with durable Telegram replay safety.

    The function never performs an unapproved schema mutation. It also returns
    only bounded, prewritten messages: remote exception bodies, credentials,
    and local paths are intentionally excluded from both results and audit rows.
    """

    if not is_schema_command(text):
        return _denied("NOT_SCHEMA_COMMAND", "지원하는 스키마 명령이 아닙니다.")
    try:
        identity = _event_identity(event)
    except HermesSchemaGatewayError as exc:
        return _denied("TELEGRAM_IDENTITY", str(exc))

    try:
        parsed = _parse_command(text)
        command_hash = _command_hash(parsed, identity)
        if parsed.command_name == "nx_schema_plan":
            parsed = replace(
                parsed,
                schema_proposal_id=f"SP-{_plan_seed(command_hash, identity)[:32]}",
            )
    except (HermesSchemaGatewayError, NotionSchemaSecurityError) as exc:
        command_name = text.strip().split(maxsplit=1)[0].lstrip("/").lower()
        malformed = ParsedSchemaCommand(command_name=command_name)
        command_hash = _malformed_command_hash(text, identity)
        try:
            claim = database.claim_schema_gateway_event(
                command_name=malformed.command_name,
                telegram_user_id=identity.user_id,
                chat_id=identity.chat_id,
                thread_id=identity.thread_id,
                message_id=identity.message_id,
                update_id=identity.update_id,
                command_hash=command_hash,
            )
            if claim.get("status") == "completed":
                return str(claim.get("safe_result") or "")
            return _complete_result(
                database,
                parsed=malformed,
                identity=identity,
                command_hash=command_hash,
                result=_denied("MALFORMED", str(exc)),
            )
        except (SchemaEventClaimError, SchemaStateError):
            return _denied("EVENT_REPLAY_MISMATCH", "Telegram 명령 재전송 내용이 달라졌습니다.")

    try:
        # This generic claim provides Telegram delivery replay safety only.
        # Restart-safe recovery authority is claimed separately under
        # notion_schema_recovery/v1 before any remote read is attempted.
        claim = database.claim_schema_gateway_event(
            command_name=parsed.command_name,
            telegram_user_id=identity.user_id,
            chat_id=identity.chat_id,
            thread_id=identity.thread_id,
            message_id=identity.message_id,
            update_id=identity.update_id,
            command_hash=command_hash,
            schema_proposal_id=parsed.schema_proposal_id,
        )
    except (SchemaEventClaimError, SchemaStateError):
        return _denied("EVENT_REPLAY_MISMATCH", "Telegram 명령 재전송 내용이 달라졌습니다.")
    if claim.get("status") == "completed":
        return str(claim.get("safe_result") or "")

    if identity.user_id not in {str(user) for user in config.approval.allowed_telegram_users}:
        result = _denied("UNAUTHORIZED_USER", "허용된 Telegram 사용자가 아닙니다.")
    else:
        try:
            result = _dispatch(parsed, identity, config, database, command_hash)
        except KeyError:
            result = _denied("NOT_FOUND", "스키마 제안 또는 승인 기록을 찾을 수 없습니다.")
        except (SchemaApprovalError, SchemaRecoveryError, HermesSchemaGatewayError):
            result = _denied("SCOPE_OR_STATE", "명령의 사용자·채팅·revision·digest 또는 상태가 일치하지 않습니다.")
        except ApplyLeaseError:
            result = _denied("TARGET_BUSY", "다른 승인 작업이 같은 Notion 대상을 처리 중입니다.")
        except (NotionSchemaSecurityError, SchemaStateError):
            result = _denied("VALIDATION", "승인된 스키마 범위나 저장 상태를 정확히 검증할 수 없습니다.")
        except NotionError:
            result = _safe_failure(
                "READ_FAILED",
                "Notion 읽기 검증에 실패했습니다. Notion은 변경되지 않았습니다.",
            )
        except Exception:
            result = _safe_failure(
                "INTERNAL_ERROR",
                "스키마 명령을 안전하게 완료하지 못했습니다. 원격 변경을 재시도하지 않습니다.",
            )
    try:
        return _complete_result(
            database,
            parsed=parsed,
            identity=identity,
            command_hash=command_hash,
            result=result,
        )
    except (SchemaEventClaimError, SchemaStateError):
        return _denied("EVENT_RESULT", "명령 결과를 재전송 기록에 안전하게 저장하지 못했습니다.")


__all__ = [
    "SCHEMA_APPROVE_COMMAND",
    "SCHEMA_COMMANDS",
    "SCHEMA_PLAN_COMMAND",
    "SCHEMA_RECOVER_COMMAND",
    "SCHEMA_REJECT_COMMAND",
    "SCHEMA_SHOW_COMMAND",
    "HermesSchemaGatewayError",
    "TelegramSchemaEventIdentity",
    "handle_schema_command",
    "is_schema_command",
]
