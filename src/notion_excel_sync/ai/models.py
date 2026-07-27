from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from notion_excel_sync.models import JsonValue, ProposedChange, ReviewItem, sha256_json


AI_SCHEMA_VERSION = "1.0"
AI_PROMPT_VERSION = "structured-analysis-v1"
_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_INFERENCE_TYPES = frozenset(
    {"extract", "classify", "link", "summarize", "recommend"}
)
_TASK_FIELDS: Mapping[str, frozenset[str]] = {
    "party_resolution": frozenset(
        {"canonical_party_name", "party_names", "party_type", "summary"}
    ),
    "email_case_event": frozenset(
        {
            "case_numbers",
            "event_type",
            "occurred_on",
            "summary",
            "next_action",
            "next_deadline",
        }
    ),
    "government_support_evidence": frozenset(
        {
            "recommended_case_numbers",
            "evidence_method",
            "summary",
            "review_reason",
        }
    ),
    "case_history_summary": frozenset({"summary", "next_action"}),
    "excel_row_semantics": frozenset(
        {"classification", "case_numbers", "summary"}
    ),
}
_PARTY_TYPES = frozenset(
    {"개인", "법인", "기관", "정부지원기관", "기타"}
)
_DATE_FIELDS = frozenset({"occurred_on", "next_deadline"})
_CASE_FIELDS = frozenset(
    {"case_numbers", "recommended_case_numbers"}
)


class AISchemaError(ValueError):
    """Raised when a model response is not safely structured and grounded."""


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    location: str
    quote: str


@dataclass(frozen=True, slots=True)
class AIClaim:
    field: str
    value: JsonValue
    confidence: float
    inference_type: str
    evidence: tuple[EvidenceSpan, ...]


@dataclass(frozen=True, slots=True)
class AIRequest:
    task_type: str
    source_hash: str
    source_type: str
    source_locator: str
    payload: Mapping[str, JsonValue]
    evidence_corpus: str = field(repr=False)
    known_case_numbers: tuple[str, ...] = ()
    prompt_version: str = AI_PROMPT_VERSION

    def __post_init__(self) -> None:
        if self.task_type not in _TASK_FIELDS:
            raise ValueError(f"Unsupported AI task type: {self.task_type}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_hash):
            raise ValueError("AI source_hash must be a lowercase SHA-256 digest")
        if not self.source_type.strip() or not self.source_locator.strip():
            raise ValueError("AI source type and locator are required")
        if not self.prompt_version.strip():
            raise ValueError("AI prompt version is required")

    @property
    def request_digest(self) -> str:
        return sha256_json(
            {
                "task_type": self.task_type,
                "source_hash": self.source_hash,
                "source_type": self.source_type,
                "source_locator": self.source_locator,
                "payload": self.payload,
                "known_case_numbers": self.known_case_numbers,
                "prompt_version": self.prompt_version,
            }
        )


@dataclass(frozen=True, slots=True)
class AIProviderResponse:
    payload: Mapping[str, Any]
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class AIAnalysis:
    task_type: str
    source_hash: str
    summary: str
    claims: tuple[AIClaim, ...]
    conflicts: tuple[str, ...]
    needs_review: bool
    overall_confidence: float
    provider: str
    model: str
    prompt_version: str
    request_digest: str

    @property
    def output_digest(self) -> str:
        return sha256_json(self.public_payload(include_model=True))

    def public_payload(self, *, include_model: bool = True) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "schema_version": AI_SCHEMA_VERSION,
            "task_type": self.task_type,
            "source_hash": self.source_hash,
            "summary": self.summary,
            "claims": [
                {
                    "field": claim.field,
                    "value": claim.value,
                    "confidence": claim.confidence,
                    "inference_type": claim.inference_type,
                    "evidence": [
                        {"location": item.location, "quote": item.quote}
                        for item in claim.evidence
                    ],
                }
                for claim in self.claims
            ],
            "conflicts": list(self.conflicts),
            "needs_review": self.needs_review,
            "overall_confidence": self.overall_confidence,
            "prompt_version": self.prompt_version,
            "request_digest": self.request_digest,
        }
        if include_model:
            payload["provider"] = self.provider
            payload["model"] = self.model
        return payload

    def claim(self, field_name: str) -> AIClaim | None:
        return next(
            (claim for claim in self.claims if claim.field == field_name),
            None,
        )


@dataclass(slots=True)
class AIEnrichmentReport:
    proposed_changes: list[ProposedChange] = field(default_factory=list)
    review_items: list[ReviewItem] = field(default_factory=list)
    replaced_operation_ids: set[str] = field(default_factory=set)
    attempted: int = 0
    completed: int = 0
    cache_hits: int = 0
    failures: int = 0
    skipped: int = 0
    shadow_results: int = 0
    failure_codes: dict[str, int] = field(default_factory=dict)

    def audit_payload(self) -> dict[str, JsonValue]:
        return {
            "attempted": self.attempted,
            "completed": self.completed,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "skipped": self.skipped,
            "shadow_results": self.shadow_results,
            "proposed_changes": len(self.proposed_changes),
            "review_items": len(self.review_items),
            "replaced_operations": len(self.replaced_operation_ids),
            "failure_codes": dict(sorted(self.failure_codes.items())),
        }


def structured_output_schema(task_type: str) -> dict[str, Any]:
    allowed = sorted(_TASK_FIELDS[task_type])
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "task_type",
            "source_hash",
            "summary",
            "claims",
            "conflicts",
            "needs_review",
            "overall_confidence",
        ],
        "properties": {
            "schema_version": {"const": AI_SCHEMA_VERSION},
            "task_type": {"const": task_type},
            "source_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "summary": {"type": "string", "maxLength": 800},
            "claims": {
                "type": "array",
                "maxItems": 25,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "field",
                        "value",
                        "confidence",
                        "inference_type",
                        "evidence",
                    ],
                    "properties": {
                        "field": {"type": "string", "enum": allowed},
                        "value": {},
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "inference_type": {
                            "type": "string",
                            "enum": sorted(_INFERENCE_TYPES),
                        },
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["location", "quote"],
                                "properties": {
                                    "location": {
                                        "type": "string",
                                        "maxLength": 120,
                                    },
                                    "quote": {
                                        "type": "string",
                                        "maxLength": 500,
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "conflicts": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "maxLength": 300},
            },
            "needs_review": {"type": "boolean"},
            "overall_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
        },
    }


def validate_analysis(
    request: AIRequest,
    response: AIProviderResponse,
) -> AIAnalysis:
    payload = dict(response.payload)
    # Some otherwise schema-capable providers omit constant envelope fields.
    # These values are trusted request metadata, not model-derived facts, so a
    # missing value can be restored safely. An explicitly conflicting value is
    # still rejected below.
    payload.setdefault("schema_version", AI_SCHEMA_VERSION)
    payload.setdefault("task_type", request.task_type)
    payload.setdefault("source_hash", request.source_hash)
    allowed_top = {
        "schema_version",
        "task_type",
        "source_hash",
        "summary",
        "claims",
        "conflicts",
        "needs_review",
        "overall_confidence",
    }
    required_top = allowed_top
    if set(payload) != required_top:
        raise AISchemaError("AI response fields do not exactly match the schema")
    if payload["schema_version"] != AI_SCHEMA_VERSION:
        raise AISchemaError("AI schema version is unsupported")
    if payload["task_type"] != request.task_type:
        raise AISchemaError("AI task type does not match the request")
    if payload["source_hash"] != request.source_hash:
        raise AISchemaError("AI source hash does not match the request")

    summary = _bounded_string(payload["summary"], "summary", 800)
    conflicts_raw = payload["conflicts"]
    if not isinstance(conflicts_raw, list) or len(conflicts_raw) > 10:
        raise AISchemaError("AI conflicts must be a bounded list")
    conflicts = tuple(
        _bounded_string(item, "conflict", 300) for item in conflicts_raw
    )
    needs_review = payload["needs_review"]
    if not isinstance(needs_review, bool):
        raise AISchemaError("AI needs_review must be boolean")
    overall_confidence = _confidence(
        payload["overall_confidence"],
        "overall_confidence",
    )

    claims_raw = payload["claims"]
    if not isinstance(claims_raw, list) or len(claims_raw) > 25:
        raise AISchemaError("AI claims must be a bounded list")
    claims: list[AIClaim] = []
    seen_fields: set[str] = set()
    for raw in claims_raw:
        if not isinstance(raw, dict) or set(raw) != {
            "field",
            "value",
            "confidence",
            "inference_type",
            "evidence",
        }:
            raise AISchemaError("AI claim does not exactly match the schema")
        field_name = _bounded_string(raw["field"], "claim field", 64)
        if (
            _FIELD_RE.fullmatch(field_name) is None
            or field_name not in _TASK_FIELDS[request.task_type]
            or field_name in seen_fields
        ):
            raise AISchemaError("AI claim field is unsupported or duplicated")
        seen_fields.add(field_name)
        inference_type = _bounded_string(
            raw["inference_type"],
            "inference_type",
            20,
        )
        if inference_type not in _INFERENCE_TYPES:
            raise AISchemaError("AI inference_type is unsupported")
        value = _json_value(raw["value"])
        evidence = _evidence_spans(raw["evidence"], request.evidence_corpus)
        _validate_claim_value(request, field_name, value)
        claims.append(
            AIClaim(
                field=field_name,
                value=value,
                confidence=_confidence(raw["confidence"], "claim confidence"),
                inference_type=inference_type,
                evidence=evidence,
            )
        )

    return AIAnalysis(
        task_type=request.task_type,
        source_hash=request.source_hash,
        summary=summary,
        claims=tuple(claims),
        conflicts=conflicts,
        needs_review=needs_review,
        overall_confidence=overall_confidence,
        provider=_bounded_string(response.provider or "unknown", "provider", 120),
        model=_bounded_string(response.model or "unknown", "model", 200),
        prompt_version=request.prompt_version,
        request_digest=request.request_digest,
    )


def analysis_from_cache(payload: Mapping[str, Any]) -> AIAnalysis:
    claims = tuple(
        AIClaim(
            field=str(item["field"]),
            value=item.get("value"),
            confidence=float(item["confidence"]),
            inference_type=str(item["inference_type"]),
            evidence=tuple(
                EvidenceSpan(
                    location=str(span["location"]),
                    quote=str(span["quote"]),
                )
                for span in item.get("evidence", [])
            ),
        )
        for item in payload.get("claims", [])
    )
    return AIAnalysis(
        task_type=str(payload["task_type"]),
        source_hash=str(payload["source_hash"]),
        summary=str(payload["summary"]),
        claims=claims,
        conflicts=tuple(str(item) for item in payload.get("conflicts", [])),
        needs_review=bool(payload["needs_review"]),
        overall_confidence=float(payload["overall_confidence"]),
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        prompt_version=str(payload["prompt_version"]),
        request_digest=str(payload["request_digest"]),
    )


def _bounded_string(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise AISchemaError(f"AI {label} must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise AISchemaError(f"AI {label} is empty or too long")
    return normalized


def _confidence(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AISchemaError(f"AI {label} must be numeric")
    confidence = float(value)
    if not 0 <= confidence <= 1:
        raise AISchemaError(f"AI {label} must be between 0 and 1")
    return confidence


def _json_value(value: object) -> JsonValue:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AISchemaError("AI claim value is not valid JSON") from exc
    if len(rendered) > 4_000:
        raise AISchemaError("AI claim value is too large")
    return value  # type: ignore[return-value]


def _normalized_evidence(value: str) -> str:
    return " ".join(value.split()).casefold()


def _evidence_spans(
    value: object,
    evidence_corpus: str,
) -> tuple[EvidenceSpan, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise AISchemaError("Every AI claim requires one to five evidence spans")
    corpus = _normalized_evidence(evidence_corpus)
    spans: list[EvidenceSpan] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"location", "quote"}:
            raise AISchemaError("AI evidence does not exactly match the schema")
        location = _bounded_string(item["location"], "evidence location", 120)
        quote = _bounded_string(item["quote"], "evidence quote", 500)
        if _normalized_evidence(quote) not in corpus:
            raise AISchemaError("AI evidence quote is absent from the supplied source")
        spans.append(EvidenceSpan(location=location, quote=quote))
    return tuple(spans)


def _as_text_values(value: JsonValue, *, field_name: str) -> tuple[str, ...]:
    raw = value if isinstance(value, list) else [value]
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AISchemaError(f"AI {field_name} must contain non-empty text")
        values.append(item.strip())
    return tuple(values)


def _validate_claim_value(
    request: AIRequest,
    field_name: str,
    value: JsonValue,
) -> None:
    corpus = _normalized_evidence(request.evidence_corpus)
    if field_name in _CASE_FIELDS:
        known = {item.casefold() for item in request.known_case_numbers}
        for item in _as_text_values(value, field_name=field_name):
            if item.casefold() not in known and item.casefold() not in corpus:
                raise AISchemaError("AI case number is absent from the source and known cases")
    elif field_name == "party_names":
        for item in _as_text_values(value, field_name=field_name):
            if item.casefold() not in corpus:
                raise AISchemaError("AI party name is absent from the source")
    elif field_name == "canonical_party_name":
        values = _as_text_values(value, field_name=field_name)
        if len(values) != 1 or values[0].casefold() not in corpus:
            raise AISchemaError("AI canonical party name is absent from the source")
    elif field_name == "party_type":
        if not isinstance(value, str) or value not in _PARTY_TYPES:
            raise AISchemaError("AI party_type is unsupported")
    elif field_name in _DATE_FIELDS and value is not None:
        if not isinstance(value, str):
            raise AISchemaError(f"AI {field_name} must be an ISO date or null")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise AISchemaError(f"AI {field_name} must be an ISO date") from exc
        if value not in request.evidence_corpus:
            raise AISchemaError(f"AI {field_name} is not explicitly present in the source")
