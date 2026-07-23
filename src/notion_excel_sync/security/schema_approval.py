from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Callable

from notion_excel_sync.models import (
    SCHEMA_APPROVAL_PURPOSE,
    SchemaApprovalReceipt,
    SchemaProposalRevision,
    SchemaProposalStatus,
    canonical_json,
    schema_approval_command_hash,
)


_HMAC_DOMAIN = b"notion-excel-sync\x00notion_schema_create/v1\x00"


class SchemaApprovalError(PermissionError):
    """Raised when exact Telegram authority for schema creation is absent."""


class SchemaApprovalService:
    """Issue and verify domain-separated schema-creation receipts.

    This service deliberately does not inherit from or call the ordinary data
    :class:`ApprovalService`.  Schema authority has its own purpose, receipt
    type, nonce namespace, and lifecycle.
    """

    def __init__(
        self,
        secret: str,
        allowed_users: set[str],
        ttl_seconds: int = 1800,
        nonce_used: Callable[[str], bool] | None = None,
    ) -> None:
        if len(secret) < 24:
            raise ValueError("Schema approval secret must be at least 24 characters")
        if ttl_seconds <= 0:
            raise ValueError("Schema approval TTL must be positive")
        self._secret = secret.encode("utf-8")
        self._allowed_users = {str(user) for user in allowed_users}
        self._ttl_seconds = ttl_seconds
        self._nonce_used = nonce_used or (lambda _: False)

    @staticmethod
    def _signature_payload(receipt_data: dict[str, object]) -> bytes:
        payload = dict(receipt_data)
        payload.pop("signature", None)
        if payload.get("purpose") != SCHEMA_APPROVAL_PURPOSE:
            raise SchemaApprovalError("Schema receipt purpose is invalid")
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
        now: datetime | None = None,
    ) -> SchemaApprovalReceipt:
        now = _aware_utc(now)
        user = str(telegram_user_id)
        chat = str(chat_id)
        thread = str(thread_id)
        message = str(telegram_message_id).strip()
        if proposal.purpose != SCHEMA_APPROVAL_PURPOSE:
            raise SchemaApprovalError("Schema proposal purpose is invalid")
        if user not in self._allowed_users:
            raise SchemaApprovalError("Telegram user is not authorized for schema creation")
        if (
            user != proposal.requested_by
            or chat != proposal.chat_id
            or thread != proposal.thread_id
        ):
            raise SchemaApprovalError(
                "Schema approval must come from the request owner and original chat/thread"
            )
        if not message:
            raise SchemaApprovalError("Telegram message_id is required")
        if isinstance(telegram_update_id, bool) or telegram_update_id < 0:
            raise SchemaApprovalError("Telegram update_id is invalid")
        if proposal.status is not SchemaProposalStatus.PENDING_APPROVAL:
            raise SchemaApprovalError("Schema proposal is not awaiting final approval")
        if proposal.expires_at is None or proposal.expires_at.tzinfo is None:
            raise SchemaApprovalError("Schema proposal has no valid approval expiry")
        if now >= proposal.expires_at:
            raise SchemaApprovalError("Schema proposal has expired")
        if not hmac.compare_digest(confirmed_digest.lower(), proposal.digest):
            raise SchemaApprovalError(
                "Confirmed schema proposal digest does not match the latest revision"
            )

        unsigned: dict[str, object] = {
            "schema_proposal_id": proposal.schema_proposal_id,
            "revision": proposal.revision,
            "proposal_digest": proposal.digest,
            "precondition_digest": proposal.precondition_digest,
            "telegram_user_id": user,
            "chat_id": chat,
            "thread_id": thread,
            "telegram_message_id": message,
            "telegram_update_id": int(telegram_update_id),
            "command_hash": schema_approval_command_hash(
                proposal.schema_proposal_id,
                proposal.revision,
                proposal.digest,
            ),
            "issued_at": now,
            "expires_at": now + timedelta(seconds=self._ttl_seconds),
            "nonce": token_hex(16),
            "signature": "",
            "purpose": SCHEMA_APPROVAL_PURPOSE,
        }
        unsigned["signature"] = self._sign(unsigned)
        purpose = unsigned.pop("purpose")
        assert purpose == SCHEMA_APPROVAL_PURPOSE
        return SchemaApprovalReceipt(**unsigned)  # type: ignore[arg-type]

    def verify(
        self,
        receipt: SchemaApprovalReceipt,
        proposal: SchemaProposalRevision,
        *,
        telegram_user_id: str,
        chat_id: str,
        thread_id: str,
        telegram_message_id: str,
        telegram_update_id: int,
        now: datetime | None = None,
        require_used: bool = False,
    ) -> None:
        """Verify an exact event-bound receipt before or during schema apply."""

        now = _aware_utc(now)
        self._verify_signature_and_scope(receipt, proposal)
        used = self._nonce_used(receipt.nonce)
        started_reconciliation = (
            require_used
            and used
            and proposal.status is SchemaProposalStatus.APPLYING
        )
        if now >= receipt.expires_at and not started_reconciliation:
            raise SchemaApprovalError("Schema approval receipt has expired")
        if require_used and not used:
            raise SchemaApprovalError("Schema approval receipt has not started apply")
        if not require_used and used:
            raise SchemaApprovalError("Schema approval receipt has already been used")
        expected_status = (
            SchemaProposalStatus.APPLYING
            if require_used
            else SchemaProposalStatus.APPROVED
        )
        if proposal.status is not expected_status:
            raise SchemaApprovalError(
                f"Schema proposal is not in {expected_status.value} state"
            )
        if (
            receipt.telegram_user_id != str(telegram_user_id)
            or receipt.chat_id != str(chat_id)
            or receipt.thread_id != str(thread_id)
            or receipt.telegram_message_id != str(telegram_message_id).strip()
            or receipt.telegram_update_id != telegram_update_id
        ):
            raise SchemaApprovalError(
                "Schema approval Telegram event identity does not match the receipt"
            )

    def verify_operation_scope(
        self,
        receipt: SchemaApprovalReceipt,
        proposal: SchemaProposalRevision,
        operation_id: str,
    ) -> bool:
        """Verify the sole exact schema operation after its nonce was consumed."""

        try:
            self._verify_signature_and_scope(receipt, proposal)
        except SchemaApprovalError:
            return False
        return (
            proposal.status is SchemaProposalStatus.APPLYING
            and self._nonce_used(receipt.nonce)
            and operation_id == proposal.operation_id
        )

    def _verify_signature_and_scope(
        self,
        receipt: SchemaApprovalReceipt,
        proposal: SchemaProposalRevision,
    ) -> None:
        if not isinstance(receipt, SchemaApprovalReceipt):
            raise SchemaApprovalError("A schema-specific approval receipt is required")
        expected_signature = self._sign(asdict(receipt))
        if not hmac.compare_digest(expected_signature, receipt.signature):
            raise SchemaApprovalError("Schema approval receipt signature is invalid")
        if receipt.telegram_user_id not in self._allowed_users:
            raise SchemaApprovalError("Schema approval user is no longer authorized")
        if receipt.issued_at.tzinfo is None or receipt.expires_at.tzinfo is None:
            raise SchemaApprovalError("Schema approval receipt timestamps are invalid")
        if receipt.expires_at <= receipt.issued_at:
            raise SchemaApprovalError("Schema approval receipt validity window is invalid")
        if (
            proposal.purpose != SCHEMA_APPROVAL_PURPOSE
            or receipt.purpose != SCHEMA_APPROVAL_PURPOSE
            or receipt.schema_proposal_id != proposal.schema_proposal_id
            or receipt.revision != proposal.revision
            or not hmac.compare_digest(receipt.proposal_digest, proposal.digest)
            or not hmac.compare_digest(
                receipt.precondition_digest,
                proposal.precondition_digest,
            )
            or receipt.telegram_user_id != proposal.requested_by
            or receipt.chat_id != proposal.chat_id
            or receipt.thread_id != proposal.thread_id
            or not hmac.compare_digest(
                receipt.command_hash,
                schema_approval_command_hash(
                    proposal.schema_proposal_id,
                    proposal.revision,
                    proposal.digest,
                ),
            )
        ):
            raise SchemaApprovalError(
                "Schema approval receipt does not match the exact proposal scope"
            )


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise SchemaApprovalError("Schema approval time must be timezone-aware")
    return current.astimezone(UTC)
