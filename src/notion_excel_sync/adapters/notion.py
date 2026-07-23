from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from notion_excel_sync.models import (
    SCHEMA_APPROVAL_PURPOSE,
    ApprovalReceipt,
    JsonValue,
    SchemaApprovalReceipt,
)


NOTION_BASE_URL = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2026-03-11"
DATA_SOURCE_API_VERSION = "2025-09-03"


class NotionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        mutation_outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.mutation_outcome_unknown = mutation_outcome_unknown


class ApprovalRequiredError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class NotionDatabaseCreateResult:
    database_id: str
    data_source_id: str


class NotionTokenProvider(Protocol):
    def __call__(self) -> str: ...


class NotionUrlOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> Any: ...


class ApprovalVerifier(Protocol):
    def __call__(
        self,
        receipt: ApprovalReceipt,
        operation_id: str,
        data_source_id: str,
        match: NotionMatch,
        entity_key: JsonValue,
        properties: Mapping[str, Any],
        precondition: NotionPrecondition | None,
    ) -> bool: ...


class SchemaApprovalVerifier(Protocol):
    def __call__(
        self,
        receipt: SchemaApprovalReceipt,
        operation_id: str,
        expected_body: Mapping[str, Any],
        precondition: Mapping[str, Any],
        notion_version: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class NotionMatch:
    property_name: str
    property_type: str = "rich_text"

    def __post_init__(self) -> None:
        if not self.property_name.strip():
            raise ValueError("match property name must not be empty")
        allowed = {
            "title",
            "rich_text",
            "number",
            "select",
            "url",
            "email",
            "phone_number",
        }
        if self.property_type not in allowed:
            raise ValueError(f"Unsupported Notion match property type: {self.property_type}")


@dataclass(frozen=True, slots=True)
class NotionPrecondition:
    page_id: str | None
    last_edited_time: str | None
    property_name: str
    property_value: Any


@dataclass(frozen=True, slots=True)
class NotionUpsertResult:
    page_id: str
    created: bool
    page: Mapping[str, Any]


class NotionGateway(Protocol):
    def get_page(self, page_id: str) -> Mapping[str, Any]: ...

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]: ...

    def query_database(
        self,
        database_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]: ...

    def find_page(
        self, data_source_id: str, match: NotionMatch, entity_key: JsonValue
    ) -> Mapping[str, Any] | None: ...

    def upsert_page(
        self,
        data_source_id: str,
        match: NotionMatch,
        entity_key: JsonValue,
        properties: Mapping[str, Any],
        *,
        operation_id: str,
        approval: ApprovalReceipt,
        precondition: NotionPrecondition | None = None,
    ) -> NotionUpsertResult: ...


def _require_approval(
    verifier: ApprovalVerifier,
    receipt: ApprovalReceipt,
    operation_id: str,
    data_source_id: str,
    match: NotionMatch,
    entity_key: JsonValue,
    properties: Mapping[str, Any],
    precondition: NotionPrecondition | None,
    *,
    now: Callable[[], datetime],
) -> None:
    if not operation_id.strip():
        raise ApprovalRequiredError("A non-empty approved operation_id is required")
    if not isinstance(receipt, ApprovalReceipt):
        raise ApprovalRequiredError("A signed ApprovalReceipt is required")
    if not receipt.signature or not receipt.proposal_digest or not receipt.nonce:
        raise ApprovalRequiredError("ApprovalReceipt is incomplete")
    current = now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    expires_at = receipt.expires_at
    issued_at = receipt.issued_at
    if expires_at.tzinfo is None or issued_at.tzinfo is None:
        raise ApprovalRequiredError("ApprovalReceipt timestamps must be timezone-aware")
    if expires_at <= issued_at:
        raise ApprovalRequiredError("ApprovalReceipt has an invalid validity window")
    receipt_expired = expires_at <= current.astimezone(expires_at.tzinfo)
    permits_started_reconciliation = (
        getattr(verifier, "allows_expired_started_receipt", False) is True
    )
    if receipt_expired and not permits_started_reconciliation:
        raise ApprovalRequiredError("ApprovalReceipt has expired")
    try:
        verified = verifier(
            receipt,
            operation_id,
            data_source_id,
            match,
            entity_key,
            properties,
            precondition,
        )
    except Exception as exc:
        raise ApprovalRequiredError("Approval verifier rejected the write") from exc
    if verified is not True:
        raise ApprovalRequiredError("Operation is not covered by the verified approval")


def _require_schema_approval(
    verifier: SchemaApprovalVerifier | None,
    receipt: SchemaApprovalReceipt,
    operation_id: str,
    expected_body: Mapping[str, Any],
    precondition: Mapping[str, Any],
    notion_version: str,
    *,
    now: Callable[[], datetime],
) -> None:
    """Fail closed before the schema mutation can reach the HTTP opener."""

    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ApprovalRequiredError("A non-empty approved schema operation_id is required")
    if not isinstance(receipt, SchemaApprovalReceipt):
        raise ApprovalRequiredError("A signed SchemaApprovalReceipt is required")
    required_text = (
        receipt.schema_proposal_id,
        receipt.proposal_digest,
        receipt.precondition_digest,
        receipt.telegram_user_id,
        receipt.chat_id,
        receipt.telegram_message_id,
        receipt.command_hash,
        receipt.nonce,
        receipt.signature,
    )
    if any(not isinstance(value, str) or not value.strip() for value in required_text):
        raise ApprovalRequiredError("SchemaApprovalReceipt is incomplete")
    if not isinstance(receipt.thread_id, str):
        raise ApprovalRequiredError("SchemaApprovalReceipt is incomplete")
    if (
        receipt.purpose != SCHEMA_APPROVAL_PURPOSE
        or isinstance(receipt.revision, bool)
        or not isinstance(receipt.revision, int)
        or receipt.revision < 1
        or isinstance(receipt.telegram_update_id, bool)
        or not isinstance(receipt.telegram_update_id, int)
        or receipt.telegram_update_id < 0
    ):
        raise ApprovalRequiredError("SchemaApprovalReceipt is incomplete")
    current = now()
    if not isinstance(current, datetime):
        raise ApprovalRequiredError("Schema approval clock returned an invalid time")
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if (
        not isinstance(receipt.expires_at, datetime)
        or not isinstance(receipt.issued_at, datetime)
        or receipt.expires_at.tzinfo is None
        or receipt.issued_at.tzinfo is None
    ):
        raise ApprovalRequiredError("SchemaApprovalReceipt timestamps must be timezone-aware")
    if receipt.expires_at <= receipt.issued_at:
        raise ApprovalRequiredError("SchemaApprovalReceipt has an invalid validity window")
    receipt_expired = receipt.expires_at <= current.astimezone(
        receipt.expires_at.tzinfo
    )
    permits_started_operation = (
        verifier is not None
        and getattr(verifier, "allows_expired_started_receipt", False) is True
    )
    if receipt_expired and not permits_started_operation:
        raise ApprovalRequiredError("SchemaApprovalReceipt has expired")
    if verifier is None:
        raise ApprovalRequiredError("No schema approval verifier is configured")
    try:
        verified = verifier(
            receipt,
            operation_id,
            copy.deepcopy(dict(expected_body)),
            copy.deepcopy(dict(precondition)),
            notion_version,
        )
    except Exception as exc:
        raise ApprovalRequiredError("Schema approval verifier rejected the write") from exc
    if verified is not True:
        raise ApprovalRequiredError("Schema operation is not covered by the verified approval")


def _database_create_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the minimum 2026-03-11 create-database contract.

    The endpoint creates a database container and its first data source. Keeping
    the schema under ``initial_data_source.properties`` also prevents an
    accidental fallback to the deprecated pre-2025 request shape.
    """

    if not isinstance(body, Mapping):
        raise TypeError("Notion database create body must be an object")
    prepared = copy.deepcopy(dict(body))
    parent = prepared.get("parent")
    if (
        not isinstance(parent, Mapping)
        or parent.get("type") != "page_id"
        or not isinstance(parent.get("page_id"), str)
        or not str(parent["page_id"]).strip()
    ):
        raise ValueError("Notion database parent must contain a non-empty page_id")
    if "properties" in prepared:
        raise ValueError("Database properties must be nested under initial_data_source")
    initial_data_source = prepared.get("initial_data_source")
    if not isinstance(initial_data_source, Mapping):
        raise ValueError("Notion database create body requires initial_data_source")
    properties = initial_data_source.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        raise ValueError("Notion initial data source requires a non-empty properties schema")
    title_properties = [
        name
        for name, definition in properties.items()
        if isinstance(definition, Mapping) and isinstance(definition.get("title"), Mapping)
    ]
    if len(title_properties) != 1:
        raise ValueError("Notion initial data source requires exactly one title property")
    return prepared


_SAFE_ERROR_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)


def _safe_error_atom(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    return value if all(character in _SAFE_ERROR_CHARS for character in value) else None


def _safe_schema_error_message(
    *,
    status: int | None,
    code: str | None,
    request_id: str | None,
) -> str:
    fields: list[str] = []
    if status is not None:
        fields.append(f"status={status}")
    if code is not None:
        fields.append(f"code={code}")
    if request_id is not None:
        fields.append(f"request_id={request_id}")
    detail = ", ".join(fields) if fields else "network_error"
    return f"Notion schema create failed ({detail})"


def _match_filter(match: NotionMatch, entity_key: JsonValue) -> dict[str, Any]:
    return {
        "property": match.property_name,
        match.property_type: {"equals": entity_key},
    }


def _match_property_value(match: NotionMatch, entity_key: JsonValue) -> dict[str, Any]:
    if match.property_type in {"title", "rich_text"}:
        return {
            match.property_type: [
                {"type": "text", "text": {"content": str(entity_key)}}
            ]
        }
    if match.property_type == "select":
        return {"select": {"name": str(entity_key)}}
    return {match.property_type: entity_key}


def _plain_property_value(property_value: Any, property_type: str) -> JsonValue:
    if not isinstance(property_value, Mapping):
        return None
    if property_type in {"title", "rich_text"}:
        rows = property_value.get(property_type, [])
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return None
        text: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            plain = row.get("plain_text")
            if plain is None and isinstance(row.get("text"), Mapping):
                plain = row["text"].get("content")
            if plain is not None:
                text.append(str(plain))
        return "".join(text)
    if property_type == "select":
        selected = property_value.get("select")
        return selected.get("name") if isinstance(selected, Mapping) else None
    return property_value.get(property_type)


def _prepare_properties(
    properties: Mapping[str, Any], match: NotionMatch, entity_key: JsonValue
) -> dict[str, Any]:
    if entity_key is None or (isinstance(entity_key, str) and not entity_key.strip()):
        raise ValueError("entity_key must not be empty")
    clean_properties = copy.deepcopy(dict(properties))
    if match.property_name in clean_properties:
        supplied_key = _plain_property_value(
            clean_properties[match.property_name], match.property_type
        )
        if supplied_key != entity_key:
            raise ValueError("The supplied match property does not equal entity_key")
    else:
        clean_properties[match.property_name] = _match_property_value(match, entity_key)
    return clean_properties


def _prepare_update_properties(
    properties: Mapping[str, Any], match: NotionMatch, entity_key: JsonValue
) -> dict[str, Any]:
    del match, entity_key
    return copy.deepcopy(dict(properties))


def _check_precondition(
    current: Mapping[str, Any] | None,
    precondition: NotionPrecondition | None,
) -> None:
    if precondition is None:
        return
    current_page_id = str(current["id"]) if current else None
    if current_page_id != precondition.page_id:
        raise NotionError("Notion page identity changed before mutation", status=409)
    if current is None:
        return
    if (
        precondition.last_edited_time is not None
        and current.get("last_edited_time") != precondition.last_edited_time
    ):
        raise NotionError("Notion page changed before mutation", status=409)
    current_property = current.get("properties", {}).get(
        precondition.property_name
    )
    if current_property != precondition.property_value:
        raise NotionError("Notion property changed before mutation", status=409)


class InMemoryNotionGateway:
    """Approval-gated Notion substitute for tests and dry-run integration."""

    def __init__(
        self,
        approval_verifier: ApprovalVerifier,
        *,
        initial_pages: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._approval_verifier = approval_verifier
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._pages: dict[str, dict[str, Any]] = {}
        self._data_source_pages: dict[str, list[str]] = {}
        for data_source_id, pages in (initial_pages or {}).items():
            for raw_page in pages:
                page = copy.deepcopy(dict(raw_page))
                page_id = str(page.get("id") or uuid4())
                page["id"] = page_id
                page.setdefault(
                    "parent",
                    {"type": "data_source_id", "data_source_id": data_source_id},
                )
                page.setdefault("properties", {})
                self._pages[page_id] = page
                self._data_source_pages.setdefault(data_source_id, []).append(page_id)

    def get_page(self, page_id: str) -> Mapping[str, Any]:
        with self._lock:
            try:
                return copy.deepcopy(self._pages[page_id])
            except KeyError as exc:
                raise NotionError(f"Notion page {page_id!r} does not exist", status=404) from exc

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        with self._lock:
            pages = [
                copy.deepcopy(self._pages[page_id])
                for page_id in self._data_source_pages.get(data_source_id, [])
            ]
        if not filter_body:
            return pages
        try:
            property_name = str(filter_body["property"])
            property_types = [key for key in filter_body if key != "property"]
            property_type = property_types[0]
            expected = filter_body[property_type]["equals"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Unsupported in-memory Notion filter") from exc
        return [
            page
            for page in pages
            if _plain_property_value(page.get("properties", {}).get(property_name), property_type)
            == expected
        ]

    def query_database(
        self,
        database_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        """Compatibility alias for legacy single-source database configurations."""

        return self.query_data_source(
            database_id, filter_body=filter_body, page_size=page_size
        )

    def find_page(
        self, data_source_id: str, match: NotionMatch, entity_key: JsonValue
    ) -> Mapping[str, Any] | None:
        matches = self.query_data_source(
            data_source_id,
            filter_body=_match_filter(match, entity_key),
            page_size=2,
        )
        if len(matches) > 1:
            raise NotionError(
                f"Multiple pages match {match.property_name}={entity_key!r}; upsert is unsafe"
            )
        return matches[0] if matches else None

    def upsert_page(
        self,
        data_source_id: str,
        match: NotionMatch,
        entity_key: JsonValue,
        properties: Mapping[str, Any],
        *,
        operation_id: str,
        approval: ApprovalReceipt,
        precondition: NotionPrecondition | None = None,
    ) -> NotionUpsertResult:
        _require_approval(
            self._approval_verifier,
            approval,
            operation_id,
            data_source_id,
            match,
            entity_key,
            properties,
            precondition,
            now=self._now,
        )
        with self._lock:
            current = self.find_page(data_source_id, match, entity_key)
            _check_precondition(current, precondition)
            if current is None:
                if match.property_name not in properties:
                    raise ApprovalRequiredError(
                        "Creating a page requires an explicitly approved title property"
                    )
                clean_properties = _prepare_properties(properties, match, entity_key)
                page_id = str(uuid4())
                page = {
                    "object": "page",
                    "id": page_id,
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": data_source_id,
                    },
                    "properties": clean_properties,
                }
                self._pages[page_id] = page
                self._data_source_pages.setdefault(data_source_id, []).append(page_id)
                return NotionUpsertResult(page_id, True, copy.deepcopy(page))
            clean_properties = _prepare_update_properties(
                properties, match, entity_key
            )
            page_id = str(current["id"])
            stored = self._pages[page_id]
            stored.setdefault("properties", {}).update(clean_properties)
            return NotionUpsertResult(page_id, False, copy.deepcopy(stored))


class HttpNotionGateway:
    """Minimal Notion HTTP gateway whose only mutation entrypoint is approval-gated."""

    def __init__(
        self,
        access_token: str | NotionTokenProvider,
        approval_verifier: ApprovalVerifier,
        *,
        schema_approval_verifier: SchemaApprovalVerifier | None = None,
        notion_version: str = DEFAULT_NOTION_VERSION,
        base_url: str = NOTION_BASE_URL,
        timeout: float = 30.0,
        opener: NotionUrlOpener | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._access_token = access_token
        self._approval_verifier = approval_verifier
        self._schema_approval_verifier = schema_approval_verifier
        self._notion_version = notion_version
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = opener or urlopen
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def uses_data_sources(self) -> bool:
        return self._notion_version >= DATA_SOURCE_API_VERSION

    def _token(self) -> str:
        token = self._access_token() if callable(self._access_token) else self._access_token
        token = token.strip()
        if not token:
            raise NotionError("Notion API token is empty")
        return token

    def _request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        data = None
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Notion-Version": self._notion_version,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}/{path.lstrip('/')}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise NotionError(
                f"Notion API {method} failed ({exc.code}): {detail}", status=exc.code
            ) from exc
        except URLError as exc:
            raise NotionError(f"Notion API {method} failed: {exc.reason}") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotionError("Notion API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise NotionError("Notion API returned a non-object JSON response")
        return result

    def _schema_create_request(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send the single allowed schema mutation with non-sensitive errors."""

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._base_url}/databases",
            data=data,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Notion-Version": self._notion_version,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response_headers: Any = None
        try:
            with self._opener(request, timeout=self._timeout) as response:
                response_headers = getattr(response, "headers", None)
                raw = response.read()
        except HTTPError as exc:
            raw_error = b""
            try:
                raw_error = exc.read(4096)
            except Exception:
                pass
            code: str | None = None
            try:
                error_payload = json.loads(raw_error.decode("utf-8"))
                if isinstance(error_payload, Mapping):
                    code = _safe_error_atom(error_payload.get("code"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            headers = getattr(exc, "headers", None)
            request_id = _safe_error_atom(
                headers.get("x-request-id") if headers is not None else None
            )
            status = int(exc.code)
            raise NotionError(
                _safe_schema_error_message(
                    status=status,
                    code=code,
                    request_id=request_id,
                ),
                status=status,
                code=code,
                request_id=request_id,
                mutation_outcome_unknown=status >= 500,
            ) from exc
        except OSError as exc:
            raise NotionError(
                _safe_schema_error_message(
                    status=None,
                    code="network_error",
                    request_id=None,
                ),
                code="network_error",
                mutation_outcome_unknown=True,
            ) from exc
        request_id = _safe_error_atom(
            response_headers.get("x-request-id")
            if response_headers is not None
            else None
        )
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotionError(
                _safe_schema_error_message(
                    status=None,
                    code="invalid_response",
                    request_id=request_id,
                ),
                code="invalid_response",
                request_id=request_id,
                mutation_outcome_unknown=True,
            ) from exc
        if not isinstance(result, dict):
            raise NotionError(
                _safe_schema_error_message(
                    status=None,
                    code="invalid_response",
                    request_id=request_id,
                ),
                code="invalid_response",
                request_id=request_id,
                mutation_outcome_unknown=True,
            )
        return result

    def _schema_database_lookup(self, database_id: str) -> Mapping[str, Any]:
        """Read back a minimally returned create result without leaking errors."""

        try:
            return self._request("GET", f"databases/{quote(database_id, safe='')}")
        except Exception as exc:
            status = exc.status if isinstance(exc, NotionError) else None
            raise NotionError(
                _safe_schema_error_message(
                    status=status,
                    code="verification_failed",
                    request_id=None,
                ),
                status=status,
                code="verification_failed",
                mutation_outcome_unknown=True,
            ) from None

    def get_self(self) -> Mapping[str, Any]:
        """Retrieve the bot user associated with the configured token."""

        return self._request("GET", "users/me")

    def list_block_children(
        self,
        block_id: str,
        *,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        """Return every direct child block, preserving Notion cursor opacity."""

        if not block_id.strip():
            raise ValueError("block_id must not be empty")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        children: list[Mapping[str, Any]] = []
        while True:
            query: dict[str, str | int] = {"page_size": page_size}
            if cursor is not None:
                query["start_cursor"] = cursor
            path = (
                f"blocks/{quote(block_id, safe='')}/children?"
                f"{urlencode(query)}"
            )
            payload = self._request("GET", path)
            results = payload.get("results")
            if not isinstance(results, list):
                raise NotionError("Notion block children returned an invalid results field")
            children.extend(item for item in results if isinstance(item, Mapping))
            if not payload.get("has_more"):
                return children
            next_cursor = payload.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise NotionError("Notion block children is missing its next cursor")
            if next_cursor in seen_cursors:
                raise NotionError("Notion block children repeated its next cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def create_database(
        self,
        expected_body: Mapping[str, Any],
        *,
        operation_id: str,
        approval: SchemaApprovalReceipt,
        precondition: Mapping[str, Any],
    ) -> NotionDatabaseCreateResult:
        """Create a database and its initial data source after exact approval.

        There is intentionally no generic public schema mutation method. This
        fixed endpoint is the only path to ``POST /v1/databases`` and the opener
        remains unreachable until the dedicated verifier returns exactly true.
        """

        if not isinstance(precondition, Mapping):
            raise TypeError("Notion schema precondition must be an object")
        body = _database_create_body(expected_body)
        _require_schema_approval(
            self._schema_approval_verifier,
            approval,
            operation_id,
            body,
            precondition,
            self._notion_version,
            now=self._now,
        )
        created = self._schema_create_request(body)
        database_id = created.get("id")
        if (
            created.get("object") != "database"
            or not isinstance(database_id, str)
            or not database_id.strip()
        ):
            raise NotionError(
                _safe_schema_error_message(
                    status=None,
                    code="invalid_response",
                    request_id=None,
                ),
                code="invalid_response",
                mutation_outcome_unknown=True,
        )
        if "data_sources" not in created:
            resolved = self._schema_database_lookup(database_id)
            if resolved.get("object") != "database" or resolved.get("id") != database_id:
                raise NotionError(
                    _safe_schema_error_message(
                        status=None,
                        code="invalid_response",
                        request_id=None,
                    ),
                    code="invalid_response",
                    mutation_outcome_unknown=True,
                )
            data_sources = resolved.get("data_sources")
        else:
            data_sources = created.get("data_sources")
        if (
            not isinstance(data_sources, list)
            or len(data_sources) != 1
            or not isinstance(data_sources[0], Mapping)
            or not isinstance(data_sources[0].get("id"), str)
            or not str(data_sources[0]["id"])
        ):
            raise NotionError(
                _safe_schema_error_message(
                    status=None,
                    code="invalid_response",
                    request_id=None,
                ),
                code="invalid_response",
                mutation_outcome_unknown=True,
            )
        return NotionDatabaseCreateResult(
            database_id=database_id,
            data_source_id=str(data_sources[0]["id"]),
        )

    def get_page(self, page_id: str) -> Mapping[str, Any]:
        return self._request("GET", f"pages/{quote(page_id, safe='')}")

    def get_database(self, database_id: str) -> Mapping[str, Any]:
        """Retrieve a database container, including its child data source IDs."""

        return self._request("GET", f"databases/{quote(database_id, safe='')}")

    def get_data_source(self, data_source_id: str) -> Mapping[str, Any]:
        return self._request("GET", f"data_sources/{quote(data_source_id, safe='')}")

    def _query_collection(
        self,
        collection: str,
        collection_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        path = f"{collection}/{quote(collection_id, safe='')}/query"
        cursor: str | None = None
        pages: list[Mapping[str, Any]] = []
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if filter_body:
                body["filter"] = copy.deepcopy(dict(filter_body))
            if cursor:
                body["start_cursor"] = cursor
            payload = self._request("POST", path, body)
            results = payload.get("results", [])
            if not isinstance(results, list):
                raise NotionError("Notion query returned an invalid results field")
            pages.extend(row for row in results if isinstance(row, dict))
            if not payload.get("has_more"):
                break
            next_cursor = payload.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise NotionError("Notion query is missing its next cursor")
            cursor = next_cursor
        return pages

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        return self._query_collection(
            "data_sources",
            data_source_id,
            filter_body=filter_body,
            page_size=page_size,
        )

    def query_database(
        self,
        database_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        """Query the deprecated pre-2025 database API for explicit legacy configs."""

        return self._query_collection(
            "databases",
            database_id,
            filter_body=filter_body,
            page_size=page_size,
        )

    def find_page(
        self, data_source_id: str, match: NotionMatch, entity_key: JsonValue
    ) -> Mapping[str, Any] | None:
        query = self.query_data_source if self.uses_data_sources else self.query_database
        pages = query(data_source_id, filter_body=_match_filter(match, entity_key), page_size=2)
        if len(pages) > 1:
            raise NotionError(
                f"Multiple pages match {match.property_name}={entity_key!r}; upsert is unsafe"
            )
        return pages[0] if pages else None

    def upsert_page(
        self,
        data_source_id: str,
        match: NotionMatch,
        entity_key: JsonValue,
        properties: Mapping[str, Any],
        *,
        operation_id: str,
        approval: ApprovalReceipt,
        precondition: NotionPrecondition | None = None,
    ) -> NotionUpsertResult:
        _require_approval(
            self._approval_verifier,
            approval,
            operation_id,
            data_source_id,
            match,
            entity_key,
            properties,
            precondition,
            now=self._now,
        )
        current = self.find_page(data_source_id, match, entity_key)
        _check_precondition(current, precondition)
        if current is None:
            if match.property_name not in properties:
                raise ApprovalRequiredError(
                    "Creating a page requires an explicitly approved title property"
                )
            clean_properties = _prepare_properties(properties, match, entity_key)
            parent = (
                {"type": "data_source_id", "data_source_id": data_source_id}
                if self.uses_data_sources
                else {"database_id": data_source_id}
            )
            page = self._request(
                "POST",
                "pages",
                {
                    "parent": parent,
                    "properties": clean_properties,
                },
            )
            return NotionUpsertResult(str(page["id"]), True, page)
        clean_properties = _prepare_update_properties(
            properties, match, entity_key
        )
        page_id = str(current["id"])
        page = self._request(
            "PATCH",
            f"pages/{quote(page_id, safe='')}",
            {"properties": clean_properties},
        )
        return NotionUpsertResult(page_id, False, page)
