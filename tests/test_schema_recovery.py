from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from notion_excel_sync.models import (
    SCHEMA_RECOVERY_PURPOSE,
    SchemaProposalRevision,
    SchemaProposalStatus,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.schema_approval import SchemaApprovalService
from notion_excel_sync.security.schema_recovery import (
    SchemaRecoveryError,
    SchemaRecoveryService,
)


NOW = datetime(2026, 7, 22, 10, tzinfo=UTC)
RAW_DATABASE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RAW_DATA_SOURCE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _proposal() -> SchemaProposalRevision:
    return SchemaProposalRevision(
        schema_proposal_id="SP-recovery-test",
        revision=1,
        requested_by="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
        operation_id="operation-1",
        logical_database_name="정부지원사업 증빙",
        parent_page_id="c" * 32,
        schema_payload={"fixed": True},
        precondition={"fixed": True},
        status=SchemaProposalStatus.UNKNOWN,
        created_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=5),
    )


def _service(secret: str = "recovery-test-secret-that-is-long-enough") -> SchemaRecoveryService:
    return SchemaRecoveryService(secret, {"user-1"}, nonce_used=lambda _: False)


def _receipt(service: SchemaRecoveryService | None = None):
    proposal = _proposal()
    return (service or _service()).issue(
        proposal,
        telegram_user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
        telegram_message_id="message-3",
        telegram_update_id=3,
        confirmed_digest=proposal.digest,
        raw_database_id=RAW_DATABASE_ID,
        raw_data_source_id=RAW_DATA_SOURCE_ID,
        canonical_database_id=RAW_DATABASE_ID.replace("-", ""),
        canonical_data_source_id=RAW_DATA_SOURCE_ID.replace("-", ""),
        attempt_started_at=NOW - timedelta(seconds=3),
        attempt_finished_at=NOW - timedelta(seconds=2),
        approval_receipt_nonce="approval-nonce",
        conflict_telegram_update_id=2,
        now=NOW,
    )


def test_recovery_receipt_has_separate_hmac_domain_and_exact_raw_identity() -> None:
    proposal = _proposal()
    service = _service()
    receipt = _receipt(service)

    service.verify(
        receipt,
        proposal,
        telegram_user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
        telegram_message_id="message-3",
        telegram_update_id=3,
        raw_database_id=RAW_DATABASE_ID,
        raw_data_source_id=RAW_DATA_SOURCE_ID,
        attempt_started_at=NOW - timedelta(seconds=3),
        attempt_finished_at=NOW - timedelta(seconds=2),
        now=NOW,
    )
    assert receipt.purpose == SCHEMA_RECOVERY_PURPOSE

    with pytest.raises(SchemaRecoveryError):
        service.verify(
            replace(receipt, raw_database_id=RAW_DATABASE_ID.replace("-", "")),
            proposal,
            telegram_user_id="user-1",
            chat_id="chat-1",
            thread_id="thread-1",
            telegram_message_id="message-3",
            telegram_update_id=3,
            raw_database_id=RAW_DATABASE_ID.replace("-", ""),
            raw_data_source_id=RAW_DATA_SOURCE_ID,
            attempt_started_at=NOW - timedelta(seconds=3),
            attempt_finished_at=NOW - timedelta(seconds=2),
            now=NOW,
        )

    create_service = SchemaApprovalService(
        "recovery-test-secret-that-is-long-enough",
        {"user-1"},
        nonce_used=lambda _: True,
    )
    assert not create_service.verify_operation_scope(  # type: ignore[arg-type]
        receipt,
        replace(proposal, status=SchemaProposalStatus.APPLYING),
        proposal.operation_id,
    )


def test_recovery_issue_rejects_raw_and_canonical_id_disagreement() -> None:
    proposal = _proposal()
    with pytest.raises(SchemaRecoveryError):
        _service().issue(
            proposal,
            telegram_user_id="user-1",
            chat_id="chat-1",
            thread_id="thread-1",
            telegram_message_id="message-3",
            telegram_update_id=3,
            confirmed_digest=proposal.digest,
            raw_database_id=RAW_DATABASE_ID,
            raw_data_source_id=RAW_DATA_SOURCE_ID,
            canonical_database_id="d" * 32,
            canonical_data_source_id=RAW_DATA_SOURCE_ID.replace("-", ""),
            attempt_started_at=NOW - timedelta(seconds=3),
            attempt_finished_at=NOW - timedelta(seconds=2),
            approval_receipt_nonce="approval-nonce",
            conflict_telegram_update_id=2,
            now=NOW,
        )


def test_recovery_receipt_expiry_and_nonce_are_fail_closed() -> None:
    used: set[str] = set()
    service = SchemaRecoveryService(
        "recovery-test-secret-that-is-long-enough",
        {"user-1"},
        ttl_seconds=10,
        nonce_used=used.__contains__,
    )
    proposal = _proposal()
    receipt = _receipt(service)
    verify_args = {
        "telegram_user_id": "user-1",
        "chat_id": "chat-1",
        "thread_id": "thread-1",
        "telegram_message_id": "message-3",
        "telegram_update_id": 3,
        "raw_database_id": RAW_DATABASE_ID,
        "raw_data_source_id": RAW_DATA_SOURCE_ID,
        "attempt_started_at": NOW - timedelta(seconds=3),
        "attempt_finished_at": NOW - timedelta(seconds=2),
    }
    with pytest.raises(SchemaRecoveryError):
        service.verify(
            receipt,
            proposal,
            **verify_args,
            now=NOW + timedelta(seconds=10),
        )
    used.add(receipt.nonce)
    with pytest.raises(SchemaRecoveryError):
        service.verify(receipt, proposal, **verify_args, now=NOW)


def test_recovery_tables_enforce_the_separate_purpose(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    with database.session() as connection:
        receipt_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'schema_recovery_receipt'"
        ).fetchone()["sql"]
        event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'schema_recovery_event_claim'"
        ).fetchone()["sql"]
    assert "purpose = 'notion_schema_recovery/v1'" in receipt_sql
    assert "purpose = 'notion_schema_recovery/v1'" in event_sql
