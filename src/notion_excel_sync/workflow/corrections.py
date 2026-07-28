from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any

from notion_excel_sync.models import JsonValue, USER_CORRECTION_PURPOSE


CORRECTION_COMMAND = "/nx_correct"
MAX_CORRECTION_JSON_CHARS = 4096
MAX_CORRECTION_JSON_DEPTH = 20
MAX_CORRECTION_JSON_ITEMS = 512

_REQUIRED_KEYS = frozenset({"database", "entity_key", "property", "value", "reason"})
_OPTIONAL_KEYS = frozenset({"case_number"})
_ALLOWED_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS

_MAX_DATABASE_CHARS = 128
_MAX_ENTITY_KEY_CHARS = 512
_MAX_PROPERTY_CHARS = 128
_MAX_REASON_CHARS = 2_000
_MAX_CASE_NUMBER_CHARS = 256
_MAX_VALUE_STRING_CHARS = 2_048
_MAX_VALUE_KEY_CHARS = 128


class CorrectionRequestError(ValueError):
    """Raised when an independent correction request is not exact and bounded."""


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _strict_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CorrectionRequestError(f"{label} must be a string")
    if not value or not value.strip():
        raise CorrectionRequestError(f"{label} must not be empty")
    if value != value.strip():
        raise CorrectionRequestError(
            f"{label} must not contain surrounding whitespace"
        )
    if len(value) > maximum:
        raise CorrectionRequestError(
            f"{label} exceeds the {maximum}-character limit"
        )
    if _has_control_character(value):
        raise CorrectionRequestError(f"{label} contains control characters")
    return value


def _validate_json_tree(
    value: object,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if depth > MAX_CORRECTION_JSON_DEPTH:
        raise CorrectionRequestError(
            f"Correction JSON exceeds depth {MAX_CORRECTION_JSON_DEPTH}"
        )
    observed = counter if counter is not None else [0]
    observed[0] += 1
    if observed[0] > MAX_CORRECTION_JSON_ITEMS:
        raise CorrectionRequestError(
            f"Correction JSON exceeds {MAX_CORRECTION_JSON_ITEMS} total items"
        )

    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CorrectionRequestError(
                "Correction JSON contains NaN or Infinity"
            )
        return
    if isinstance(value, str):
        _strict_text(
            value,
            label="JSON string",
            maximum=_MAX_VALUE_STRING_CHARS,
        )
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_tree(
                item,
                depth=depth + 1,
                counter=observed,
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _strict_text(
                key,
                label="JSON object key",
                maximum=_MAX_VALUE_KEY_CHARS,
            )
            _validate_json_tree(
                item,
                depth=depth + 1,
                counter=observed,
            )
        return
    raise CorrectionRequestError("Correction value must be JSON-safe")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CorrectionRequestError(
            "Correction payload must be JSON-safe"
        ) from exc


def _reject_non_finite_json(token: str) -> object:
    raise CorrectionRequestError(
        f"Correction JSON contains forbidden number: {token}"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorrectionRequestError(
                f"Correction JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """A pure, normalized request for one independently approved correction."""

    database: str
    entity_key: str
    property_name: str
    value: JsonValue
    reason: str
    case_number: str | None = None

    def __post_init__(self) -> None:
        _strict_text(
            self.database,
            label="database",
            maximum=_MAX_DATABASE_CHARS,
        )
        _strict_text(
            self.entity_key,
            label="entity_key",
            maximum=_MAX_ENTITY_KEY_CHARS,
        )
        _strict_text(
            self.property_name,
            label="property",
            maximum=_MAX_PROPERTY_CHARS,
        )
        _strict_text(
            self.reason,
            label="reason",
            maximum=_MAX_REASON_CHARS,
        )
        if self.case_number is not None:
            _strict_text(
                self.case_number,
                label="case_number",
                maximum=_MAX_CASE_NUMBER_CHARS,
            )
        self._validate_current_payload()

    def _request_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "database": self.database,
            "entity_key": self.entity_key,
            "property": self.property_name,
            "value": self.value,
            "reason": self.reason,
        }
        if self.case_number is not None:
            payload["case_number"] = self.case_number
        return payload

    def _validate_current_payload(self) -> None:
        _validate_json_tree(self._request_dict())

    def as_dict(self) -> dict[str, JsonValue]:
        """Return a detached JSON object using the exact public request keys."""

        self._validate_current_payload()
        return json.loads(_canonical_json(self._request_dict()))

    @property
    def canonical_payload(self) -> str:
        """Domain-separated canonical JSON used for approval and replay binding."""

        self._validate_current_payload()
        return _canonical_json(
            {
                "command": CORRECTION_COMMAND.removeprefix("/"),
                "purpose": USER_CORRECTION_PURPOSE,
                "request": self._request_dict(),
            }
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_payload.encode("utf-8")).hexdigest()

    @property
    def mutation_scope(self) -> tuple[str, str, str, str]:
        """Return the shared case/database/entity/property collision scope."""

        return (
            self.case_number or "",
            self.database,
            self.entity_key,
            self.property_name,
        )


def parse_correction_command(text: str) -> CorrectionRequest:
    """Parse exactly ``/nx_correct <JSON object>`` without external effects."""

    if not isinstance(text, str):
        raise CorrectionRequestError("Correction command must be text")
    normalized = text.strip()
    prefix = CORRECTION_COMMAND + " "
    if not normalized.startswith(prefix):
        raise CorrectionRequestError(
            "Use exactly: /nx_correct "
            '{"database":"...","entity_key":"...","property":"...",'
            '"value":...,"reason":"..."}'
        )
    raw_json = normalized[len(prefix) :]
    if not raw_json or not raw_json.strip():
        raise CorrectionRequestError("Correction command requires a JSON object")
    if len(raw_json) > MAX_CORRECTION_JSON_CHARS:
        raise CorrectionRequestError(
            f"Correction JSON exceeds {MAX_CORRECTION_JSON_CHARS} characters"
        )
    try:
        payload = json.loads(
            raw_json,
            parse_constant=_reject_non_finite_json,
            object_pairs_hook=_unique_object,
        )
    except CorrectionRequestError:
        raise
    except json.JSONDecodeError as exc:
        raise CorrectionRequestError(
            f"Correction command contains invalid JSON: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise CorrectionRequestError("Correction JSON must be an object")

    keys = set(payload)
    missing = sorted(_REQUIRED_KEYS - keys)
    unexpected = sorted(keys - _ALLOWED_KEYS)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise CorrectionRequestError(
            "Correction JSON keys do not match the exact allowlist ("
            + "; ".join(details)
            + ")"
        )
    if "case_number" in payload and payload["case_number"] is None:
        raise CorrectionRequestError(
            "case_number must be omitted rather than set to null"
        )

    return CorrectionRequest(
        database=payload["database"],
        entity_key=payload["entity_key"],
        property_name=payload["property"],
        value=payload["value"],
        reason=payload["reason"],
        case_number=payload.get("case_number"),
    )
