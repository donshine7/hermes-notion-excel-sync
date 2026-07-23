from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Callable

from notion_excel_sync.models import (
    ApprovalReceipt,
    ProposalAction,
    ProposalRevision,
    ProposalStatus,
    canonical_json,
    sha256_json,
)


class ApprovalError(PermissionError):
    """Raised when a Notion write is not covered by exact user approval."""


class ApprovalService:
    def __init__(
        self,
        secret: str,
        allowed_users: set[str],
        ttl_seconds: int = 1800,
        nonce_used: Callable[[str], bool] | None = None,
        mark_nonce_used: Callable[[str], None] | None = None,
    ) -> None:
        if len(secret) < 24:
            raise ValueError("Approval secret must be at least 24 characters")
        self._secret = secret.encode("utf-8")
        self._allowed_users = {str(user) for user in allowed_users}
        self._ttl_seconds = ttl_seconds
        self._nonce_used = nonce_used or (lambda _: False)
        self._mark_nonce_used = mark_nonce_used or (lambda _: None)

    def _signature_payload(self, receipt_data: dict[str, object]) -> bytes:
        payload = dict(receipt_data)
        payload.pop("signature", None)
        return canonical_json(payload).encode("utf-8")

    def _sign(self, receipt_data: dict[str, object]) -> str:
        return hmac.new(
            self._secret, self._signature_payload(receipt_data), hashlib.sha256
        ).hexdigest()

    def issue(
        self,
        proposal: ProposalRevision,
        telegram_user_id: str,
        chat_id: str,
        confirmed_digest: str,
        now: datetime | None = None,
    ) -> ApprovalReceipt:
        now = now or datetime.now(UTC)
        user = str(telegram_user_id)
        if user not in self._allowed_users:
            raise ApprovalError("Telegram user is not authorized")
        if user != proposal.requested_by or str(chat_id) != proposal.chat_id:
            raise ApprovalError("Approval must come from the request owner and original chat")
        if proposal.status is not ProposalStatus.PENDING_APPROVAL:
            raise ApprovalError("Proposal is not awaiting final approval")
        if proposal.expires_at and now >= proposal.expires_at:
            raise ApprovalError("Proposal has expired")
        if not hmac.compare_digest(confirmed_digest, proposal.digest):
            raise ApprovalError("Confirmed proposal digest does not match the latest revision")

        return self._new_receipt(proposal, user, str(chat_id), now)

    def _new_receipt(
        self,
        proposal: ProposalRevision,
        user: str,
        chat_id: str,
        now: datetime,
    ) -> ApprovalReceipt:
        unsigned = {
            "proposal_id": proposal.proposal_id,
            "revision": proposal.revision,
            "proposal_digest": proposal.digest,
            "source_version_id": proposal.source_version_id,
            "source_file_hash": proposal.source_file_hash,
            "telegram_user_id": user,
            "chat_id": chat_id,
            "issued_at": now,
            "expires_at": now + timedelta(seconds=self._ttl_seconds),
            "nonce": token_hex(16),
            "signature": "",
            "notion_binding_digest": sha256_json(proposal.notion_binding),
        }
        unsigned["signature"] = self._sign(unsigned)
        return ApprovalReceipt(**unsigned)

    def reissue_expired_approved(
        self,
        previous: ApprovalReceipt,
        proposal: ProposalRevision,
        telegram_user_id: str,
        chat_id: str,
        confirmed_digest: str,
        now: datetime | None = None,
    ) -> ApprovalReceipt:
        """Create a fresh receipt for an unchanged APPROVED proposal.

        The caller must atomically revoke ``previous`` while persisting the new
        receipt. This method permits only an expired, unused, authentic receipt;
        a consumed receipt belongs to APPLYING reconciliation instead.
        """

        now = now or datetime.now(UTC)
        user = str(telegram_user_id)
        if proposal.status is not ProposalStatus.APPROVED:
            raise ApprovalError("Only an approved proposal can refresh its receipt")
        if user not in self._allowed_users:
            raise ApprovalError("Telegram user is not authorized")
        if user != proposal.requested_by or str(chat_id) != proposal.chat_id:
            raise ApprovalError("Approval must come from the request owner and original chat")
        if now < previous.expires_at:
            raise ApprovalError("The existing approval receipt has not expired")
        if self._nonce_used(previous.nonce):
            raise ApprovalError("A consumed approval receipt cannot be reissued")
        if not hmac.compare_digest(self._sign(asdict(previous)), previous.signature):
            raise ApprovalError("Existing approval receipt signature is invalid")
        if (
            previous.proposal_id != proposal.proposal_id
            or previous.revision != proposal.revision
            or previous.source_version_id != proposal.source_version_id
            or previous.source_file_hash != proposal.source_file_hash
            or previous.telegram_user_id != proposal.requested_by
            or previous.chat_id != proposal.chat_id
            or not hmac.compare_digest(
                previous.notion_binding_digest,
                sha256_json(proposal.notion_binding),
            )
            or not hmac.compare_digest(previous.proposal_digest, proposal.digest)
        ):
            raise ApprovalError("Existing approval receipt does not match the proposal")
        if not hmac.compare_digest(confirmed_digest, proposal.digest):
            raise ApprovalError("Confirmed proposal digest does not match the latest revision")
        return self._new_receipt(proposal, user, str(chat_id), now)

    def verify(
        self,
        receipt: ApprovalReceipt,
        proposal: ProposalRevision,
        current_source_version_id: str,
        current_source_file_hash: str,
        now: datetime | None = None,
        consume: bool = False,
        require_used: bool = False,
    ) -> None:
        now = now or datetime.now(UTC)
        receipt_data = asdict(receipt)
        expected = self._sign(receipt_data)
        if not hmac.compare_digest(expected, receipt.signature):
            raise ApprovalError("Approval receipt signature is invalid")
        if receipt.telegram_user_id not in self._allowed_users:
            raise ApprovalError("Approval user is no longer authorized")
        nonce_used = self._nonce_used(receipt.nonce)
        is_started_reconciliation = (
            require_used
            and nonce_used
            and proposal.status is ProposalStatus.APPLYING
        )
        if now >= receipt.expires_at and not is_started_reconciliation:
            raise ApprovalError("Approval receipt has expired")
        if require_used and not nonce_used:
            raise ApprovalError("Approval receipt has not started an apply run")
        if not require_used and nonce_used:
            raise ApprovalError("Approval receipt has already been used")
        if receipt.proposal_id != proposal.proposal_id or receipt.revision != proposal.revision:
            raise ApprovalError("Approval targets a different proposal revision")
        if not hmac.compare_digest(receipt.proposal_digest, proposal.digest):
            raise ApprovalError("Proposal changed after approval")
        if not hmac.compare_digest(
            receipt.notion_binding_digest,
            sha256_json(proposal.notion_binding),
        ):
            raise ApprovalError("Notion destination binding changed after approval")
        if (
            receipt.source_version_id != current_source_version_id
            or receipt.source_file_hash != current_source_file_hash
        ):
            raise ApprovalError("Configured source changed after approval")
        if receipt.telegram_user_id != proposal.requested_by or receipt.chat_id != proposal.chat_id:
            raise ApprovalError("Approval routing context does not match the proposal")
        if consume:
            if require_used:
                raise ApprovalError("A started approval receipt cannot be consumed again")
            self._mark_nonce_used(receipt.nonce)

    def verify_operation_scope(
        self,
        receipt: ApprovalReceipt,
        proposal: ProposalRevision,
        operation_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Verify signature and exact operation scope after the nonce was consumed.

        The workflow consumes a receipt immediately before the first mutation. Notion
        adapters call this narrower check for every subsequent API mutation.
        """

        expected = self._sign(asdict(receipt))
        if not hmac.compare_digest(expected, receipt.signature):
            return False
        nonce_used = self._nonce_used(receipt.nonce)
        if not nonce_used or proposal.status is not ProposalStatus.APPLYING:
            return False
        # Expiry limits entering APPLYING, not finishing its exact outbox scope.
        # The consumed nonce and APPLYING state above are therefore mandatory,
        # while an expired receipt remains eligible for reconciliation here. The
        # optional `now` argument is retained for API compatibility but deliberately
        # does not shorten an already-started transaction.
        if receipt.telegram_user_id not in self._allowed_users:
            return False
        if receipt.proposal_id != proposal.proposal_id or receipt.revision != proposal.revision:
            return False
        if not hmac.compare_digest(receipt.proposal_digest, proposal.digest):
            return False
        if not hmac.compare_digest(
            receipt.notion_binding_digest,
            sha256_json(proposal.notion_binding),
        ):
            return False
        if receipt.telegram_user_id != proposal.requested_by or receipt.chat_id != proposal.chat_id:
            return False
        return any(
            operation.change.operation_id == operation_id
            and operation.action in {ProposalAction.APPLY, ProposalAction.EDIT}
            for operation in proposal.operations
        )
