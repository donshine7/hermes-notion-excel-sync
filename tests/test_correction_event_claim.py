from __future__ import annotations

import sqlite3

import pytest

from notion_excel_sync.persistence.database import (
    CorrectionEventClaimError,
    StateDatabase,
)


def test_correction_event_claim_is_idempotent_for_exact_retry(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    values = {
        "telegram_user_id": "synthetic-user",
        "chat_id": "synthetic-chat",
        "message_id": "synthetic-message",
        "update_id": 42,
        "request_digest": "a" * 64,
        "proposal_id": "PC-synthetic",
    }

    first = database.claim_correction_gateway_event(**values)
    repeated = database.claim_correction_gateway_event(**values)

    assert repeated == first
    with database.session() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM correction_gateway_event_claim"
        ).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"request_digest": "b" * 64},
        {"proposal_id": "PC-different"},
        {"chat_id": "other-chat"},
    ],
)
def test_correction_event_replay_with_changed_scope_fails_closed(
    tmp_path,
    changed,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    values = {
        "telegram_user_id": "synthetic-user",
        "chat_id": "synthetic-chat",
        "message_id": "synthetic-message",
        "update_id": 42,
        "request_digest": "a" * 64,
        "proposal_id": "PC-synthetic",
    }
    database.claim_correction_gateway_event(**values)

    with pytest.raises(CorrectionEventClaimError, match="changed content"):
        database.claim_correction_gateway_event(**{**values, **changed})


def test_correction_event_table_enforces_both_telegram_identities(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    database.claim_correction_gateway_event(
        telegram_user_id="synthetic-user",
        chat_id="synthetic-chat",
        message_id="synthetic-message",
        update_id=42,
        request_digest="a" * 64,
        proposal_id="PC-synthetic",
    )

    with (
        database.session() as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            """
            INSERT INTO correction_gateway_event_claim(
                event_key, telegram_user_id, chat_id, message_id, update_id,
                request_digest, proposal_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "c" * 64,
                "other-user",
                "synthetic-chat",
                "synthetic-message",
                99,
                "d" * 64,
                "PC-other",
                "2026-07-23T00:00:00+00:00",
            ),
        )
