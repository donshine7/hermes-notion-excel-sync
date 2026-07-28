from __future__ import annotations

import hashlib
import json

import pytest

from notion_excel_sync.models import USER_CORRECTION_PURPOSE
from notion_excel_sync.workflow.corrections import (
    CORRECTION_COMMAND,
    MAX_CORRECTION_JSON_CHARS,
    MAX_CORRECTION_JSON_DEPTH,
    MAX_CORRECTION_JSON_ITEMS,
    CorrectionRequest,
    CorrectionRequestError,
    parse_correction_command,
)


def command(payload: object) -> str:
    return CORRECTION_COMMAND + " " + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def valid_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "database": "한국 특허 사건",
        "entity_key": "SS-2026-001",
        "property": "현재상태",
        "value": "보류",
        "reason": "사용자가 사건 상태를 확인했습니다.",
    }
    payload.update(changes)
    return payload


def test_parse_minimal_request_and_optional_case_scope() -> None:
    minimal = parse_correction_command(command(valid_payload()))
    assert minimal.database == "한국 특허 사건"
    assert minimal.entity_key == "SS-2026-001"
    assert minimal.property_name == "현재상태"
    assert minimal.value == "보류"
    assert minimal.reason == "사용자가 사건 상태를 확인했습니다."
    assert minimal.case_number is None
    assert minimal.mutation_scope == (
        "",
        "한국 특허 사건",
        "SS-2026-001",
        "현재상태",
    )

    with_case = parse_correction_command(
        command(valid_payload(case_number="10-2026-0001234"))
    )
    assert with_case.mutation_scope == (
        "10-2026-0001234",
        "한국 특허 사건",
        "SS-2026-001",
        "현재상태",
    )


def test_canonical_payload_and_digest_are_order_and_whitespace_independent() -> None:
    first = parse_correction_command(command(valid_payload()))
    second = parse_correction_command(
        """
        /nx_correct {
          "reason": "사용자가 사건 상태를 확인했습니다.",
          "value": "보류",
          "property": "현재상태",
          "entity_key": "SS-2026-001",
          "database": "한국 특허 사건"
        }
        """
    )

    assert first.canonical_payload == second.canonical_payload
    decoded = json.loads(first.canonical_payload)
    assert decoded["purpose"] == USER_CORRECTION_PURPOSE
    assert decoded["command"] == "nx_correct"
    assert decoded["request"] == valid_payload()
    assert first.digest == hashlib.sha256(
        first.canonical_payload.encode("utf-8")
    ).hexdigest()
    assert len(first.digest) == 64


def test_as_dict_is_detached_and_uses_only_public_request_keys() -> None:
    original = {"nested": ["승인값"]}
    request = CorrectionRequest(
        database="한국 특허 사건",
        entity_key="SS-2026-001",
        property_name="비고",
        value=original,
        reason="사용자 확인",
        case_number="10-2026-0001234",
    )

    exported = request.as_dict()
    assert set(exported) == {
        "database",
        "entity_key",
        "property",
        "value",
        "reason",
        "case_number",
    }
    assert exported["value"] == original
    assert exported["value"] is not original


@pytest.mark.parametrize(
    "text",
    [
        "/nx_correct",
        "/nx_correct\t{}",
        "/NX_CORRECT {}",
        "/nx_corrected {}",
        "/nx_correct []",
        "/nx_correct null",
        "/nx_correct {",
    ],
)
def test_command_syntax_and_object_shape_are_strict(text: str) -> None:
    with pytest.raises(CorrectionRequestError):
        parse_correction_command(text)


@pytest.mark.parametrize("missing", sorted({"database", "entity_key", "property", "value", "reason"}))
def test_every_required_key_is_mandatory(missing: str) -> None:
    payload = valid_payload()
    del payload[missing]
    with pytest.raises(CorrectionRequestError, match="missing="):
        parse_correction_command(command(payload))


def test_top_level_keys_use_an_exact_allowlist() -> None:
    with pytest.raises(CorrectionRequestError, match="unexpected="):
        parse_correction_command(command(valid_payload(token="secret")))
    with pytest.raises(CorrectionRequestError, match="duplicate key"):
        parse_correction_command(
            '/nx_correct {"database":"A","database":"B","entity_key":"E",'
            '"property":"P","value":"V","reason":"R"}'
        )


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_numbers_are_rejected(token: str) -> None:
    with pytest.raises(CorrectionRequestError, match="forbidden number"):
        parse_correction_command(
            "/nx_correct "
            f'{{"database":"A","entity_key":"E","property":"P",'
            f'"value":{token},"reason":"R"}}'
        )


def test_json_character_limit_is_applied_to_the_json_only() -> None:
    raw = '{"database":"A","entity_key":"E","property":"P","value":null,"reason":"' + (
        "x" * MAX_CORRECTION_JSON_CHARS
    ) + '"}'
    with pytest.raises(CorrectionRequestError, match="4096 characters"):
        parse_correction_command(CORRECTION_COMMAND + " " + raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database", ""),
        ("database", " "),
        ("database", " leading"),
        ("entity_key", "trailing "),
        ("property", "현재\n상태"),
        ("reason", "확인\u007f완료"),
        ("case_number", None),
        ("case_number", ""),
    ],
)
def test_text_fields_reject_empty_surrounding_whitespace_and_controls(
    field: str,
    value: object,
) -> None:
    with pytest.raises(CorrectionRequestError):
        parse_correction_command(command(valid_payload(**{field: value})))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database", 1),
        ("entity_key", False),
        ("property", []),
        ("reason", {}),
    ],
)
def test_text_fields_reject_non_strings(field: str, value: object) -> None:
    with pytest.raises(CorrectionRequestError, match="must be a string"):
        parse_correction_command(command(valid_payload(**{field: value})))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database", "d" * 129),
        ("entity_key", "e" * 513),
        ("property", "p" * 129),
        ("reason", "r" * 2_001),
        ("case_number", "c" * 257),
    ],
)
def test_text_field_lengths_are_bounded(field: str, value: str) -> None:
    with pytest.raises(CorrectionRequestError, match="character limit"):
        parse_correction_command(command(valid_payload(**{field: value})))


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "line\nbreak",
        "x" * 2_049,
        {"": "value"},
        {"safe": "\u0000"},
    ],
)
def test_nested_json_strings_and_object_keys_are_bounded(value: object) -> None:
    with pytest.raises(CorrectionRequestError):
        parse_correction_command(command(valid_payload(value=value)))


def nested_list(levels: int) -> object:
    value: object = "leaf"
    for _ in range(levels):
        value = [value]
    return value


def test_depth_limit_accepts_boundary_and_rejects_one_level_more() -> None:
    # The request object is depth zero and ``value`` starts at depth one.
    parse_correction_command(
        command(valid_payload(value=nested_list(MAX_CORRECTION_JSON_DEPTH - 1)))
    )
    with pytest.raises(CorrectionRequestError, match="depth"):
        parse_correction_command(
            command(valid_payload(value=nested_list(MAX_CORRECTION_JSON_DEPTH)))
        )


def test_total_item_limit_is_enforced() -> None:
    with pytest.raises(CorrectionRequestError, match="total items"):
        CorrectionRequest(
            database="A",
            entity_key="E",
            property_name="P",
            value=[0] * MAX_CORRECTION_JSON_ITEMS,
            reason="R",
        )


@pytest.mark.parametrize(
    "value",
    [
        object(),
        (1, 2),
        float("nan"),
        float("inf"),
        {1: "not-a-string-key"},
    ],
)
def test_direct_model_construction_also_enforces_json_safety(value: object) -> None:
    with pytest.raises(CorrectionRequestError):
        CorrectionRequest(
            database="A",
            entity_key="E",
            property_name="P",
            value=value,  # type: ignore[arg-type]
            reason="R",
        )


def test_mutating_a_nested_value_cannot_produce_an_unvalidated_digest() -> None:
    nested = {"safe": ["value"]}
    request = CorrectionRequest(
        database="A",
        entity_key="E",
        property_name="P",
        value=nested,
        reason="R",
    )
    original_digest = request.digest
    nested["safe"].append("\n")

    with pytest.raises(CorrectionRequestError):
        _ = request.digest
    assert original_digest
