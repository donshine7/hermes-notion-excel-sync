from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from notion_excel_sync.knowledge import corrections
from notion_excel_sync.knowledge.corrections import (
    ApprovedCorrectionEvent,
    CORRECTION_AUTHORITY,
    CorrectionEventConflict,
    CorrectionOverlayError,
    CorrectionOverlayStore,
    CorrectionValidationError,
)


_PROPOSAL_DIGEST = "a" * 64
_SOURCE_REFS_DIGEST = "b" * 64


def _event(
    event_id: str = "correction-001",
    **overrides: object,
) -> ApprovedCorrectionEvent:
    values: dict[str, object] = {
        "event_id": event_id,
        "proposal_id": "P-20260723-abcd1234",
        "proposal_revision": 2,
        "proposal_digest": _PROPOSAL_DIGEST,
        "operation_id": "operation-001",
        "case_number": "10-2026-1234567",
        "target_database": "한국 특허 사건",
        "entity_key": "10-2026-1234567",
        "property_name": "진행상태",
        "current_value": "심사중",
        "approved_value": "등록결정",
        "reason": "Excel 원본과 대조하여 사용자가 수정값을 승인했습니다.",
        "approved_by_user_id": "synthetic-user-001",
        "approved_in_chat_id": "synthetic-chat-001",
        "approved_at": datetime(2026, 7, 23, 10, 30, tzinfo=UTC),
        "source_refs_digest": _SOURCE_REFS_DIGEST,
    }
    values.update(overrides)
    return ApprovedCorrectionEvent(**values)  # type: ignore[arg-type]


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_publish_writes_only_append_only_correction_layer(tmp_path: Path) -> None:
    output = tmp_path / "wiki-output"
    existing = output / "excel" / "generations" / "existing" / "manifest.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"authority":"excel_source"}', encoding="utf-8")
    existing_before = existing.read_bytes()
    existing_mtime = existing.stat().st_mtime_ns
    store = CorrectionOverlayStore(output)
    event = _event()

    result = store.publish(event)

    assert result.event_created is True
    assert result.event_count == 1
    assert result.event_path == output / "corrections" / "events" / "correction-001.json"
    assert result.catalog_path == (
        output
        / "corrections"
        / "generations"
        / result.generation
        / "catalog.json"
    )
    assert result.current_path.read_text(encoding="ascii") == f"{result.generation}\n"
    payload = _read_json(result.event_path)
    assert payload == event.as_dict()
    assert payload["authority"] == CORRECTION_AUTHORITY
    assert payload["current_value"] == "심사중"
    assert payload["approved_value"] == "등록결정"
    assert "approval_nonce" not in payload
    assert "token" not in payload
    assert "mail_body" not in payload
    catalog = _read_json(result.catalog_path)
    assert catalog["event_count"] == 1
    assert catalog["events"] == [
        {
            "event_id": event.event_id,
            "payload_digest": event.payload_digest,
        }
    ]
    assert existing.read_bytes() == existing_before
    assert existing.stat().st_mtime_ns == existing_mtime


def test_same_event_and_payload_is_idempotent_without_rewriting_event(
    tmp_path: Path,
) -> None:
    store = CorrectionOverlayStore(tmp_path / "output")
    event = _event()
    first = store.publish(event)
    event_mtime = first.event_path.stat().st_mtime_ns

    repeated = store.publish(
        replace(
            event,
            approved_at=datetime(
                2026,
                7,
                23,
                19,
                30,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )
    )

    assert repeated.event_created is False
    assert repeated.event_digest == first.event_digest
    assert repeated.generation == first.generation
    assert repeated.catalog_path == first.catalog_path
    assert repeated.event_path.stat().st_mtime_ns == event_mtime


def test_same_event_id_with_different_payload_fails_closed(tmp_path: Path) -> None:
    store = CorrectionOverlayStore(tmp_path / "output")
    original = store.publish(_event())
    event_before = original.event_path.read_bytes()
    current_before = original.current_path.read_bytes()

    with pytest.raises(CorrectionEventConflict, match="different content"):
        store.publish(_event(reason="다른 승인 사유"))

    assert original.event_path.read_bytes() == event_before
    assert original.current_path.read_bytes() == current_before
    assert len(list((store.corrections_root / "events").glob("*.json"))) == 1


def test_catalog_generation_is_deterministic_and_preserves_older_generations(
    tmp_path: Path,
) -> None:
    first_store = CorrectionOverlayStore(tmp_path / "first")
    second_store = CorrectionOverlayStore(tmp_path / "second")
    first_event = _event("correction-001")
    second_event = _event(
        "correction-002",
        operation_id="operation-002",
        property_name="비고",
        current_value=None,
        approved_value="사용자 승인 보정",
    )

    first_generation = first_store.publish(first_event)
    first_final = first_store.publish(second_event)
    second_store.publish(second_event)
    second_final = second_store.publish(first_event)

    assert first_final.generation == second_final.generation
    assert first_final.event_count == 2
    assert first_generation.catalog_path.is_file()
    assert first_generation.catalog_path != first_final.catalog_path
    catalog = _read_json(first_final.catalog_path)
    assert [row["event_id"] for row in catalog["events"]] == [
        "correction-001",
        "correction-002",
    ]
    assert len(list((first_store.corrections_root / "events").glob("*.json"))) == 2


def test_failed_current_publication_keeps_previous_pointer_and_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CorrectionOverlayStore(tmp_path / "output")
    first = store.publish(_event())
    second = _event(
        "correction-002",
        operation_id="operation-002",
        approved_value="보정값 2",
    )
    real_replace = corrections.os.replace

    def fail_current(source: object, destination: object) -> None:
        if Path(destination).name == "CURRENT":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(corrections.os, "replace", fail_current)
    with pytest.raises(OSError, match="pointer failure"):
        store.publish(second)
    assert first.current_path.read_text(encoding="ascii") == f"{first.generation}\n"
    assert (store.corrections_root / "events" / "correction-002.json").is_file()

    monkeypatch.setattr(corrections.os, "replace", real_replace)
    recovered = store.publish(second)

    assert recovered.event_created is False
    assert recovered.event_count == 2
    assert recovered.current_path.read_text(encoding="ascii") == (
        f"{recovered.generation}\n"
    )


@pytest.mark.parametrize(
    "event_id",
    [
        "",
        "../escape",
        "folder/event",
        "UPPERCASE",
        ".",
        "event id",
        "a" * 129,
    ],
)
def test_event_id_rejects_traversal_and_abnormal_names(
    event_id: str,
) -> None:
    with pytest.raises(CorrectionValidationError, match="event_id"):
        _event(event_id)


def test_event_rejects_secrets_raw_mail_and_non_json_values() -> None:
    with pytest.raises(TypeError):
        ApprovedCorrectionEvent(  # type: ignore[call-arg]
            **_event().as_dict(),
            approval_nonce="must-not-be-accepted",
        )
    with pytest.raises(CorrectionValidationError, match="tokens"):
        _event(approved_value={"approval_nonce": "secret"})
    with pytest.raises(CorrectionValidationError, match="tokens"):
        _event(current_value={"mail_body": "raw message"})
    with pytest.raises(CorrectionValidationError, match="non-finite"):
        _event(approved_value=float("nan"))
    with pytest.raises(CorrectionValidationError, match="JSON-safe"):
        _event(approved_value=("tuple",))
    with pytest.raises(CorrectionValidationError, match="timezone-aware"):
        _event(approved_at=datetime(2026, 7, 23, 10, 30))

    payload = _event().as_dict()
    payload["access_token"] = "must-not-load"
    with pytest.raises(CorrectionValidationError, match="unexpected"):
        ApprovedCorrectionEvent.from_dict(payload)


def test_output_root_and_managed_paths_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CorrectionOverlayError, match="absolute"):
        CorrectionOverlayStore("relative-output")

    output = tmp_path / "output"
    store = CorrectionOverlayStore(output)
    outside = tmp_path / "outside"
    outside.mkdir()
    events = store.corrections_root / "events"
    try:
        events.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted on this host")
    with pytest.raises(CorrectionOverlayError, match="symbolic links"):
        store.publish(_event())
