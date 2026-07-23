from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Callable

from notion_excel_sync.models import (
    SCHEMA_RECOVERY_PURPOSE,
    SchemaProposalRevision,
    SchemaProposalStatus,
    SchemaRecoveryReceipt,
    canonical_json,
    schema_recovery_command_hash,
)


_HMAC_DOMAIN = b"notion-excel-sync\x00notion_schema_recovery/v1\x00"
_CANONICAL_NOTION_ID = re.compile(r"[0-9a-f]{32}")
_NOTION_ID = re.compile(
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)


class SchemaRecoveryError(PermissionError):
    """Raised when exact Telegram authority for GET-only recovery is absent."""


class SchemaRecoveryService:
    """Issue and verify one-time, domain-separated schema recovery receipts."""

    def __init__(
        self,
        secret: str,
        allowed_users: set[str],
        ttl_seconds: int = 1800,
        nonce_used: Callable[[str], bool] | None = None,
    ) -> None:
        if len(secret) < 24:
            raise ValueError("Schema recovery secret must be at least 24 characters")
        if ttl_seconds <= 0:
            raise ValueError("Schema recovery TTL must be positive")
        self._secret = secret.encode("utf-8")
        self._allowed_users = {str(user) for user in allowed_users}
        self._ttl_seconds = ttl_seconds
        self._nonce_used = nonce_used or (lambda _: False)

    @staticmethod
    def _signature_payload(receipt_data: dict[str, object]) -> bytes:
        payload = dict(receipt_data)
        payload.pop("signature", None)
        if payload.get("purpose") != SCHEMA_RECOVERY_PURPOSE:
            raise SchemaRecoveryError("Schema recovery receipt purpose is invalid")
        return _HMAC_DOMAIN + canonical_json(payload).encode("utf-8")

    def _sign(self, receipt_data: dict[str, object]) -> str:
        return hmac.new(
            self._secret,
            self._signature_payload(receipt_data),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        proposal: SchemaProposalRevision,
        *,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
        confirmed_digest: str,
        raw_database_id: str,
        raw_data_source_id: str,
        canonical_database_id: str,
        canonical_data_source_id: str,
        attempt_started_at: datetime,
        attempt_finished_at: datetime,
        approval_receipt_nonce: str,
        conflict_telegram_update_id: int,
        now: datetime | None = None,
    ) -> SchemaRecoveryReceipt:
        issued_at = _aware_utc(now)
        user = str(telegram_user_id)
        chat = str(chat_id)
        thread = str(thread_id)
        message = str(telegram_message_id).strip()
        started = _aware_utc(attempt_started_at)
        finished = _aware_utc(attempt_finished_at)
        if proposal.status is not SchemaProposalStatus.UNKNOWN:
            raise SchemaRecoveryError("Only an UNKNOWN schema attempt can be recovered")
        if user not in self._allowed_users:
            raise SchemaRecoveryError("Telegram user is not authorized for recovery")
        if (
            user != proposal.requested_by
            or chat != proposal.chat_id
            or thread != proposal.thread_id
        ):
            raise SchemaRecoveryError(
                "Recovery must come from the proposal owner and original chat/thread"
            )
        if not message:
            raise SchemaRecoveryError("Telegram message_id is required")
        if isinstance(telegram_update_id, bool) or telegram_update_id < 0:
            raise SchemaRecoveryError("Telegram update_id is invalid")
        if (
            isinstance(conflict_telegram_update_id, bool)
            or conflict_telegram_update_id < 0
        ):
            raise SchemaRecoveryError("Conflict update_id is invalid")
        if not hmac.compare_digest(confirmed_digest.lower(), proposal.digest):
            raise SchemaRecoveryError("Confirmed recovery digest differs")
        if not raw_database_id.strip() or not raw_data_source_id.strip():
            raise SchemaRecoveryError("Journaled remote IDs are incomplete")
        if (
            _CANONICAL_NOTION_ID.fullmatch(canonical_database_id) is None
            or _CANONICAL_NOTION_ID.fullmatch(canonical_data_source_id) is None
        ):
            raise SchemaRecoveryError("Canonical remote IDs are invalid")
        if (
            _canonical_notion_id(raw_database_id) != canonical_database_id
            or _canonical_notion_id(raw_data_source_id) != canonical_data_source_id
        ):
            raise SchemaRecoveryError("Raw and canonical remote IDs differ")
        if finished < started or issued_at < finished:
            raise SchemaRecoveryError("Recovery evidence time ordering is invalid")
        if not approval_receipt_nonce.strip():
            raise SchemaRecoveryError("Prior approval receipt nonce is required")

        unsigned: dict[str, object] = {
            "schema_proposal_id": proposal.schema_proposal_id,
            "revision": proposal.revision,
            "proposal_digest": proposal.digest,
            "precondition_digest": proposal.precondition_digest,
            "operation_id": proposal.operation_id,
            "raw_database_id": raw_database_id,
            "raw_data_source_id": raw_data_source_id,
            "canonical_database_id": canonical_database_id,
            "canonical_data_source_id": canonical_data_source_id,
            "attempt_started_at": started,
            "attempt_finished_at": finished,
            "approval_receipt_nonce": approval_receipt_nonce,
            "conflict_telegram_update_id": int(conflict_telegram_update_id),
            "telegram_user_id": user,
            "chat_id": chat,
            "thread_id": thread,
            "telegram_message_id": message,
            "telegram_update_id": int(telegram_update_id),
            "command_hash": schema_recovery_command_hash(
                proposal.schema_proposal_id,
                proposal.revision,
                proposal.digest,
                telegram_user_id=user,
                chat_id=chat,
                thread_id=thread,
                telegram_message_id=message,
                telegram_update_id=telegram_update_id,
            ),
            "issued_at": issued_at,
            "expires_at": issued_at + timedelta(seconds=self._ttl_seconds),
            "nonce": token_hex(16),
            "signature": "",
            "purpose": SCHEMA_RECOVERY_PURPOSE,
        }
        unsigned["signature"] = self._sign(unsigned)
        purpose = unsigned.pop("purpose")
        assert purpose == SCHEMA_RECOVERY_PURPOSE
        return SchemaRecoveryReceipt(**unsigned)  # type: ignore[arg-type]

    def verify(
        self,
        receipt: SchemaRecoveryReceipt,
        proposal: SchemaProposalRevision,
        *,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
        raw_database_id: str,
        raw_data_source_id: str,
        attempt_started_at: datetime,
        attempt_finished_at: datetime,
        now: datetime | None = None,
    ) -> None:
        checked_at = _aware_utc(now)
        if not isinstance(receipt, SchemaRecoveryReceipt):
            raise SchemaRecoveryError("A schema-recovery receipt is required")
        expected_signature = self._sign(asdict(receipt))
        if not hmac.compare_digest(expected_signature, receipt.signature):
            raise SchemaRecoveryError("Schema recovery receipt signature is invalid")
        if receipt.telegram_user_id not in self._allowed_users:
            raise SchemaRecoveryError("Schema recovery user is no longer authorized")
        if (
            receipt.issued_at.tzinfo is None
            or receipt.expires_at.tzinfo is None
            or receipt.attempt_started_at.tzinfo is None
            or receipt.attempt_finished_at.tzinfo is None
            or receipt.expires_at <= receipt.issued_at
            or receipt.attempt_finished_at < receipt.attempt_started_at
            or receipt.issued_at < receipt.attempt_finished_at
        ):
            raise SchemaRecoveryError("Schema recovery timestamps are invalid")
        if checked_at >= receipt.expires_at:
            raise SchemaRecoveryError("Schema recovery receipt has expired")
        if self._nonce_used(receipt.nonce):
            raise SchemaRecoveryError("Schema recovery receipt has already been used")
        expected_hash = schema_recovery_command_hash(
            proposal.schema_proposal_id,
            proposal.revision,
            proposal.digest,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            telegram_message_id=telegram_message_id,
            telegram_update_id=telegram_update_id,
        )
        if (
            receipt.purpose != SCHEMA_RECOVERY_PURPOSE
            or proposal.status is not SchemaProposalStatus.UNKNOWN
            or receipt.schema_proposal_id != proposal.schema_proposal_id
            or receipt.revision != proposal.revision
            or not hmac.compare_digest(receipt.proposal_digest, proposal.digest)
            or not hmac.compare_digest(
                receipt.precondition_digest,
                proposal.precondition_digest,
            )
            or receipt.operation_id != proposal.operation_id
            or receipt.telegram_user_id != proposal.requested_by
            or receipt.chat_id != proposal.chat_id
            or receipt.thread_id != proposal.thread_id
            or receipt.telegram_user_id != str(telegram_user_id)
            or receipt.chat_id != str(chat_id)
            or receipt.thread_id != str(thread_id)
            or receipt.telegram_message_id != str(telegram_message_id).strip()
            or receipt.telegram_update_id != telegram_update_id
            or not hmac.compare_digest(receipt.command_hash, expected_hash)
            or receipt.raw_database_id != raw_database_id
            or receipt.raw_data_source_id != raw_data_source_id
            or _canonical_notion_id(receipt.raw_database_id)
            != receipt.canonical_database_id
            or _canonical_notion_id(receipt.raw_data_source_id)
            != receipt.canonical_data_source_id
            or receipt.attempt_started_at != _aware_utc(attempt_started_at)
            or receipt.attempt_finished_at != _aware_utc(attempt_finished_at)
        ):
            raise SchemaRecoveryError(
                "Schema recovery receipt does not match the exact recovery scope"
            )


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise SchemaRecoveryError("Schema recovery time must be timezone-aware")
    return current.astimezone(UTC)


def _canonical_notion_id(value: str) -> str:
    raw = str(value).strip()
    if _NOTION_ID.fullmatch(raw) is None:
        raise SchemaRecoveryError("Journaled remote ID is invalid")
    return raw.replace("-", "").lower()


__all__ = ["SchemaRecoveryError", "SchemaRecoveryService"]
