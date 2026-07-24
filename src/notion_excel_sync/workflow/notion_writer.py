from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from notion_excel_sync.adapters.notion import (
    NotionGateway,
    NotionMatch,
    NotionPrecondition,
    NotionPropertyMutation,
)
from notion_excel_sync.models import (
    ApprovalReceipt,
    JsonValue,
    ProposalAction,
    ProposalOperation,
    ProposalPurpose,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    WritePreconditionState,
    canonical_json,
    sha256_json,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.approval import ApprovalService


class NotionSchemaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DatabaseDefinition:
    title_property: str
    properties: Mapping[str, str]
    relations: Mapping[str, str] = field(default_factory=dict)


NOTION_DEFINITIONS: dict[str, DatabaseDefinition] = {
    "한국 특허 사건": DatabaseDefinition(
        "당소 사건번호",
        {
            "당소 사건번호": "title",
            "사건종류": "select",
            "수임상품": "select",
            "의뢰일": "date",
            "착수일": "date",
            "완료일": "date",
            "내부기일": "date",
            "법정기일": "date",
            "현재상태": "select",
            "내부상태": "select",
            "공식절차": "select",
            "발명자료/거절이유": "select",
            "우선심사사유": "select",
            "그룹": "relation",
            "의뢰인": "relation",
            "출원인": "relation",
            "발명자": "relation",
            "비용부담자": "relation",
            "관련사건": "relation",
            "모사건": "relation",
            "비고": "rich_text",
            "OneDrive URL": "url",
            "로컬ID": "rich_text",
            "데이터확인필요": "checkbox",
        },
        {
            "그룹": "그룹",
            "의뢰인": "당사자",
            "출원인": "당사자",
            "발명자": "당사자",
            "비용부담자": "당사자",
            "관련사건": "한국 특허 사건",
            "모사건": "한국 특허 사건",
        },
    ),
    "그룹": DatabaseDefinition(
        "그룹명",
        {
            "그룹명": "title",
            "그룹유형": "select",
            "그룹코드": "rich_text",
            "협약번호": "rich_text",
            "관리기관": "rich_text",
            "사업연도": "number",
            "지원한도": "number",
            "자부담액": "number",
            "상태": "select",
            "시작일": "date",
            "종료일": "date",
            "비고": "rich_text",
        },
    ),
    "당사자": DatabaseDefinition(
        "당사자명",
        {
            "당사자명": "title",
            "구분": "select",
            "대표자": "rich_text",
            "담당자명": "rich_text",
            "사업자번호": "rich_text",
            "특허고객번호": "rich_text",
            "주소": "rich_text",
            "이메일": "email",
            "전화번호": "phone_number",
            "비고": "rich_text",
            "민감정보": "checkbox",
            "OneDrive URL": "url",
        },
    ),
    "업무·절차": DatabaseDefinition(
        "업무명",
        {
            "업무명": "title",
            "업무종류": "select",
            "절차대분류": "select",
            "공식상태": "select",
            "내부상태": "select",
            "접수일": "date",
            "내부기일": "date",
            "법정기일": "date",
            "완료일": "date",
            "레거시시트": "rich_text",
            "레거시행": "number",
            "원본비고": "rich_text",
            "사건": "relation",
            "그룹": "relation",
            "검토필요": "checkbox",
            "OneDrive URL": "url",
        },
        {"사건": "한국 특허 사건", "그룹": "그룹"},
    ),
    "기일": DatabaseDefinition(
        "기일명",
        {
            "기일명": "title",
            "기일유형": "select",
            "구분": "select",
            "기일": "date",
            "완료일": "date",
            "산정근거": "rich_text",
            "상태": "select",
            "알림메모": "rich_text",
            "사건": "relation",
            "업무": "relation",
            "그룹": "relation",
            "검토필요": "checkbox",
        },
        {"사건": "한국 특허 사건", "업무": "업무·절차", "그룹": "그룹"},
    ),
    "비용·청구": DatabaseDefinition(
        "항목명",
        {
            "항목명": "title",
            "기록유형": "select",
            "계산방식": "select",
            "청구조건": "select",
            "공급가": "number",
            "부가세": "number",
            "관납료예상": "number",
            "관납료확정": "number",
            "약정총액": "number",
            "청구액": "number",
            "입금액": "number",
            "미납액": "number",
            "할인액": "number",
            "지원금": "number",
            "자부담": "number",
            "선수금차감액": "number",
            "선수금보유액": "number",
            "차감후잔액": "number",
            "청구일": "date",
            "입금일": "date",
            "입금기한": "date",
            "차감일": "date",
            "입금상태": "select",
            "근거메모": "rich_text",
            "사건": "relation",
            "그룹": "relation",
            "비용부담자": "relation",
            "OneDrive URL": "url",
        },
        {"사건": "한국 특허 사건", "그룹": "그룹", "비용부담자": "당사자"},
    ),
    "정부지원사업 증빙": DatabaseDefinition(
        "증빙명",
        {
            "증빙명": "title",
            "증빙유형": "select",
            "인정 건수": "number",
            "산정방식": "rich_text",
            "실제 비용 합계": "number",
            "인정대상금액": "number",
            "정부지원금": "number",
            "자부담": "number",
            "부가세 처리": "select",
            "증빙상태": "select",
            "부족액": "number",
            "초과액": "number",
            "중복증빙": "checkbox",
            "산정근거": "rich_text",
            "대상 사건": "relation",
            "정부지원사업 그룹": "relation",
            "관련 실제 비용": "relation",
            "근거자료": "relation",
        },
        {
            "대상 사건": "한국 특허 사건",
            "정부지원사업 그룹": "그룹",
            "관련 실제 비용": "비용·청구",
            "근거자료": "근거자료",
        },
    ),
    "등록결정 후속관리": DatabaseDefinition(
        "후속관리명",
        {
            "후속관리명": "title",
            "등록마감일": "date",
            "다음연락일": "date",
            "최초연락일": "date",
            "후속상태": "select",
            "분할결정": "select",
            "분할제안건수": "number",
            "분할확정건수": "number",
            "제안금액": "number",
            "미납금액": "number",
            "입금상태": "select",
            "최근연락결과": "select",
            "기획이전안내": "select",
            "메모": "rich_text",
            "사건": "relation",
            "그룹": "relation",
            "검토필요": "checkbox",
        },
        {"사건": "한국 특허 사건", "그룹": "그룹"},
    ),
    "연락이력": DatabaseDefinition(
        "연락제목",
        {
            "연락제목": "title",
            "연락일시": "date",
            "채널": "select",
            "결과": "select",
            "다음연락일": "date",
            "상대방명": "rich_text",
            "메모": "rich_text",
            "근거URL": "url",
            "사건": "relation",
            "상대방": "relation",
            "등록결정": "relation",
        },
        {
            "사건": "한국 특허 사건",
            "상대방": "당사자",
            "등록결정": "등록결정 후속관리",
        },
    ),
    "근거자료": DatabaseDefinition(
        "근거명",
        {
            "근거명": "title",
            "OneDrive URL": "url",
            "driveItem ID": "rich_text",
            "버전ID": "rich_text",
            "SHA256": "rich_text",
            "원본유형": "select",
            "발생일시": "date",
            "수집일시": "date",
            "요약": "rich_text",
            "LLM신뢰도": "number",
            "보안등급": "select",
            "파싱상태": "select",
            "사람확인": "checkbox",
            "사건": "relation",
            "업무": "relation",
            "그룹": "relation",
        },
        {"사건": "한국 특허 사건", "업무": "업무·절차", "그룹": "그룹"},
    ),
    "검토함": DatabaseDefinition(
        "검토항목",
        {
            "검토항목": "title",
            "검토유형": "select",
            "상태": "select",
            "현재값": "rich_text",
            "제안값": "rich_text",
            "근거": "rich_text",
            "신뢰도": "number",
            "메모": "rich_text",
            "처리일": "date",
            "사건": "relation",
            "업무": "relation",
            "비용·청구": "relation",
            "그룹": "relation",
            "근거자료": "relation",
        },
        {
            "사건": "한국 특허 사건",
            "업무": "업무·절차",
            "비용·청구": "비용·청구",
            "그룹": "그룹",
            "근거자료": "근거자료",
        },
    ),
    "변경이력": DatabaseDefinition(
        "변경명",
        {
            "변경명": "title",
            "변경유형": "select",
            "변경영역": "select",
            "이전값": "rich_text",
            "새값": "rich_text",
            "변경사유": "rich_text",
            "기록방식": "select",
            "신뢰도": "number",
            "변경일시": "date",
            "사람확인": "checkbox",
            "근거URL": "url",
            "사건": "relation",
            "업무": "relation",
            "비용·청구": "relation",
            "그룹": "relation",
            "검토항목": "relation",
            "근거자료": "relation",
        },
        {
            "사건": "한국 특허 사건",
            "업무": "업무·절차",
            "비용·청구": "비용·청구",
            "그룹": "그룹",
            "검토항목": "검토함",
            "근거자료": "근거자료",
        },
    ),
}


NOTION_BINDING_VERSION = 1
NotionSchemaProvider = Callable[[str], Mapping[str, Any]]


def build_notion_binding(
    items: Iterable[ProposedChange | ProposalOperation],
    data_source_ids: Mapping[str, str],
    definitions: Mapping[str, DatabaseDefinition] = NOTION_DEFINITIONS,
    schema_provider: NotionSchemaProvider | None = None,
) -> dict[str, Any]:
    """Create the immutable destination/schema scope covered by an approval.

    Only properties needed by the proposal (plus each database title used for
    matching) are captured. When a schema provider is supplied, the exact
    relevant Notion property definitions are also captured, including select
    options and relation targets.
    """

    required: dict[str, set[str]] = {}
    for item in items:
        change = item.change if isinstance(item, ProposalOperation) else item
        definition = definitions.get(change.target_database)
        if definition is None:
            raise NotionSchemaError(
                f"Unknown Notion database: {change.target_database}"
            )
        if change.property_name not in definition.properties:
            raise NotionSchemaError(
                f"Unknown property: {change.target_database}.{change.property_name}"
            )
        required.setdefault(change.target_database, set()).update(
            {definition.title_property, change.property_name}
        )

    databases: dict[str, Any] = {}
    for database_name in sorted(required):
        definition = definitions[database_name]
        data_source_id = data_source_ids.get(database_name)
        property_names = sorted(required[database_name])
        property_types = {
            name: definition.properties[name]
            for name in property_names
        }
        relations: dict[str, Any] = {}
        for property_name, property_type in property_types.items():
            if property_type != "relation":
                continue
            target_database = definition.relations.get(property_name)
            if not target_database:
                raise NotionSchemaError(
                    f"Missing relation target for {database_name}.{property_name}"
                )
            relations[property_name] = {
                "target_database": target_database,
                "target_data_source_id": data_source_ids.get(target_database),
            }
        entry: dict[str, Any] = {
            "data_source_id": data_source_id,
            "title_property": definition.title_property,
            "property_types": property_types,
            "relations": relations,
        }
        if schema_provider is not None and data_source_id:
            schema = schema_provider(data_source_id)
            properties = schema.get("properties") if isinstance(schema, Mapping) else None
            if not isinstance(properties, Mapping):
                raise NotionSchemaError(
                    f"Notion data source {database_name} has no property schema"
                )
            remote_properties: dict[str, Any] = {}
            for property_name, expected_type in property_types.items():
                actual = properties.get(property_name)
                if not isinstance(actual, Mapping):
                    raise NotionSchemaError(
                        f"Notion property does not exist: "
                        f"{database_name}.{property_name}"
                    )
                if actual.get("type") != expected_type:
                    raise NotionSchemaError(
                        f"Notion property type mismatch for "
                        f"{database_name}.{property_name}"
                    )
                if expected_type == "relation":
                    target = relations[property_name]["target_data_source_id"]
                    relation = actual.get("relation")
                    actual_target = (
                        relation.get("data_source_id")
                        or relation.get("database_id")
                        if isinstance(relation, Mapping)
                        else None
                    )
                    if not target or str(actual_target or "") != str(target):
                        raise NotionSchemaError(
                            f"Relation target mismatch for "
                            f"{database_name}.{property_name}"
                        )
                remote_properties[property_name] = deepcopy(dict(actual))
            entry["remote_properties"] = remote_properties
        databases[database_name] = entry
    return {
        "version": NOTION_BINDING_VERSION,
        "databases": databases,
    }


def _binding_structure(binding: Mapping[str, Any]) -> dict[str, Any]:
    structure = deepcopy(dict(binding))
    databases = structure.get("databases")
    if isinstance(databases, dict):
        for entry in databases.values():
            if isinstance(entry, dict):
                entry.pop("remote_properties", None)
    return structure


def assert_notion_binding_matches(
    binding: Mapping[str, Any],
    items: Iterable[ProposedChange | ProposalOperation],
    data_source_ids: Mapping[str, str],
    definitions: Mapping[str, DatabaseDefinition] = NOTION_DEFINITIONS,
    *,
    schema_provider: NotionSchemaProvider | None = None,
    database_name: str | None = None,
    require_exact_schema: bool = False,
) -> None:
    """Fail closed if current destinations or relevant schema differ."""

    if not binding:
        raise NotionSchemaError("Proposal has no approved Notion destination binding")
    expected_structure = build_notion_binding(
        items,
        data_source_ids,
        definitions,
    )
    if canonical_json(_binding_structure(binding)) != canonical_json(
        expected_structure
    ):
        raise NotionSchemaError(
            "Notion destination or local schema mapping changed after approval"
        )
    databases = binding.get("databases")
    if not isinstance(databases, Mapping):
        raise NotionSchemaError("Approved Notion destination binding is malformed")
    selected = [database_name] if database_name is not None else list(databases)
    for selected_name in selected:
        entry = databases.get(selected_name)
        if not isinstance(entry, Mapping):
            raise NotionSchemaError(
                f"Approved Notion binding is missing database: {selected_name}"
            )
        data_source_id = entry.get("data_source_id")
        remote_properties = entry.get("remote_properties")
        if require_exact_schema and data_source_id and not isinstance(
            remote_properties, Mapping
        ):
            raise NotionSchemaError(
                f"Approval did not bind the exact Notion schema for {selected_name}"
            )
        if not isinstance(remote_properties, Mapping):
            continue
        if schema_provider is None or not data_source_id:
            raise NotionSchemaError(
                f"Cannot verify the approved Notion schema for {selected_name}"
            )
        schema = schema_provider(str(data_source_id))
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(properties, Mapping):
            raise NotionSchemaError(
                f"Notion data source {selected_name} has no property schema"
            )
        current_projection = {
            property_name: deepcopy(dict(properties[property_name]))
            for property_name in remote_properties
            if isinstance(properties.get(property_name), Mapping)
        }
        if canonical_json(current_projection) != canonical_json(remote_properties):
            raise NotionSchemaError(
                f"Notion schema changed after approval for {selected_name}"
            )


def _plain_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(
            str(item.get("plain_text") or item.get("text", {}).get("content") or "")
            for item in value
            if isinstance(item, Mapping)
        )
    return str(value or "")


def decode_property(raw: Any, property_type: str) -> JsonValue:
    if not isinstance(raw, Mapping):
        return None
    if property_type in {"title", "rich_text"}:
        return _plain_text(raw.get(property_type, []))
    if property_type == "select":
        selected = raw.get("select")
        return selected.get("name") if isinstance(selected, Mapping) else None
    if property_type == "multi_select":
        return [
            item.get("name")
            for item in raw.get("multi_select", [])
            if isinstance(item, Mapping)
        ]
    if property_type == "date":
        date = raw.get("date")
        return date.get("start") if isinstance(date, Mapping) else None
    if property_type == "relation":
        return [item.get("id") for item in raw.get("relation", []) if isinstance(item, Mapping)]
    return raw.get(property_type)


_CORRECTION_TARGET_KEYS = frozenset(
    {
        "version",
        "database",
        "data_source_id",
        "entity_key",
        "property",
        "page_id",
        "parent",
    }
)


def _page_parent_identity(page: Mapping[str, Any]) -> dict[str, str]:
    parent = page.get("parent")
    if not isinstance(parent, Mapping):
        raise NotionSchemaError("Correction target page has no parent binding")
    parent_type = parent.get("type")
    if parent_type not in {"data_source_id", "database_id"}:
        raise NotionSchemaError("Correction target page has an unsupported parent")
    parent_id = parent.get(str(parent_type))
    if not isinstance(parent_id, str) or not parent_id.strip():
        raise NotionSchemaError("Correction target page parent ID is missing")
    return {"type": str(parent_type), "id": parent_id}


def _assert_exact_page_binding(
    page: Mapping[str, Any],
    *,
    page_id: str,
    data_source_id: str,
    title_property: str,
    match_value: str,
    label: str,
) -> None:
    if str(page.get("id") or "") != page_id:
        raise NotionSchemaError(f"{label} page identity changed")
    if page.get("archived") is True or page.get("in_trash") is True:
        raise NotionSchemaError(f"{label} page is archived or in trash")
    parent = _page_parent_identity(page)
    if parent["id"] != data_source_id:
        raise NotionSchemaError(f"{label} page belongs to another data source")
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        raise NotionSchemaError(f"{label} page has no property values")
    actual_match = decode_property(properties.get(title_property), "title")
    if actual_match != match_value:
        raise NotionSchemaError(f"{label} page title no longer matches its binding")


def build_correction_target_binding(
    page: Mapping[str, Any],
    *,
    database_name: str,
    data_source_id: str,
    entity_key: str,
    property_name: str,
) -> dict[str, JsonValue]:
    """Bind an independent correction to one exact existing Notion page."""

    page_id = page.get("id")
    if not isinstance(page_id, str) or not page_id.strip():
        raise NotionSchemaError("Correction target page ID is missing")
    if not data_source_id.strip():
        raise NotionSchemaError("Correction target data source ID is missing")
    parent = _page_parent_identity(page)
    if parent["id"] != data_source_id:
        raise NotionSchemaError(
            "Correction target page belongs to a different data source"
        )
    return {
        "version": 1,
        "database": database_name,
        "data_source_id": data_source_id,
        "entity_key": entity_key,
        "property": property_name,
        "page_id": page_id,
        "parent": parent,
    }


def _validated_correction_target(
    proposal: ProposalRevision,
    operation: ProposalOperation,
    data_source_id: str,
) -> Mapping[str, Any]:
    target = proposal.correction_binding.get("target")
    if not isinstance(target, Mapping) or set(target) != _CORRECTION_TARGET_KEYS:
        raise NotionSchemaError(
            "Independent correction has no exact Notion page binding"
        )
    change = operation.change
    expected = {
        "version": 1,
        "database": change.target_database,
        "data_source_id": data_source_id,
        "entity_key": change.entity_key,
        "property": change.property_name,
    }
    if any(target.get(key) != value for key, value in expected.items()):
        raise NotionSchemaError(
            "Independent correction target differs from its approved operation"
        )
    page_id = target.get("page_id")
    parent = target.get("parent")
    if (
        not isinstance(page_id, str)
        or not page_id.strip()
        or not isinstance(parent, Mapping)
        or set(parent) != {"type", "id"}
        or parent.get("type") not in {"data_source_id", "database_id"}
        or not isinstance(parent.get("id"), str)
        or not str(parent.get("id")).strip()
        or parent.get("id") != data_source_id
    ):
        raise NotionSchemaError(
            "Independent correction target binding is malformed"
        )
    return target


def _assert_bound_correction_page(
    page: Mapping[str, Any],
    target: Mapping[str, Any],
) -> None:
    if str(page.get("id") or "") != target["page_id"]:
        raise NotionSchemaError(
            "Independent correction Notion page identity changed after approval"
        )
    if _page_parent_identity(page) != dict(target["parent"]):
        raise NotionSchemaError(
            "Independent correction Notion page parent changed after approval"
        )


def _correction_match_value(proposal: ProposalRevision) -> str:
    case_number = proposal.correction_binding.get("case_number")
    if not isinstance(case_number, str) or not case_number.strip():
        raise NotionSchemaError(
            "Independent correction has no exact title match binding"
        )
    return case_number


def encode_property(
    value: JsonValue,
    property_type: str,
    relation_ids: list[str] | None = None,
) -> dict[str, Any]:
    if property_type in {"title", "rich_text"}:
        return {property_type: [] if value in (None, "") else [{"text": {"content": str(value)}}]}
    if property_type == "select":
        return {"select": None if value in (None, "") else {"name": str(value)}}
    if property_type == "multi_select":
        values = value if isinstance(value, list) else ([] if value is None else [value])
        return {"multi_select": [{"name": str(item)} for item in values]}
    if property_type == "number":
        return {"number": None if value in (None, "") else float(value)}
    if property_type == "checkbox":
        return {"checkbox": bool(value)}
    if property_type == "date":
        return {"date": None if value in (None, "") else {"start": str(value)}}
    if property_type == "relation":
        return {"relation": [{"id": item} for item in relation_ids or []]}
    if property_type in {"url", "email", "phone_number"}:
        return {property_type: None if value in (None, "") else str(value)}
    raise NotionSchemaError(f"Unsupported Notion property type: {property_type}")


class ExactNotionMutationVerifier:
    """Bind a receipt operation to its exact destination and encoded value."""

    # Expiry limits starting a write run. Once begin_apply atomically consumes the
    # nonce and enters APPLYING, this verifier may reconcile only the still-pending
    # exact operation scope after TTL. Generic adapter verifiers do not receive this
    # capability and continue to reject every expired receipt.
    allows_expired_started_receipt = True

    def __init__(
        self,
        approval: ApprovalService,
        database: StateDatabase,
        data_source_ids: Mapping[str, str],
        definitions: Mapping[str, DatabaseDefinition] = NOTION_DEFINITIONS,
    ) -> None:
        self.approval = approval
        self.database = database
        self.data_source_ids = data_source_ids
        self.definitions = definitions
        self._proposal: ProposalRevision | None = None
        self._operations: dict[str, ProposalOperation] = {}
        self._title_values: dict[tuple[str, str], JsonValue] = {}
        self._outbox: dict[str, str] | None = None
        self._batch_operation_ids: set[str] | None = None
        self._verified_receipts: set[tuple[str, int, str, str]] = set()
        self._consumed_operation_ids: set[str] = set()
        self._binding_verified = False
        self._mapping_cache: dict[
            tuple[str, str],
            dict[str, str] | None,
        ] = {}

    def begin_batch_verification(self, operation_ids: Sequence[str]) -> None:
        items = tuple(operation_ids)
        if (
            not items
            or any(not item.strip() for item in items)
            or len(set(items)) != len(items)
        ):
            raise NotionSchemaError("Approved mutation batch is malformed")
        self._batch_operation_ids = set(items)
        self._outbox = None
        self._mapping_cache.clear()

    def end_batch_verification(
        self,
        operation_ids: Sequence[str],
        *,
        verified: bool,
    ) -> None:
        del verified
        if self._batch_operation_ids != set(operation_ids):
            raise NotionSchemaError("Approved mutation batch scope changed")
        self._batch_operation_ids = None

    def _load_proposal(self, receipt: ApprovalReceipt) -> ProposalRevision:
        if self._proposal is None:
            proposal = self.database.load_proposal(
                receipt.proposal_id,
                receipt.revision,
            )
            self._proposal = proposal
            self._operations = {
                item.change.operation_id: item
                for item in proposal.operations
                if item.action in {ProposalAction.APPLY, ProposalAction.EDIT}
            }
            for item in self._operations.values():
                definition = self.definitions.get(
                    item.change.target_database
                )
                if (
                    definition is not None
                    and item.change.property_name
                    == definition.title_property
                ):
                    self._title_values[
                        (
                            item.change.target_database,
                            item.change.entity_key,
                        )
                    ] = item.approved_value
        proposal = self._proposal
        if (
            proposal.proposal_id != receipt.proposal_id
            or proposal.revision != receipt.revision
        ):
            raise NotionSchemaError("Approval receipt targets another proposal")
        return proposal

    def _mapping(
        self,
        database_name: str,
        entity_key: str,
    ) -> dict[str, str] | None:
        key = (database_name, entity_key)
        if key not in self._mapping_cache:
            self._mapping_cache[key] = self.database.get_entity_mapping(
                database_name,
                entity_key,
            )
        return self._mapping_cache[key]

    @staticmethod
    def _approved_schema_provider(
        proposal: ProposalRevision,
    ) -> NotionSchemaProvider | None:
        databases = proposal.notion_binding.get("databases")
        if not isinstance(databases, Mapping):
            return None
        schemas: dict[str, Mapping[str, Any]] = {}
        for entry in databases.values():
            if not isinstance(entry, Mapping):
                continue
            data_source_id = entry.get("data_source_id")
            remote_properties = entry.get("remote_properties")
            if (
                isinstance(data_source_id, str)
                and isinstance(remote_properties, Mapping)
            ):
                schemas[data_source_id] = {
                    "properties": remote_properties,
                }
        if not schemas:
            return None

        def provider(data_source_id: str) -> Mapping[str, Any]:
            try:
                return schemas[data_source_id]
            except KeyError as exc:
                raise NotionSchemaError(
                    "Approved Notion schema binding is incomplete"
                ) from exc

        return provider

    def __call__(
        self,
        receipt: ApprovalReceipt,
        operation_id: str,
        data_source_id: str,
        match: NotionMatch,
        entity_key: JsonValue,
        properties: Mapping[str, Any],
        precondition: NotionPrecondition | None,
    ) -> bool:
        try:
            proposal = self._load_proposal(receipt)
            if (
                self._batch_operation_ids is not None
                and operation_id not in self._batch_operation_ids
            ):
                return False
            if operation_id in self._consumed_operation_ids:
                return False
            if proposal.status is not ProposalStatus.APPLYING:
                return False
            if self._outbox is None:
                self._outbox = self.database.outbox_statuses(
                    proposal.proposal_id,
                    proposal.revision,
                )
            if self._outbox.get(operation_id) != "pending":
                return False
            operation = self._operations.get(operation_id)
            if operation is None:
                return False
            receipt_key = (
                receipt.proposal_id,
                receipt.revision,
                receipt.nonce,
                receipt.signature,
            )
            if receipt_key not in self._verified_receipts:
                if not self.database.nonce_used(receipt.nonce):
                    return False
                if not self.approval.verify_operation_scope(
                    receipt,
                    proposal,
                    operation_id,
                ):
                    return False
                self._verified_receipts.add(receipt_key)
            if not self._binding_verified:
                assert_notion_binding_matches(
                    proposal.notion_binding,
                    proposal.operations,
                    self.data_source_ids,
                    self.definitions,
                    schema_provider=self._approved_schema_provider(
                        proposal
                    ),
                )
                self._binding_verified = True
            change = operation.change
            definition = self.definitions[change.target_database]
            if data_source_id != self.data_source_ids[change.target_database]:
                return False
            if proposal.purpose is ProposalPurpose.USER_CORRECTION:
                target = _validated_correction_target(
                    proposal,
                    operation,
                    data_source_id,
                )
                if (
                    precondition is None
                    or precondition.page_id != target["page_id"]
                ):
                    return False
            else:
                mapping = self._mapping(
                    change.target_database,
                    change.entity_key,
                )
                if mapping is not None and (
                    precondition is None
                    or precondition.page_id != mapping["notion_page_id"]
                ):
                    return False
            if (
                match.property_name != definition.title_property
                or match.property_type != "title"
            ):
                return False
            if entity_key != self._match_value(proposal, operation, definition):
                return False
            property_type = definition.properties[change.property_name]
            relation_ids = self._relation_ids(
                operation,
                definition,
                property_type,
            )
            expected = {
                change.property_name: encode_property(
                    operation.approved_value,
                    property_type,
                    relation_ids,
                )
            }
            if canonical_json(dict(properties)) != canonical_json(expected):
                return False
            if (
                precondition is None
                or precondition.property_name != change.property_name
            ):
                return False
            verified = (
                decode_property(precondition.property_value, property_type)
                == change.current_value
            )
            if verified:
                self._consumed_operation_ids.add(operation_id)
            return verified
        except (KeyError, StopIteration, TypeError, ValueError):
            return False

    def _match_value(
        self,
        proposal: ProposalRevision,
        operation: ProposalOperation,
        definition: DatabaseDefinition,
    ) -> str:
        change = operation.change
        if proposal.purpose is ProposalPurpose.USER_CORRECTION:
            return _correction_match_value(proposal)
        mapping = self._mapping(
            change.target_database,
            change.entity_key,
        )
        if mapping:
            return mapping["match_value"]
        title_value = self._title_values.get(
            (
                change.target_database,
                change.entity_key,
            )
        )
        if title_value not in (None, ""):
            return str(title_value)
        return change.entity_key.split(":", 1)[-1]

    def _relation_ids(
        self,
        operation: ProposalOperation,
        definition: DatabaseDefinition,
        property_type: str,
    ) -> list[str] | None:
        if property_type != "relation":
            return None
        change = operation.change
        target_database = definition.relations[change.property_name]
        logical_keys = (
            operation.approved_value
            if isinstance(operation.approved_value, list)
            else (
                []
                if operation.approved_value is None
                else [operation.approved_value]
            )
        )
        page_ids: list[str] = []
        for logical_key in logical_keys:
            mapping = self._mapping(
                target_database,
                str(logical_key),
            )
            if mapping is None:
                raise NotionSchemaError(
                    "Approved relation target has no verified page mapping"
                )
            page_ids.append(mapping["notion_page_id"])
        return page_ids


class NotionCurrentValueReader:
    DEFAULT_BULK_READ_THRESHOLD = 20

    def __init__(
        self,
        gateway: NotionGateway,
        data_source_ids: Mapping[str, str],
        definitions: Mapping[str, DatabaseDefinition] = NOTION_DEFINITIONS,
        database: StateDatabase | None = None,
        *,
        bulk_read_threshold: int = DEFAULT_BULK_READ_THRESHOLD,
    ) -> None:
        if isinstance(bulk_read_threshold, bool) or bulk_read_threshold < 1:
            raise ValueError("bulk_read_threshold must be a positive integer")
        self.gateway = gateway
        self.data_source_ids = data_source_ids
        self.definitions = definitions
        self.database = database
        self.bulk_read_threshold = bulk_read_threshold
        # A reader is constructed once per authenticated prepare command.
        # These caches intentionally live only for that reader instance so
        # separate synchronization commands cannot reuse stale Notion state.
        self._mapping_cache: dict[
            tuple[str, str],
            dict[str, Any] | None,
        ] = {}
        self._page_cache: dict[
            tuple[str, str, str, str],
            Mapping[str, Any] | None,
        ] = {}
        self._bulk_title_index: dict[
            tuple[str, str, str],
            dict[str, list[Mapping[str, Any]]],
        ] = {}
        self._unmapped_title_owner: dict[
            tuple[str, str],
            tuple[str, str],
        ] = {}

    def clear_cache(self) -> None:
        """Discard the current prepare command's read snapshot."""

        self._mapping_cache.clear()
        self._page_cache.clear()
        self._bulk_title_index.clear()
        self._unmapped_title_owner.clear()

    def _query_all_pages(
        self,
        data_source_id: str,
    ) -> list[Mapping[str, Any]]:
        query = (
            self.gateway.query_database
            if getattr(self.gateway, "uses_data_sources", True) is False
            else self.gateway.query_data_source
        )
        pages = query(data_source_id, page_size=100)
        if not isinstance(pages, list) or not all(
            isinstance(page, Mapping) for page in pages
        ):
            raise NotionSchemaError(
                "Notion bulk query returned an invalid page collection"
            )
        return pages

    def _load_unmapped_group(
        self,
        database_name: str,
        cache_keys: list[tuple[str, str]],
        title_values: Mapping[tuple[str, str], str],
        page_cache: dict[
            tuple[str, str],
            Mapping[str, Any] | None,
        ],
    ) -> None:
        definition = self.definitions[database_name]
        data_source_id = self.data_source_ids[database_name]
        match_values = {
            cache_key: title_values.get(cache_key, cache_key[1])
            for cache_key in cache_keys
        }
        requested_by_title: dict[str, tuple[str, str]] = {}
        pending: list[tuple[str, str]] = []
        for cache_key, match_value in match_values.items():
            previous = requested_by_title.get(match_value)
            if previous is not None and previous != cache_key:
                raise NotionSchemaError(
                    "Multiple requested entities resolve to the same "
                    "Notion title"
                )
            previous = self._unmapped_title_owner.get(
                (database_name, match_value)
            )
            if previous is not None and previous != cache_key:
                raise NotionSchemaError(
                    "Multiple requested entities resolve to the same "
                    "Notion title"
                )
            requested_by_title[match_value] = cache_key
        for match_value, cache_key in requested_by_title.items():
            self._unmapped_title_owner[
                (database_name, match_value)
            ] = cache_key

        for cache_key, match_value in match_values.items():
            resolution_key = (
                database_name,
                cache_key[1],
                "title",
                match_value,
            )
            if resolution_key in self._page_cache:
                page_cache[cache_key] = self._page_cache[resolution_key]
            else:
                pending.append(cache_key)
        if not pending:
            return

        bulk_key = (
            database_name,
            data_source_id,
            definition.title_property,
        )
        if (
            bulk_key not in self._bulk_title_index
            and len(cache_keys) < self.bulk_read_threshold
        ):
            match = NotionMatch(definition.title_property, "title")
            for cache_key in pending:
                page = self.gateway.find_page(
                    data_source_id,
                    match,
                    match_values[cache_key],
                )
                page_cache[cache_key] = page
                self._page_cache[
                    (
                        database_name,
                        cache_key[1],
                        "title",
                        match_values[cache_key],
                    )
                ] = page
            return

        if bulk_key not in self._bulk_title_index:
            title_index: dict[str, list[Mapping[str, Any]]] = {}
            for page in self._query_all_pages(data_source_id):
                properties = page.get("properties")
                if not isinstance(properties, Mapping):
                    continue
                title = decode_property(
                    properties.get(definition.title_property),
                    "title",
                )
                if not isinstance(title, str):
                    continue
                title_index.setdefault(title, []).append(page)
            self._bulk_title_index[bulk_key] = title_index
        title_index = self._bulk_title_index[bulk_key]

        for cache_key in pending:
            match_value = match_values[cache_key]
            matches = title_index.get(match_value, [])
            if len(matches) > 1:
                raise NotionSchemaError(
                    "Multiple Notion pages match a requested title"
                )
            page = matches[0] if matches else None
            page_cache[cache_key] = page
            self._page_cache[
                (
                    database_name,
                    cache_key[1],
                    "title",
                    match_value,
                )
            ] = page

    def load(
        self,
        keys: Iterable[tuple[str, str, str]],
        title_values: Mapping[tuple[str, str], str],
    ) -> dict[tuple[str, str, str], JsonValue]:
        result: dict[tuple[str, str, str], JsonValue] = {}
        page_cache: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        requested_properties: dict[tuple[str, str], set[str]] = {}
        for database, entity_key, property_name in keys:
            definition = self.definitions.get(database)
            data_source_id = self.data_source_ids.get(database)
            if (
                not definition
                or not data_source_id
                or property_name not in definition.properties
            ):
                continue
            cache_key = (database, entity_key)
            requested_properties.setdefault(cache_key, set()).add(
                property_name
            )

        unmapped_by_database: dict[str, list[tuple[str, str]]] = {}
        for cache_key in requested_properties:
            database_name, entity_key = cache_key
            definition = self.definitions[database_name]
            data_source_id = self.data_source_ids[database_name]
            if cache_key not in self._mapping_cache:
                self._mapping_cache[cache_key] = (
                    self.database.get_entity_mapping(
                        database_name,
                        entity_key,
                    )
                    if self.database
                    else None
                )
            mapping = self._mapping_cache[cache_key]
            if mapping is None:
                unmapped_by_database.setdefault(
                    database_name,
                    [],
                ).append(cache_key)
                continue
            resolution_key = (
                database_name,
                entity_key,
                "page",
                f"{mapping['notion_page_id']}\0{mapping['match_value']}",
            )
            if resolution_key not in self._page_cache:
                page = self.gateway.get_page(mapping["notion_page_id"])
                if not isinstance(page, Mapping):
                    raise NotionSchemaError(
                        "Mapped Notion page could not be read"
                    )
                _assert_exact_page_binding(
                    page,
                    page_id=mapping["notion_page_id"],
                    data_source_id=data_source_id,
                    title_property=definition.title_property,
                    match_value=mapping["match_value"],
                    label="Mapped Notion",
                )
                self._page_cache[resolution_key] = page
            page = self._page_cache[resolution_key]
            page_cache[cache_key] = page

        for database_name, cache_keys in unmapped_by_database.items():
            self._load_unmapped_group(
                database_name,
                cache_keys,
                title_values,
                page_cache,
            )

        for cache_key, property_names in requested_properties.items():
            database_name, entity_key = cache_key
            definition = self.definitions[database_name]
            page = page_cache[cache_key]
            properties = page.get("properties") if page else None
            for property_name in property_names:
                raw = (
                    properties.get(property_name)
                    if isinstance(properties, Mapping)
                    else None
                )
                result[
                    (database_name, entity_key, property_name)
                ] = decode_property(
                    raw,
                    definition.properties[property_name],
                )
        return result


class ApprovedNotionWriter:
    def __init__(
        self,
        gateway: NotionGateway,
        database: StateDatabase,
        proposal: ProposalRevision,
        data_source_ids: Mapping[str, str],
        definitions: Mapping[str, DatabaseDefinition] = NOTION_DEFINITIONS,
        *,
        require_exact_schema: bool = False,
    ) -> None:
        self.gateway = gateway
        self.database = database
        self.proposal = proposal
        self.data_source_ids = data_source_ids
        self.definitions = definitions
        self.require_exact_schema = require_exact_schema
        self._defer_live_schema = False
        self._schema_cache: dict[str, Mapping[str, Any] | None] = {}
        self._binding_cache: set[str] = set()
        self._mapping_cache: dict[
            tuple[str, str],
            dict[str, str] | None,
        ] = {}
        self._relation_validation_cache: dict[
            tuple[str, str, str, str],
            str,
        ] = {}
        self._planned_relation_misses: set[tuple[str, str]] = set()
        self._mutated_pages: dict[
            tuple[str, str],
            tuple[str, bool],
        ] = {}
        self._intents: dict[str, NotionPrecondition] = {}
        self._values: dict[tuple[str, str, str], JsonValue] = {}
        for operation in proposal.operations:
            if operation.action not in {ProposalAction.APPLY, ProposalAction.EDIT}:
                continue
            self._values[
                (
                    operation.change.target_database,
                    operation.change.entity_key,
                    operation.change.property_name,
                )
            ] = operation.approved_value

    def _definition(self, database_name: str) -> tuple[DatabaseDefinition, str]:
        if database_name not in self.definitions:
            raise NotionSchemaError(f"Unknown Notion database: {database_name}")
        if database_name not in self.data_source_ids:
            raise NotionSchemaError(f"Missing Notion data source ID for: {database_name}")
        return self.definitions[database_name], self.data_source_ids[database_name]

    def _schema(
        self,
        data_source_id: str,
        *,
        force_fresh: bool = False,
    ) -> Mapping[str, Any]:
        getter = getattr(self.gateway, "get_data_source", None)
        if getter is None:
            raise NotionSchemaError("Notion gateway cannot read destination schema")
        if force_fresh:
            self._schema_cache.pop(data_source_id, None)
        if data_source_id not in self._schema_cache:
            self._schema_cache[data_source_id] = getter(data_source_id)
        schema = self._schema_cache[data_source_id]
        if not isinstance(schema, Mapping):
            raise NotionSchemaError(
                f"Notion returned no schema for data source {data_source_id}"
            )
        return schema

    def _approved_bound_schema(
        self,
        data_source_id: str,
    ) -> Mapping[str, Any] | None:
        databases = self.proposal.notion_binding.get("databases")
        if not isinstance(databases, Mapping):
            return None
        for entry in databases.values():
            if (
                isinstance(entry, Mapping)
                and entry.get("data_source_id") == data_source_id
                and isinstance(entry.get("remote_properties"), Mapping)
            ):
                return {"properties": entry["remote_properties"]}
        return None

    def begin_global_preflight(self) -> None:
        """Use the approved schema projection until the forced live check."""

        self._defer_live_schema = True
        self._schema_cache.clear()
        self._binding_cache.clear()

    def _assert_destination_binding(
        self,
        database_name: str,
        *,
        force_fresh: bool = False,
    ) -> None:
        if force_fresh:
            self._binding_cache.discard(database_name)
        if database_name in self._binding_cache:
            return
        getter = getattr(self.gateway, "get_data_source", None)
        if self._defer_live_schema:
            provider = self._approved_bound_schema
        else:
            provider = (
                (lambda data_source_id: self._schema(data_source_id))
                if getter is not None
                else None
            )
        assert_notion_binding_matches(
            self.proposal.notion_binding,
            self.proposal.operations,
            self.data_source_ids,
            self.definitions,
            schema_provider=provider,
            database_name=database_name,
            require_exact_schema=self.require_exact_schema,
        )
        self._binding_cache.add(database_name)

    def _validate_remote_schema(
        self,
        database_name: str,
        property_name: str,
        approved_value: JsonValue,
    ) -> None:
        definition, data_source_id = self._definition(database_name)
        getter = getattr(self.gateway, "get_data_source", None)
        if self._defer_live_schema:
            schema = self._approved_bound_schema(data_source_id)
            if schema is None:
                return
        elif getter is None:
            return
        else:
            schema = self._schema(data_source_id)
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise NotionSchemaError(
                f"Notion data source {database_name} has no property schema"
            )
        actual = properties.get(property_name)
        if not isinstance(actual, Mapping):
            raise NotionSchemaError(
                f"Notion property does not exist: {database_name}.{property_name}"
            )
        expected_type = definition.properties[property_name]
        if actual.get("type") != expected_type:
            raise NotionSchemaError(
                f"Notion property type mismatch for {database_name}.{property_name}: "
                f"expected {expected_type}, got {actual.get('type')}"
            )
        if expected_type in {"select", "multi_select"}:
            option_rows = actual.get(expected_type, {}).get("options", [])
            option_names = {
                str(item.get("name"))
                for item in option_rows
                if isinstance(item, Mapping) and item.get("name") is not None
            }
            requested = (
                approved_value
                if isinstance(approved_value, list)
                else ([] if approved_value in (None, "") else [approved_value])
            )
            unknown = [str(item) for item in requested if str(item) not in option_names]
            if unknown:
                raise NotionSchemaError(
                    f"Unknown Notion option for {database_name}.{property_name}: {unknown}"
                )
        if expected_type == "relation":
            target_database = definition.relations[property_name]
            expected_target = self.data_source_ids.get(target_database)
            relation = actual.get("relation")
            if isinstance(relation, Mapping) and expected_target:
                actual_target = relation.get("data_source_id") or relation.get(
                    "database_id"
                )
                if actual_target and str(actual_target) != expected_target:
                    raise NotionSchemaError(
                        f"Relation target mismatch for {database_name}.{property_name}"
                    )

    def prepare_apply(self, operations: Sequence[ProposalOperation]) -> None:
        """Force one fresh exact schema/binding check before nonce consumption."""

        items = tuple(operations)
        self._defer_live_schema = False
        self._schema_cache.clear()
        self._binding_cache.clear()
        self._mapping_cache.clear()
        self._relation_validation_cache.clear()
        databases: list[str] = []
        seen_databases: set[str] = set()
        for operation in items:
            if operation.action not in {ProposalAction.APPLY, ProposalAction.EDIT}:
                raise NotionSchemaError("Apply preparation contains a non-write action")
            database_name = operation.change.target_database
            if database_name not in seen_databases:
                databases.append(database_name)
                seen_databases.add(database_name)
        for database_name in databases:
            _, data_source_id = self._definition(database_name)
            if getattr(self.gateway, "get_data_source", None) is not None:
                self._schema(data_source_id, force_fresh=True)
            self._assert_destination_binding(database_name)
        for operation in items:
            change = operation.change
            self._validate_remote_schema(
                change.target_database,
                change.property_name,
                operation.approved_value,
            )

    @staticmethod
    def _validated_operation_batch(
        operations: Sequence[ProposalOperation],
    ) -> tuple[ProposalOperation, ...]:
        items = tuple(operations)
        if not items:
            raise NotionSchemaError("Notion operation batch must not be empty")
        first = items[0].change
        if not first.target_database.strip() or not first.entity_key.strip():
            raise NotionSchemaError("Notion operation batch has an empty target")
        operation_ids: set[str] = set()
        property_names: set[str] = set()
        for operation in items:
            change = operation.change
            if operation.action not in {ProposalAction.APPLY, ProposalAction.EDIT}:
                raise NotionSchemaError("Notion operation batch contains a non-write")
            if (
                change.target_database != first.target_database
                or change.entity_key != first.entity_key
            ):
                raise NotionSchemaError("Notion operation batch has mixed page targets")
            if (
                not change.operation_id.strip()
                or not change.property_name.strip()
                or change.operation_id in operation_ids
                or change.property_name in property_names
            ):
                raise NotionSchemaError(
                    "Notion operation batch has duplicate or empty scope"
                )
            operation_ids.add(change.operation_id)
            property_names.add(change.property_name)
        return items

    def _mapping(
        self,
        database_name: str,
        entity_key: str,
    ) -> dict[str, str] | None:
        key = (database_name, entity_key)
        if key not in self._mapping_cache:
            self._mapping_cache[key] = self.database.get_entity_mapping(
                database_name,
                entity_key,
            )
        return self._mapping_cache[key]

    def _match_value(self, database_name: str, entity_key: str) -> str:
        definition, _ = self._definition(database_name)
        if self.proposal.purpose is ProposalPurpose.USER_CORRECTION:
            return _correction_match_value(self.proposal)
        mapping = self._mapping(database_name, entity_key)
        if mapping:
            return mapping["match_value"]
        title = self._values.get((database_name, entity_key, definition.title_property))
        if title not in (None, ""):
            return str(title)
        return entity_key.split(":", 1)[-1]

    def _find_page(self, database_name: str, entity_key: str) -> Mapping[str, Any] | None:
        definition, data_source_id = self._definition(database_name)
        return self.gateway.find_page(
            data_source_id,
            NotionMatch(definition.title_property, "title"),
            self._match_value(database_name, entity_key),
        )

    def _operation_page(
        self,
        operation: ProposalOperation,
    ) -> Mapping[str, Any] | None:
        change = operation.change
        if self.proposal.purpose is not ProposalPurpose.USER_CORRECTION:
            definition, data_source_id = self._definition(
                change.target_database
            )
            mapping = self._mapping(
                change.target_database,
                change.entity_key,
            )
            if mapping is not None:
                page = self.gateway.get_page(mapping["notion_page_id"])
                if not isinstance(page, Mapping):
                    raise NotionSchemaError(
                        "Mapped Notion page could not be read"
                    )
                try:
                    _assert_exact_page_binding(
                        page,
                        page_id=mapping["notion_page_id"],
                        data_source_id=data_source_id,
                        title_property=definition.title_property,
                        match_value=mapping["match_value"],
                        label="Mapped Notion",
                    )
                except NotionSchemaError:
                    properties = page.get("properties")
                    approved_title = self._values.get(
                        (
                            change.target_database,
                            change.entity_key,
                            definition.title_property,
                        )
                    )
                    actual_title = (
                        decode_property(
                            properties.get(definition.title_property),
                            "title",
                        )
                        if isinstance(properties, Mapping)
                        else None
                    )
                    if (
                        self.proposal.status
                        not in {ProposalStatus.APPLYING, ProposalStatus.FAILED}
                        or approved_title in (None, "")
                        or actual_title != approved_title
                        or str(page.get("id") or "")
                        != mapping["notion_page_id"]
                        or page.get("archived") is True
                        or page.get("in_trash") is True
                        or _page_parent_identity(page)["id"] != data_source_id
                    ):
                        raise
                    try:
                        started_receipt = self.database.load_latest_receipt(
                            self.proposal.proposal_id,
                            self.proposal.revision,
                        )
                    except KeyError:
                        raise
                    title_operation = next(
                        (
                            item
                            for item in self.proposal.operations
                            if item.action
                            in {ProposalAction.APPLY, ProposalAction.EDIT}
                            and item.change.target_database
                            == change.target_database
                            and item.change.entity_key == change.entity_key
                            and item.change.property_name
                            == definition.title_property
                        ),
                        None,
                    )
                    title_outbox = (
                        self.database.operation_outbox_state(
                            title_operation.change.operation_id
                        )
                        if title_operation is not None
                        else None
                    )
                    if (
                        title_operation is None
                        or title_outbox is None
                        or title_outbox["proposal_id"]
                        != self.proposal.proposal_id
                        or int(title_outbox["revision"])
                        != self.proposal.revision
                        or title_outbox["status"] == "done"
                        or started_receipt.proposal_digest
                        != self.proposal.digest
                        or started_receipt.notion_binding_digest
                        != sha256_json(self.proposal.notion_binding)
                        or not self.database.nonce_used(started_receipt.nonce)
                    ):
                        raise
                return page
            return self._find_page(change.target_database, change.entity_key)
        definition, data_source_id = self._definition(change.target_database)
        target = _validated_correction_target(
            self.proposal,
            operation,
            data_source_id,
        )
        page = self.gateway.get_page(str(target["page_id"]))
        if not isinstance(page, Mapping):
            raise NotionSchemaError(
                "Independent correction target page could not be read"
            )
        _assert_bound_correction_page(page, target)
        _assert_exact_page_binding(
            page,
            page_id=str(target["page_id"]),
            data_source_id=data_source_id,
            title_property=definition.title_property,
            match_value=_correction_match_value(self.proposal),
            label="Independent correction target",
        )
        return page

    def _resolve_relations(
        self,
        database_name: str,
        property_name: str,
        value: JsonValue,
        *,
        allow_planned: bool = False,
    ) -> list[str]:
        definition, _ = self._definition(database_name)
        target_database = definition.relations.get(property_name)
        if not target_database:
            raise NotionSchemaError(f"Missing relation target for {database_name}.{property_name}")
        logical_keys = value if isinstance(value, list) else ([] if value is None else [value])
        page_ids: list[str] = []
        for logical_key in logical_keys:
            key = str(logical_key)
            mapping = self._mapping(target_database, key)
            if mapping:
                page_id = mapping["notion_page_id"]
                target_definition, target_data_source_id = self._definition(
                    target_database
                )
                cache_key = (
                    target_database,
                    key,
                    page_id,
                    mapping["match_value"],
                )
                if cache_key not in self._relation_validation_cache:
                    page = self.gateway.get_page(page_id)
                    if not isinstance(page, Mapping):
                        raise NotionSchemaError(
                            "Mapped relation target page could not be read"
                        )
                    _assert_exact_page_binding(
                        page,
                        page_id=page_id,
                        data_source_id=target_data_source_id,
                        title_property=target_definition.title_property,
                        match_value=mapping["match_value"],
                        label="Mapped relation target",
                    )
                    self._relation_validation_cache[cache_key] = page_id
                page_ids.append(page_id)
                continue
            page = self._find_page(target_database, key)
            if not page:
                target_definition, _ = self._definition(target_database)
                planned_title = self._values.get(
                    (target_database, key, target_definition.title_property)
                )
                if allow_planned and planned_title not in (None, ""):
                    self._planned_relation_misses.add((target_database, key))
                    continue
                raise NotionSchemaError(
                    f"Related entity does not exist: {target_database}/{key}"
                )
            page_id = str(page["id"])
            self.database.save_entity_mapping(
                target_database, key, page_id, self._match_value(target_database, key)
            )
            self._mapping_cache[(target_database, key)] = {
                "database_name": target_database,
                "entity_key": key,
                "notion_page_id": page_id,
                "match_value": self._match_value(target_database, key),
            }
            self._planned_relation_misses.discard((target_database, key))
            page_ids.append(page_id)
        return page_ids

    def inspect_batch(
        self,
        operations: Sequence[ProposalOperation],
    ) -> dict[str, WritePreconditionState]:
        items = self._validated_operation_batch(operations)
        first_change = items[0].change
        self._assert_destination_binding(first_change.target_database)
        definition, _ = self._definition(first_change.target_database)
        relation_ids_by_operation: dict[str, list[str] | None] = {}
        for operation in items:
            change = operation.change
            property_type = definition.properties.get(change.property_name)
            if not property_type:
                raise NotionSchemaError(
                    f"Unknown property: {change.target_database}.{change.property_name}"
                )
            self._validate_remote_schema(
                change.target_database,
                change.property_name,
                operation.approved_value,
            )
            relation_ids = None
            if property_type == "relation":
                relation_ids = self._resolve_relations(
                    change.target_database,
                    change.property_name,
                    operation.approved_value,
                    allow_planned=True,
                )
            encode_property(
                operation.approved_value,
                property_type,
                relation_ids,
            )
            relation_ids_by_operation[change.operation_id] = relation_ids
        page = self._operation_page(items[0])
        page_id = str(page["id"]) if page else None
        last_edited_time = (
            str(page["last_edited_time"])
            if page and page.get("last_edited_time") is not None
            else None
        )
        properties = page.get("properties") if page else None
        states: dict[str, WritePreconditionState] = {}
        for operation in items:
            change = operation.change
            property_type = definition.properties[change.property_name]
            raw_property = (
                properties.get(change.property_name)
                if isinstance(properties, Mapping)
                else None
            )
            self._intents[change.operation_id] = NotionPrecondition(
                page_id=page_id,
                last_edited_time=last_edited_time,
                property_name=change.property_name,
                property_value=deepcopy(raw_property),
            )
            if not page:
                if (
                    change.property_name != definition.title_property
                    and (
                        change.target_database,
                        change.entity_key,
                        definition.title_property,
                    )
                    not in self._values
                ):
                    raise NotionSchemaError(
                        "A new Notion page cannot be created without an approved title"
                    )
                states[change.operation_id] = (
                    WritePreconditionState.PENDING
                    if change.current_value is None
                    else WritePreconditionState.CONFLICT
                )
                continue
            current = decode_property(raw_property, property_type)
            if current == change.current_value:
                states[change.operation_id] = WritePreconditionState.PENDING
                continue
            approved_value: JsonValue = operation.approved_value
            planned_relation = False
            if property_type == "relation":
                approved_value = (
                    relation_ids_by_operation[change.operation_id] or []
                )
                target_database = definition.relations[change.property_name]
                logical_keys = (
                    operation.approved_value
                    if isinstance(operation.approved_value, list)
                    else (
                        []
                        if operation.approved_value is None
                        else [operation.approved_value]
                    )
                )
                planned_relation = any(
                    (target_database, str(key)) in self._planned_relation_misses
                    for key in logical_keys
                )
            states[change.operation_id] = (
                WritePreconditionState.ALREADY_APPLIED
                if not planned_relation and current == approved_value
                else WritePreconditionState.CONFLICT
            )
        return states

    def inspect(self, operation: ProposalOperation) -> WritePreconditionState:
        return self.inspect_batch([operation])[operation.change.operation_id]

    def preflight(self, operation: ProposalOperation) -> bool:
        return self.inspect(operation) is WritePreconditionState.PENDING

    def apply_batch(
        self,
        operations: Sequence[ProposalOperation],
        receipt: ApprovalReceipt,
    ) -> str:
        items = self._validated_operation_batch(operations)
        # A relation target can be moved, archived, or retitled between page
        # mutations. Reuse validation only within this single atomic Notion
        # request; every later page mutation must re-read the exact target.
        self._relation_validation_cache.clear()
        first_change = items[0].change
        definition, data_source_id = self._definition(
            first_change.target_database
        )
        mutated = self._mutated_pages.get(
            (first_change.target_database, first_change.entity_key)
        )
        if mutated is not None:
            mutated_page_id, created_in_command = mutated
            for operation in items:
                operation_id = operation.change.operation_id
                intent = self._intents.get(operation_id)
                if intent is None:
                    raise NotionSchemaError(
                        "Notion batch has no global preflight intent"
                    )
                if intent.page_id is None:
                    if (
                        not created_in_command
                        or operation.change.current_value is not None
                        or intent.property_value is not None
                    ):
                        raise NotionSchemaError(
                            "A planned Notion page cannot be rebound safely"
                        )
                elif intent.page_id != mutated_page_id:
                    raise NotionSchemaError(
                        "Notion page identity changed between approved batches"
                    )
                self._intents[operation_id] = NotionPrecondition(
                    page_id=mutated_page_id,
                    last_edited_time=None,
                    property_name=intent.property_name,
                    property_value=deepcopy(intent.property_value),
                )
        mutations: list[NotionPropertyMutation] = []
        for operation in items:
            change = operation.change
            intent = self._intents.get(change.operation_id)
            if intent is None:
                raise NotionSchemaError(
                    "Notion batch has no global preflight intent"
                )
            property_type = definition.properties.get(change.property_name)
            if not property_type:
                raise NotionSchemaError(
                    f"Unknown property: {change.target_database}.{change.property_name}"
                )
            relation_ids = None
            if property_type == "relation":
                relation_ids = self._resolve_relations(
                    change.target_database,
                    change.property_name,
                    operation.approved_value,
                )
            encoded = encode_property(
                operation.approved_value,
                property_type,
                relation_ids,
            )
            mutations.append(
                NotionPropertyMutation(
                    operation_id=change.operation_id,
                    property_name=change.property_name,
                    property_value=encoded,
                    precondition=intent,
                )
            )
        self._assert_destination_binding(first_change.target_database)
        result = self.gateway.upsert_page_batch(
            data_source_id,
            NotionMatch(definition.title_property, "title"),
            self._match_value(
                first_change.target_database,
                first_change.entity_key,
            ),
            mutations,
            approval=receipt,
        )
        previous = self._mutated_pages.get(
            (first_change.target_database, first_change.entity_key)
        )
        self._mutated_pages[
            (first_change.target_database, first_change.entity_key)
        ] = (
            result.page_id,
            result.created or (previous[1] if previous is not None else False),
        )
        return result.page_id

    def complete_batch(
        self,
        operations: Sequence[ProposalOperation],
        page_id: str | None = None,
    ) -> None:
        """Atomically persist remote acknowledgement, outbox, and title mapping."""

        items = self._validated_operation_batch(operations)
        first_change = items[0].change
        definition, _ = self._definition(first_change.target_database)
        if page_id is None:
            page_ids = {
                self._intents[item.change.operation_id].page_id
                for item in items
                if item.change.operation_id in self._intents
            }
            if len(page_ids) != 1 or None in page_ids:
                raise NotionSchemaError(
                    "Reconciled Notion batch has no exact page identity"
                )
            page_id = str(next(iter(page_ids)))
        if not page_id.strip():
            raise NotionSchemaError("Notion batch returned an empty page identity")
        match_value = self._match_value(
            first_change.target_database,
            first_change.entity_key,
        )
        for operation in items:
            if (
                operation.change.property_name == definition.title_property
                and operation.approved_value not in (None, "")
            ):
                match_value = str(operation.approved_value)
                break
        mapping = {
            "database_name": first_change.target_database,
            "entity_key": first_change.entity_key,
            "notion_page_id": page_id,
            "match_value": match_value,
        }
        self.database.mark_outbox_batch_done(
            self.proposal.proposal_id,
            self.proposal.revision,
            [item.change.operation_id for item in items],
            entity_mapping=mapping,
            allow_reconciliation=(
                self.proposal.status is ProposalStatus.FAILED
            ),
        )
        self._mapping_cache[
            (first_change.target_database, first_change.entity_key)
        ] = mapping
        self._planned_relation_misses.discard(
            (first_change.target_database, first_change.entity_key)
        )

    def apply(self, operation: ProposalOperation, receipt: ApprovalReceipt) -> str:
        if not self.preflight(operation):
            raise NotionSchemaError(
                "Notion value changed after the global preflight check"
            )
        change = operation.change
        definition, data_source_id = self._definition(change.target_database)
        property_type = definition.properties.get(change.property_name)
        if not property_type:
            raise NotionSchemaError(
                f"Unknown property: {change.target_database}.{change.property_name}"
            )
        relation_ids = None
        if property_type == "relation":
            relation_ids = self._resolve_relations(
                change.target_database, change.property_name, operation.approved_value
            )
        self._assert_destination_binding(change.target_database)
        encoded = encode_property(operation.approved_value, property_type, relation_ids)
        match_value = self._match_value(change.target_database, change.entity_key)
        result = self.gateway.upsert_page(
            data_source_id,
            NotionMatch(definition.title_property, "title"),
            match_value,
            {change.property_name: encoded},
            operation_id=change.operation_id,
            approval=receipt,
            precondition=self._intents[change.operation_id],
        )
        stored_match_value = (
            str(operation.approved_value)
            if change.property_name == definition.title_property
            and operation.approved_value not in (None, "")
            else match_value
        )
        self.database.save_entity_mapping(
            change.target_database,
            change.entity_key,
            result.page_id,
            stored_match_value,
        )
        return result.page_id
