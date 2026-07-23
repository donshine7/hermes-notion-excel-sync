from __future__ import annotations

import hmac
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from notion_excel_sync.adapters.notion import DEFAULT_NOTION_VERSION
from notion_excel_sync.models import sha256_json
from notion_excel_sync.workflow.notion_writer import NOTION_DEFINITIONS


SCHEMA_PURPOSE = "notion_schema_create/v1"
SCHEMA_TEMPLATE_ID = "government-support-evidence"
SCHEMA_TEMPLATE_VERSION = 1
SCHEMA_LOGICAL_NAME = "정부지원사업 증빙"
# Pure workflow tests need a deterministic valid Notion ID. Production callers
# must supply their configured parent explicitly and must never rely on this fake.
SCHEMA_PARENT_PAGE_ID = "f0000000000000000000000000000001"
SCHEMA_API_VERSION = DEFAULT_NOTION_VERSION
REMOTE_MARKER_PREFIX = "nx-schema-create-v1:"

_NOTION_ID_RE = re.compile(
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)
_REMOTE_MARKER_RE = re.compile(
    rf"{re.escape(REMOTE_MARKER_PREFIX)}[0-9a-f]{{32}}"
)

# This literal contract deliberately duplicates the relevant part of
# NOTION_DEFINITIONS. A later broadening of the ordinary data writer must not
# silently broaden the much more powerful database-creation capability.
FIXED_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("증빙명", "title"),
    ("증빙유형", "select"),
    ("인정 건수", "number"),
    ("산정방식", "rich_text"),
    ("실제 비용 합계", "number"),
    ("인정대상금액", "number"),
    ("정부지원금", "number"),
    ("자부담", "number"),
    ("부가세 처리", "select"),
    ("증빙상태", "select"),
    ("부족액", "number"),
    ("초과액", "number"),
    ("중복증빙", "checkbox"),
    ("산정근거", "rich_text"),
    ("대상 사건", "relation"),
    ("정부지원사업 그룹", "relation"),
    ("관련 실제 비용", "relation"),
    ("근거자료", "relation"),
)

FIXED_RELATIONS: tuple[tuple[str, str], ...] = (
    ("대상 사건", "한국 특허 사건"),
    ("정부지원사업 그룹", "그룹"),
    ("관련 실제 비용", "비용·청구"),
    ("근거자료", "근거자료"),
)

FIXED_SELECT_OPTIONS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType({
    "증빙유형": (
        ("가출원", "default"),
        ("특허출원", "default"),
        ("상표출원", "default"),
        ("디자인출원", "default"),
        ("PCT출원", "default"),
        ("기타", "default"),
    ),
    "부가세 처리": (
        ("별도", "default"),
        ("포함", "default"),
        ("불인정", "default"),
        ("면세", "default"),
        ("확인필요", "default"),
    ),
    "증빙상태": (
        ("증빙가능", "green"),
        ("검토필요", "yellow"),
    ),
})


class NotionSchemaSecurityError(ValueError):
    """Base class for fail-closed schema planning and verification errors."""


class SchemaTemplateError(NotionSchemaSecurityError):
    """The requested schema is outside the fixed creation allowlist."""


class SchemaBindingError(NotionSchemaSecurityError):
    """A live parent, bot, child, or relation binding is not exact."""


class CreatedSchemaVerificationError(NotionSchemaSecurityError):
    """A purportedly created database cannot be proved to match the plan."""


class NotionSchemaSnapshotReader(Protocol):
    """Read-only port required by a future trusted schema workflow.

    The functions in this module consume already captured mappings. Defining the
    port here makes it possible to connect a Notion client without giving these
    pure security checks an HTTP or mutation capability.
    """

    def get_page(self, page_id: str) -> Mapping[str, Any]: ...

    def get_write_bot(self) -> Mapping[str, Any]: ...

    def list_direct_child_databases(
        self, parent_page_id: str
    ) -> Sequence[Mapping[str, Any]]: ...

    def get_database(self, database_id: str) -> Mapping[str, Any]: ...

    def get_data_source(self, data_source_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ParentPageBinding:
    page_id: str


@dataclass(frozen=True, slots=True)
class WriteBotBinding:
    bot_id: str


@dataclass(frozen=True, slots=True)
class RelationTarget:
    property_name: str
    logical_name: str
    data_source_id: str


@dataclass(frozen=True, slots=True)
class RelationLiveBinding:
    logical_name: str
    data_source_id: str
    parent_database_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class DirectChildIdentity:
    database_id: str
    title: str
    marker: str
    archived: bool
    in_trash: bool


@dataclass(frozen=True, slots=True)
class SchemaLiveBinding:
    parent: ParentPageBinding
    write_bot: WriteBotBinding
    relations: tuple[RelationLiveBinding, ...]
    direct_child_conflicts: tuple[DirectChildIdentity, ...] = ()

    @property
    def digest(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class SchemaCreatePlan:
    template_id: str
    template_version: int
    logical_name: str
    purpose: str
    api_version: str
    parent_page_id: str
    write_bot_id: str
    remote_marker: str
    relation_targets: tuple[RelationTarget, ...]
    approved_live_binding: SchemaLiveBinding

    def __post_init__(self) -> None:
        _assert_template_contract()
        if self.template_id != SCHEMA_TEMPLATE_ID:
            raise SchemaTemplateError("Schema template is not allowlisted")
        if self.template_version != SCHEMA_TEMPLATE_VERSION:
            raise SchemaTemplateError("Schema template version is not allowlisted")
        if self.logical_name != SCHEMA_LOGICAL_NAME:
            raise SchemaTemplateError("Schema title is not allowlisted")
        if self.purpose != SCHEMA_PURPOSE:
            raise SchemaTemplateError("Schema approval purpose is not allowlisted")
        if self.api_version != SCHEMA_API_VERSION:
            raise SchemaTemplateError("Notion API version is not allowlisted")
        if normalize_notion_id(self.parent_page_id) != self.parent_page_id:
            raise SchemaBindingError("Schema parent page ID is not canonical")
        if normalize_notion_id(self.write_bot_id) != self.write_bot_id:
            raise SchemaBindingError("Write bot ID is not canonical")
        if _REMOTE_MARKER_RE.fullmatch(self.remote_marker) is None:
            raise SchemaTemplateError("Remote schema marker is invalid")
        expected_relation_names = FIXED_RELATIONS
        actual_relation_names = tuple(
            (item.property_name, item.logical_name) for item in self.relation_targets
        )
        if actual_relation_names != expected_relation_names:
            raise SchemaTemplateError("Schema relation set is not allowlisted")
        if any(
            normalize_notion_id(item.data_source_id) != item.data_source_id
            for item in self.relation_targets
        ):
            raise SchemaBindingError("Relation data-source ID is not canonical")
        if self.approved_live_binding.direct_child_conflicts:
            raise SchemaBindingError("Target database already exists under the parent")
        if self.approved_live_binding.parent.page_id != self.parent_page_id:
            raise SchemaBindingError("Approved parent binding differs from the plan")
        if self.approved_live_binding.write_bot.bot_id != self.write_bot_id:
            raise SchemaBindingError("Approved write-bot binding differs from the plan")
        expected_live_relations = tuple(
            (item.logical_name, item.data_source_id) for item in self.relation_targets
        )
        actual_live_relations = tuple(
            (item.logical_name, item.data_source_id)
            for item in self.approved_live_binding.relations
        )
        if actual_live_relations != expected_live_relations:
            raise SchemaBindingError("Approved relation binding differs from the plan")

    @property
    def create_body(self) -> dict[str, Any]:
        """Return a fresh, exact request body; callers cannot inject fields."""

        _assert_template_contract()
        return {
            "parent": {
                "type": "page_id",
                "page_id": self.parent_page_id,
            },
            "title": [_notion_text(SCHEMA_LOGICAL_NAME)],
            "description": [_notion_text(self.remote_marker)],
            "is_inline": False,
            "initial_data_source": {
                "properties": _create_properties(self.relation_targets),
            },
        }

    def digest_payload(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "logical_name": self.logical_name,
            "api_version": self.api_version,
            "parent_page_id": self.parent_page_id,
            "write_bot_id": self.write_bot_id,
            "remote_marker": self.remote_marker,
            "relation_targets": [asdict(item) for item in self.relation_targets],
            "approved_live_binding": asdict(self.approved_live_binding),
            "create_body": self.create_body,
        }

    @property
    def digest(self) -> str:
        return sha256_json(self.digest_payload())


@dataclass(frozen=True, slots=True)
class CreatedSchemaBinding:
    database_id: str
    data_source_id: str
    parent_page_id: str
    marker: str
    database_creator_id: str | None
    data_source_creator_id: str
    database_creator_evidence: str
    database_created_time: str
    data_source_created_time: str
    data_source_time_precision: str
    schema_digest: str
    relation_targets: tuple[RelationTarget, ...]

    @property
    def digest(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class RemoteSchemaCandidate:
    database: Mapping[str, Any]
    data_source: Mapping[str, Any]


class ReconciliationDisposition(StrEnum):
    SAFE_TO_CREATE = "safe_to_create"
    ALREADY_CREATED = "already_created"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    disposition: ReconciliationDisposition
    reason_code: str
    binding: CreatedSchemaBinding | None = None


def normalize_notion_id(value: object) -> str:
    if not isinstance(value, str) or _NOTION_ID_RE.fullmatch(value.strip()) is None:
        raise SchemaBindingError("Notion ID is invalid")
    return value.strip().replace("-", "").lower()


def generate_remote_marker() -> str:
    return f"{REMOTE_MARKER_PREFIX}{secrets.token_hex(16)}"


def _normalized_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SchemaBindingError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value)
    if not allow_empty and not normalized:
        raise SchemaBindingError(f"{label} must not be empty")
    return normalized


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaBindingError(f"{label} must be an object")
    return value


def _sequence(value: object, *, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SchemaBindingError(f"{label} must be an array")
    return value


def _active_lifecycle(value: Mapping[str, Any], *, label: str) -> None:
    """Validate the lifecycle fields guaranteed by Notion-Version 2026-03-11."""

    if value.get("in_trash") is not False:
        raise SchemaBindingError(f"{label} must have in_trash=false")
    if "archived" in value and value["archived"] is not False:
        raise SchemaBindingError(f"{label} must have archived=false when present")


def _object_id(value: Mapping[str, Any], object_type: str, *, label: str) -> str:
    if value.get("object") != object_type:
        raise SchemaBindingError(f"{label} has an unexpected object type")
    return normalize_notion_id(value.get("id"))


def _rich_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    rows = _sequence(value, label=label)
    if not rows:
        if allow_empty:
            return ""
        raise SchemaBindingError(f"{label} must contain exactly one text item")
    if len(rows) != 1:
        raise SchemaBindingError(f"{label} must contain exactly one text item")
    row = _mapping(rows[0], label=f"{label} item")
    if row.get("type") not in {None, "text"}:
        raise SchemaBindingError(f"{label} must use plain text")
    nested = row.get("text")
    nested_content = None
    if nested is not None:
        nested_content = _mapping(nested, label=f"{label} text").get("content")
    plain_text = row.get("plain_text")
    if plain_text is not None and nested_content is not None:
        if _normalized_text(plain_text, label=label, allow_empty=allow_empty) != (
            _normalized_text(nested_content, label=label, allow_empty=allow_empty)
        ):
            raise SchemaBindingError(f"{label} text representations differ")
    content = nested_content if nested_content is not None else plain_text
    return _normalized_text(content, label=label, allow_empty=allow_empty)


def _optional_rich_text(value: object, *, label: str) -> str:
    if value in (None, []):
        return ""
    return _rich_text(value, label=label)


def _notion_text(content: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": content},
    }


def _assert_template_contract() -> None:
    definition = NOTION_DEFINITIONS.get(SCHEMA_LOGICAL_NAME)
    if definition is None:
        raise SchemaTemplateError("Allowlisted Notion definition is missing")
    if definition.title_property != "증빙명":
        raise SchemaTemplateError("Allowlisted title property changed")
    if len(definition.properties) != len(FIXED_PROPERTIES):
        raise SchemaTemplateError("Allowlisted property count changed")
    if dict(definition.properties) != dict(FIXED_PROPERTIES):
        raise SchemaTemplateError("Allowlisted property types changed")
    if dict(definition.relations) != dict(FIXED_RELATIONS):
        raise SchemaTemplateError("Allowlisted relation definitions changed")


def _normalize_relation_ids(
    relation_data_source_ids: Mapping[str, str],
) -> tuple[RelationTarget, ...]:
    expected_logical_names = {logical_name for _, logical_name in FIXED_RELATIONS}
    if set(relation_data_source_ids) != expected_logical_names:
        raise SchemaTemplateError("Relation target set must match the allowlisted template")
    return tuple(
        RelationTarget(
            property_name=property_name,
            logical_name=logical_name,
            data_source_id=normalize_notion_id(relation_data_source_ids[logical_name]),
        )
        for property_name, logical_name in FIXED_RELATIONS
    )


def _parent_binding(
    parent_page: Mapping[str, Any],
    expected_parent_page_id: str,
) -> ParentPageBinding:
    page_id = _object_id(parent_page, "page", label="schema parent")
    if page_id != normalize_notion_id(expected_parent_page_id):
        raise SchemaBindingError("Live schema parent differs from the expected parent")
    _active_lifecycle(parent_page, label="schema parent")
    return ParentPageBinding(page_id=page_id)


def _write_bot_binding(
    write_bot: Mapping[str, Any], expected_write_bot_id: str | None
) -> WriteBotBinding:
    bot_id = _object_id(write_bot, "user", label="write bot")
    if write_bot.get("type") != "bot" or not isinstance(write_bot.get("bot"), Mapping):
        raise SchemaBindingError("Notion write identity is not a bot")
    if expected_write_bot_id is not None:
        expected = normalize_notion_id(expected_write_bot_id)
        if bot_id != expected:
            raise SchemaBindingError("Live write bot differs from configuration")
    return WriteBotBinding(bot_id=bot_id)


def _data_source_display_name(data_source: Mapping[str, Any]) -> str:
    if isinstance(data_source.get("name"), str):
        return _normalized_text(data_source["name"], label="data-source name")
    return _rich_text(data_source.get("title"), label="data-source title")


def _relation_live_binding(
    logical_name: str,
    expected_data_source_id: str,
    data_source: Mapping[str, Any],
) -> RelationLiveBinding:
    actual_id = _object_id(data_source, "data_source", label="relation data source")
    if actual_id != expected_data_source_id:
        raise SchemaBindingError("Relation data source differs from configuration")
    _active_lifecycle(data_source, label="relation data source")
    parent = _mapping(data_source.get("parent"), label="relation data-source parent")
    if parent.get("type") != "database_id":
        raise SchemaBindingError("Relation data source has an unexpected parent type")
    parent_database_id = normalize_notion_id(parent.get("database_id"))
    display_name = _data_source_display_name(data_source)
    if display_name != logical_name:
        raise SchemaBindingError(
            "Relation data-source display name differs from its configured logical name"
        )
    return RelationLiveBinding(
        logical_name=logical_name,
        data_source_id=actual_id,
        parent_database_id=parent_database_id,
        display_name=display_name,
    )


def _direct_child_identity(
    child: Mapping[str, Any], parent_page_id: str
) -> DirectChildIdentity:
    database_id = _object_id(child, "database", label="direct child database")
    parent = _mapping(child.get("parent"), label="direct child parent")
    if parent.get("type") != "page_id":
        raise SchemaBindingError("Direct child has an unexpected parent type")
    if normalize_notion_id(parent.get("page_id")) != parent_page_id:
        raise SchemaBindingError("Direct child enumeration escaped the approved parent")
    _active_lifecycle(child, label="direct child database")
    return DirectChildIdentity(
        database_id=database_id,
        title=_rich_text(child.get("title"), label="direct child title"),
        marker=_optional_rich_text(
            child.get("description"), label="direct child description"
        ),
        archived=False,
        in_trash=False,
    )


def capture_schema_live_binding(
    *,
    parent_page: Mapping[str, Any],
    write_bot: Mapping[str, Any],
    direct_child_databases: Sequence[Mapping[str, Any]],
    relation_data_source_ids: Mapping[str, str],
    relation_data_sources: Mapping[str, Mapping[str, Any]],
    expected_write_bot_id: str | None = None,
    expected_parent_page_id: str = SCHEMA_PARENT_PAGE_ID,
) -> SchemaLiveBinding:
    """Canonicalize the exact read-only precondition for one schema proposal."""

    _assert_template_contract()
    parent = _parent_binding(parent_page, expected_parent_page_id)
    bot = _write_bot_binding(write_bot, expected_write_bot_id)
    relation_targets = _normalize_relation_ids(relation_data_source_ids)
    expected_names = {item.logical_name for item in relation_targets}
    if set(relation_data_sources) != expected_names:
        raise SchemaBindingError("Live relation data-source set is not exact")
    relations = tuple(
        _relation_live_binding(
            item.logical_name,
            item.data_source_id,
            relation_data_sources[item.logical_name],
        )
        for item in relation_targets
    )
    conflicts: list[DirectChildIdentity] = []
    for raw_child in direct_child_databases:
        child = _direct_child_identity(raw_child, parent.page_id)
        if child.title == SCHEMA_LOGICAL_NAME or child.marker.startswith(
            REMOTE_MARKER_PREFIX
        ):
            conflicts.append(child)
    conflicts.sort(key=lambda item: item.database_id)
    return SchemaLiveBinding(
        parent=parent,
        write_bot=bot,
        relations=relations,
        direct_child_conflicts=tuple(conflicts),
    )


def assert_schema_live_binding_matches(
    approved: SchemaLiveBinding,
    current: SchemaLiveBinding,
) -> None:
    if not hmac.compare_digest(approved.digest, current.digest):
        raise SchemaBindingError("Live Notion schema binding changed after approval")


def build_schema_create_plan(
    template_id: str,
    *,
    parent_page: Mapping[str, Any],
    write_bot: Mapping[str, Any],
    direct_child_databases: Sequence[Mapping[str, Any]],
    relation_data_source_ids: Mapping[str, str],
    relation_data_sources: Mapping[str, Mapping[str, Any]],
    expected_write_bot_id: str | None = None,
    expected_parent_page_id: str = SCHEMA_PARENT_PAGE_ID,
    api_version: str = SCHEMA_API_VERSION,
) -> SchemaCreatePlan:
    """Build the sole allowlisted schema plan without accepting a body or title."""

    if template_id != SCHEMA_TEMPLATE_ID:
        raise SchemaTemplateError("Schema template is not allowlisted")
    if api_version != SCHEMA_API_VERSION:
        raise SchemaTemplateError("Notion API version is not allowlisted")
    live_binding = capture_schema_live_binding(
        parent_page=parent_page,
        write_bot=write_bot,
        direct_child_databases=direct_child_databases,
        relation_data_source_ids=relation_data_source_ids,
        relation_data_sources=relation_data_sources,
        expected_write_bot_id=expected_write_bot_id,
        expected_parent_page_id=expected_parent_page_id,
    )
    expected_parent = normalize_notion_id(expected_parent_page_id)
    if live_binding.parent.page_id != expected_parent:
        raise SchemaBindingError("Live schema parent differs from the expected parent")
    relation_targets = _normalize_relation_ids(relation_data_source_ids)
    return SchemaCreatePlan(
        template_id=SCHEMA_TEMPLATE_ID,
        template_version=SCHEMA_TEMPLATE_VERSION,
        logical_name=SCHEMA_LOGICAL_NAME,
        purpose=SCHEMA_PURPOSE,
        api_version=SCHEMA_API_VERSION,
        parent_page_id=expected_parent,
        write_bot_id=live_binding.write_bot.bot_id,
        remote_marker=generate_remote_marker(),
        relation_targets=relation_targets,
        approved_live_binding=live_binding,
    )


def _create_properties(
    relation_targets: tuple[RelationTarget, ...],
) -> dict[str, Any]:
    relation_ids = {item.property_name: item.data_source_id for item in relation_targets}
    result: dict[str, Any] = {}
    for property_name, property_type in FIXED_PROPERTIES:
        if property_type == "select":
            result[property_name] = {
                "select": {
                    "options": [
                        {"name": name, "color": color}
                        for name, color in FIXED_SELECT_OPTIONS[property_name]
                    ]
                }
            }
        elif property_type == "number":
            result[property_name] = {
                "number": {
                    "format": "number" if property_name == "인정 건수" else "won"
                }
            }
        elif property_type == "relation":
            result[property_name] = {
                "relation": {
                    "data_source_id": relation_ids[property_name],
                    "single_property": {},
                }
            }
        else:
            result[property_name] = {property_type: {}}
    return result


def _canonical_expected_remote_schema(plan: SchemaCreatePlan) -> dict[str, Any]:
    relation_ids = {
        item.property_name: item.data_source_id for item in plan.relation_targets
    }
    expected: dict[str, Any] = {}
    for property_name, property_type in FIXED_PROPERTIES:
        row: dict[str, Any] = {"type": property_type}
        if property_type == "select":
            row["options"] = sorted(
                [
                    {"name": name, "color": color}
                    for name, color in FIXED_SELECT_OPTIONS[property_name]
                ],
                key=lambda item: (item["name"], item["color"]),
            )
        elif property_type == "number":
            row["format"] = "number" if property_name == "인정 건수" else "won"
        elif property_type == "relation":
            row["target_data_source_id"] = relation_ids[property_name]
            row["mode"] = "single_property"
        expected[property_name] = row
    return expected


def canonicalize_created_data_source_schema(
    plan: SchemaCreatePlan,
    data_source: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize only security-relevant schema fields and reject every extra property."""

    properties = _mapping(data_source.get("properties"), label="created properties")
    expected_names = {name for name, _ in FIXED_PROPERTIES}
    if set(properties) != expected_names:
        raise CreatedSchemaVerificationError(
            "Created data source has missing or extra properties"
        )
    canonical: dict[str, Any] = {}
    for property_name, expected_type in FIXED_PROPERTIES:
        raw = _mapping(properties[property_name], label=f"property {property_name}")
        if raw.get("type") != expected_type:
            raise CreatedSchemaVerificationError("Created property type differs")
        config = _mapping(raw.get(expected_type), label=f"property {property_name} config")
        row: dict[str, Any] = {"type": expected_type}
        if expected_type == "select":
            options = _sequence(config.get("options", []), label="select options")
            normalized_options: list[dict[str, str]] = []
            for option in options:
                option_map = _mapping(option, label="select option")
                normalized_options.append(
                    {
                        "name": _normalized_text(
                            option_map.get("name"), label="select option name"
                        ),
                        "color": _normalized_text(
                            option_map.get("color"), label="select option color"
                        ),
                    }
                )
            row["options"] = sorted(
                normalized_options,
                key=lambda item: (item["name"], item["color"]),
            )
        elif expected_type == "number":
            row["format"] = _normalized_text(
                config.get("format"), label="number format"
            )
        elif expected_type == "relation":
            if "data_source_id" not in config:
                raise CreatedSchemaVerificationError(
                    "Created relation is not bound to a data-source ID"
                )
            row["target_data_source_id"] = normalize_notion_id(
                config.get("data_source_id")
            )
            relation_type = config.get("type")
            has_single = isinstance(config.get("single_property"), Mapping)
            has_dual = "dual_property" in config
            if relation_type not in {None, "single_property"} or not has_single or has_dual:
                raise CreatedSchemaVerificationError(
                    "Created relation is not one-way single-property"
                )
            row["mode"] = "single_property"
        canonical[property_name] = row
    expected = _canonical_expected_remote_schema(plan)
    if sha256_json(canonical) != sha256_json(expected):
        raise CreatedSchemaVerificationError("Created data-source schema differs")
    return canonical


def _normalized_timestamp(value: object, *, label: str) -> tuple[datetime, str]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CreatedSchemaVerificationError(f"{label} is invalid") from exc
    else:
        raise CreatedSchemaVerificationError(f"{label} is missing")
    if parsed.tzinfo is None:
        raise CreatedSchemaVerificationError(f"{label} must be timezone-aware")
    parsed_utc = parsed.astimezone(UTC)
    canonical = parsed_utc.isoformat().replace("+00:00", "Z")
    return parsed_utc, canonical


def _created_by_id(value: Mapping[str, Any], *, label: str) -> str:
    creator = _mapping(value.get("created_by"), label=f"{label} creator")
    if creator.get("object") != "user":
        raise CreatedSchemaVerificationError(f"{label} creator is not a Notion user")
    return normalize_notion_id(creator.get("id"))


def verify_created_schema(
    plan: SchemaCreatePlan,
    database: Mapping[str, Any],
    data_source: Mapping[str, Any],
    *,
    attempt_started_at: datetime,
    observed_at: datetime,
    expected_binding: CreatedSchemaBinding | None = None,
    journal_remote_ids: tuple[str, str] | None = None,
) -> CreatedSchemaBinding:
    """Verify an exact database/data-source pair and capture its immutable identity."""

    start, _ = _normalized_timestamp(attempt_started_at, label="attempt start")
    observed, _ = _normalized_timestamp(observed_at, label="observation time")
    if observed < start:
        raise CreatedSchemaVerificationError("Creation attempt window is invalid")

    database_id = _object_id(database, "database", label="created database")
    data_source_id = _object_id(data_source, "data_source", label="created data source")
    journal_ids_match = False
    if journal_remote_ids is not None:
        if len(journal_remote_ids) != 2 or not all(journal_remote_ids):
            raise CreatedSchemaVerificationError(
                "Journaled remote identity pair is incomplete"
            )
        journal_database_id = normalize_notion_id(journal_remote_ids[0])
        journal_data_source_id = normalize_notion_id(journal_remote_ids[1])
        if (
            journal_database_id != database_id
            or journal_data_source_id != data_source_id
        ):
            raise CreatedSchemaVerificationError(
                "Created object identity differs from the journal"
            )
        journal_ids_match = True
    for remote, label in (
        (database, "created database"),
        (data_source, "created data source"),
    ):
        try:
            _active_lifecycle(remote, label=label)
        except SchemaBindingError as exc:
            raise CreatedSchemaVerificationError(str(exc)) from exc

    database_parent = _mapping(database.get("parent"), label="created database parent")
    if database_parent.get("type") != "page_id" or normalize_notion_id(
        database_parent.get("page_id")
    ) != plan.parent_page_id:
        raise CreatedSchemaVerificationError("Created database parent differs")
    data_source_parent = _mapping(
        data_source.get("parent"), label="created data-source parent"
    )
    if data_source_parent.get("type") != "database_id" or normalize_notion_id(
        data_source_parent.get("database_id")
    ) != database_id:
        raise CreatedSchemaVerificationError("Created data-source parent differs")

    if _rich_text(database.get("title"), label="created database title") != (
        SCHEMA_LOGICAL_NAME
    ):
        raise CreatedSchemaVerificationError("Created database title differs")
    if database.get("is_inline") is not False:
        raise CreatedSchemaVerificationError("Created database must not be inline")
    if _rich_text(
        database.get("description"), label="created database description"
    ) != plan.remote_marker:
        raise CreatedSchemaVerificationError("Created database marker differs")

    child_sources = _sequence(
        database.get("data_sources"), label="created database data sources"
    )
    if len(child_sources) != 1:
        raise CreatedSchemaVerificationError(
            "Created database must contain exactly one data source"
        )
    child_source = _mapping(child_sources[0], label="created database data source")
    if normalize_notion_id(child_source.get("id")) != data_source_id:
        raise CreatedSchemaVerificationError("Created data-source identity differs")
    if _normalized_text(
        child_source.get("name"), label="created database data-source name"
    ) != SCHEMA_LOGICAL_NAME:
        raise CreatedSchemaVerificationError("Created database data-source name differs")
    if _data_source_display_name(data_source) != SCHEMA_LOGICAL_NAME:
        raise CreatedSchemaVerificationError("Created data-source name differs")

    data_source_creator_id = _created_by_id(data_source, label="created data source")
    if data_source_creator_id != plan.write_bot_id:
        raise CreatedSchemaVerificationError("Created object has an unexpected creator")
    if "created_by" in database:
        database_creator_id: str | None = _created_by_id(
            database,
            label="created database",
        )
        if database_creator_id != plan.write_bot_id:
            raise CreatedSchemaVerificationError(
                "Created object has an unexpected creator"
            )
        database_creator_evidence = "direct"
    else:
        if not journal_ids_match:
            raise CreatedSchemaVerificationError(
                "Created database creator is unavailable without journal identity"
            )
        database_creator_id = None
        database_creator_evidence = "data_source_and_journal"

    database_time, database_time_text = _normalized_timestamp(
        database.get("created_time"), label="database created_time"
    )
    data_source_time, data_source_time_text = _normalized_timestamp(
        data_source.get("created_time"), label="data-source created_time"
    )
    if not (start <= database_time <= observed):
        raise CreatedSchemaVerificationError("Created object is outside the attempt window")
    if start <= data_source_time <= observed:
        data_source_time_precision = "exact"
    else:
        minute_floor_matches = (
            journal_ids_match
            and data_source_time.second == 0
            and data_source_time.microsecond == 0
            and data_source_time
            == database_time.replace(second=0, microsecond=0)
            and data_source_time <= observed
            and data_source_time + timedelta(minutes=1) > start
        )
        if not minute_floor_matches:
            raise CreatedSchemaVerificationError(
                "Created object is outside the attempt window"
            )
        data_source_time_precision = "minute_floor"

    canonical_schema = canonicalize_created_data_source_schema(plan, data_source)
    binding = CreatedSchemaBinding(
        database_id=database_id,
        data_source_id=data_source_id,
        parent_page_id=plan.parent_page_id,
        marker=plan.remote_marker,
        database_creator_id=database_creator_id,
        data_source_creator_id=data_source_creator_id,
        database_creator_evidence=database_creator_evidence,
        database_created_time=database_time_text,
        data_source_created_time=data_source_time_text,
        data_source_time_precision=data_source_time_precision,
        schema_digest=sha256_json(canonical_schema),
        relation_targets=plan.relation_targets,
    )
    if expected_binding is not None and not hmac.compare_digest(
        binding.digest, expected_binding.digest
    ):
        raise CreatedSchemaVerificationError(
            "Created schema differs from the journaled remote identity"
        )
    return binding


def _relevant_candidate_identity(
    candidate: RemoteSchemaCandidate,
    parent_page_id: str,
) -> DirectChildIdentity:
    try:
        return _direct_child_identity(candidate.database, parent_page_id)
    except SchemaBindingError as exc:
        raise CreatedSchemaVerificationError(str(exc)) from exc


def decide_schema_reconciliation(
    plan: SchemaCreatePlan,
    *,
    current_live_binding: SchemaLiveBinding,
    candidates: Sequence[RemoteSchemaCandidate],
    create_was_attempted: bool,
    attempt_started_at: datetime,
    observed_at: datetime,
    expected_binding: CreatedSchemaBinding | None = None,
    journal_remote_ids: tuple[str, str] | None = None,
) -> ReconciliationDecision:
    """Return a non-mutating, fail-closed decision after a create interruption."""

    approved_base = replace(plan.approved_live_binding, direct_child_conflicts=())
    current_base = replace(current_live_binding, direct_child_conflicts=())
    try:
        assert_schema_live_binding_matches(approved_base, current_base)
    except SchemaBindingError:
        return ReconciliationDecision(
            ReconciliationDisposition.CONFLICT,
            "live_binding_drift",
        )

    relevant: list[tuple[DirectChildIdentity, RemoteSchemaCandidate]] = []
    try:
        for candidate in candidates:
            identity = _relevant_candidate_identity(candidate, plan.parent_page_id)
            if identity.title == SCHEMA_LOGICAL_NAME or identity.marker.startswith(
                REMOTE_MARKER_PREFIX
            ):
                relevant.append((identity, candidate))
    except CreatedSchemaVerificationError:
        return ReconciliationDecision(
            ReconciliationDisposition.CONFLICT,
            "malformed_direct_child",
        )

    live_conflict_ids = {
        item.database_id for item in current_live_binding.direct_child_conflicts
    }
    candidate_ids = {item.database_id for item, _ in relevant}
    if live_conflict_ids != candidate_ids:
        return ReconciliationDecision(
            ReconciliationDisposition.CONFLICT,
            "direct_child_snapshot_mismatch",
        )
    if len(relevant) > 1:
        return ReconciliationDecision(
            ReconciliationDisposition.CONFLICT,
            "multiple_relevant_children",
        )
    if not relevant:
        if create_was_attempted:
            return ReconciliationDecision(
                ReconciliationDisposition.UNKNOWN,
                "attempted_create_has_no_provable_remote",
            )
        return ReconciliationDecision(
            ReconciliationDisposition.SAFE_TO_CREATE,
            "approved_absence_still_holds",
        )

    identity, candidate = relevant[0]
    if identity.title != SCHEMA_LOGICAL_NAME or identity.marker != plan.remote_marker:
        return ReconciliationDecision(
            ReconciliationDisposition.CONFLICT,
            "foreign_or_mismatched_child",
        )
    try:
        binding = verify_created_schema(
            plan,
            candidate.database,
            candidate.data_source,
            attempt_started_at=attempt_started_at,
            observed_at=observed_at,
            expected_binding=expected_binding,
            journal_remote_ids=journal_remote_ids,
        )
    except NotionSchemaSecurityError:
        return ReconciliationDecision(
            ReconciliationDisposition.CONFLICT,
            "candidate_verification_failed",
        )
    return ReconciliationDecision(
        ReconciliationDisposition.ALREADY_CREATED,
        "exact_remote_marker_and_schema",
        binding,
    )


__all__ = [
    "CreatedSchemaBinding",
    "CreatedSchemaVerificationError",
    "DirectChildIdentity",
    "FIXED_PROPERTIES",
    "FIXED_RELATIONS",
    "FIXED_SELECT_OPTIONS",
    "NotionSchemaSecurityError",
    "NotionSchemaSnapshotReader",
    "ParentPageBinding",
    "ReconciliationDecision",
    "ReconciliationDisposition",
    "RelationLiveBinding",
    "RelationTarget",
    "RemoteSchemaCandidate",
    "SCHEMA_API_VERSION",
    "SCHEMA_LOGICAL_NAME",
    "SCHEMA_PARENT_PAGE_ID",
    "SCHEMA_PURPOSE",
    "SCHEMA_TEMPLATE_ID",
    "SCHEMA_TEMPLATE_VERSION",
    "SchemaBindingError",
    "SchemaCreatePlan",
    "SchemaLiveBinding",
    "SchemaTemplateError",
    "WriteBotBinding",
    "assert_schema_live_binding_matches",
    "build_schema_create_plan",
    "canonicalize_created_data_source_schema",
    "capture_schema_live_binding",
    "decide_schema_reconciliation",
    "generate_remote_marker",
    "normalize_notion_id",
    "verify_created_schema",
]
