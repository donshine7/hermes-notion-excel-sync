from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from notion_excel_sync.models import (
    ApprovalReceipt,
    JsonValue,
    ProposalAction,
    ProposalOperation,
    ProposalRevision,
    ProposalStatus,
    WritePreconditionState,
    sha256_json,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.approval import ApprovalError, ApprovalService


class NotionOperationWriter(Protocol):
    def inspect(self, operation: ProposalOperation) -> WritePreconditionState:
        """Classify an operation as pending, already applied, or conflicting."""

    def preflight(self, operation: ProposalOperation) -> bool:
        """Return true only when the current Notion value still matches the proposal."""

    def apply(self, operation: ProposalOperation, receipt: ApprovalReceipt) -> str:
        """Apply an approved operation idempotently and return the Notion page URL or ID."""


@dataclass(slots=True)
class ApplyReport:
    proposal_id: str
    applied: list[str]
    already_applied: list[str]
    excluded: list[str]
    deferred: list[str]
    failed: dict[str, str]
    committed: bool


class ApprovalGatedApplyService:
    def __init__(
        self,
        database: StateDatabase,
        approval_service: ApprovalService,
        writer: NotionOperationWriter,
        *,
        correction_overlay_publisher: (
            Callable[[ProposalRevision, ApprovalReceipt], None] | None
        ) = None,
    ) -> None:
        self.database = database
        self.approval_service = approval_service
        self.writer = writer
        self.correction_overlay_publisher = correction_overlay_publisher

    def apply(
        self,
        receipt: ApprovalReceipt,
        drive_id: str,
        item_id: str,
        current_source_version_id: str,
        current_source_file_hash: str,
        source_version_guard: Callable[[], str] | None = None,
        knowledge_binding_guard: Callable[[], Mapping[str, JsonValue]] | None = None,
    ) -> ApplyReport:
        with self.database.apply_lease(
            drive_id=drive_id,
            item_id=item_id,
            proposal_id=receipt.proposal_id,
            revision=receipt.revision,
        ) as lease_token:
            return self._apply_under_lease(
                receipt,
                drive_id,
                item_id,
                current_source_version_id,
                current_source_file_hash,
                source_version_guard,
                knowledge_binding_guard,
                lease_token,
            )

    def _apply_under_lease(
        self,
        receipt: ApprovalReceipt,
        drive_id: str,
        item_id: str,
        current_source_version_id: str,
        current_source_file_hash: str,
        source_version_guard: Callable[[], str] | None,
        knowledge_binding_guard: Callable[[], Mapping[str, JsonValue]] | None,
        lease_token: str,
    ) -> ApplyReport:
        proposal = self.database.load_proposal(receipt.proposal_id, receipt.revision)
        resuming = proposal.status is ProposalStatus.APPLYING
        if proposal.status not in {ProposalStatus.APPROVED, ProposalStatus.APPLYING}:
            raise ApprovalError("Proposal is neither approved nor resumable")
        self.approval_service.verify(
            receipt,
            proposal,
            current_source_version_id=current_source_version_id,
            current_source_file_hash=current_source_file_hash,
            consume=False,
            require_used=resuming,
        )

        def knowledge_stale_reason() -> str | None:
            """Return a non-sensitive reason code when the Wiki snapshot drifted."""

            has_wiki_citations = any(
                operation.change.knowledge_refs for operation in proposal.operations
            )
            if has_wiki_citations and not proposal.knowledge_binding:
                return "binding_missing"
            if not proposal.knowledge_binding:
                return None
            if knowledge_binding_guard is None:
                return "guard_missing"
            try:
                current_binding = dict(knowledge_binding_guard())
            except Exception:
                return "guard_error"
            if sha256_json(current_binding) != sha256_json(proposal.knowledge_binding):
                return "binding_mismatch"
            return None

        def verify_latest_knowledge(*, clean_stale_allowed: bool) -> None:
            self.database.heartbeat_apply_lease(lease_token)
            reason = knowledge_stale_reason()
            if reason is None:
                return
            # STALE is safe only before a fresh approval nonce is consumed and
            # before any operation from this proposal may have been written.
            # A resumed/in-progress apply can already contain durable Notion
            # mutations, so preserve the recovery path by marking it FAILED.
            proposal.status = (
                ProposalStatus.STALE
                if clean_stale_allowed
                else ProposalStatus.FAILED
            )
            self.database.save_proposal(proposal)
            self.database.audit(
                "proposal_stale" if clean_stale_allowed else "apply_failed",
                {"knowledge_binding": reason},
                proposal.proposal_id,
                proposal.requested_by,
            )
            raise ApprovalError("Wiki sources changed after the approval screen was prepared")

        # Fail closed before any writer interaction. A proposal that cites the
        # Wiki cannot be applied unless its exact generation/source binding can
        # still be reproduced.
        verify_latest_knowledge(clean_stale_allowed=not resuming)

        write_operations = [
            item
            for item in proposal.operations
            if item.action in {ProposalAction.APPLY, ProposalAction.EDIT}
        ]
        inspections: dict[str, WritePreconditionState] = {}
        for item in write_operations:
            self.database.heartbeat_apply_lease(lease_token)
            operation_id = item.change.operation_id
            outbox_status = self.database.outbox_status(
                proposal.proposal_id,
                proposal.revision,
                operation_id,
            )
            if resuming and outbox_status == "done":
                inspections[operation_id] = WritePreconditionState.ALREADY_APPLIED
                continue
            inspect = getattr(self.writer, "inspect", None)
            if inspect is None:
                state = (
                    WritePreconditionState.PENDING
                    if self.writer.preflight(item)
                    else WritePreconditionState.CONFLICT
                )
            else:
                state = inspect(item)
            inspections[operation_id] = state
        stale = [
            operation_id
            for operation_id, state in inspections.items()
            if state is WritePreconditionState.CONFLICT
        ]
        if stale:
            proposal.status = ProposalStatus.STALE
            self.database.save_proposal(proposal)
            self.database.audit(
                "proposal_stale",
                {"operations": stale},
                proposal.proposal_id,
                proposal.requested_by,
            )
            raise ApprovalError("Notion values changed after the approval screen was prepared")

        def verify_latest_source() -> None:
            self.database.heartbeat_apply_lease(lease_token)
            if (
                source_version_guard is not None
                and source_version_guard() != current_source_version_id
            ):
                raise ApprovalError("OneDrive source changed immediately before a write")

        verify_latest_source()
        # Perform the first per-write check before consuming the approval nonce
        # or creating outbox state, so drift here is a clean STALE/zero-write
        # outcome.
        verify_latest_knowledge(clean_stale_allowed=not resuming)
        if not resuming:
            proposal.status = ProposalStatus.APPLYING
            self.database.begin_apply(proposal, receipt.nonce)

        report = ApplyReport(
            proposal_id=proposal.proposal_id,
            applied=[],
            already_applied=[],
            excluded=[
                item.change.operation_id
                for item in proposal.operations
                if item.action is ProposalAction.EXCLUDE
            ],
            deferred=[
                item.change.operation_id
                for item in proposal.operations
                if item.action is ProposalAction.DEFER
            ],
            failed={},
            committed=False,
        )
        pending_operations: list[ProposalOperation] = []
        for operation in write_operations:
            operation_id = operation.change.operation_id
            if (
                inspections[operation_id]
                is WritePreconditionState.ALREADY_APPLIED
            ):
                if self.database.outbox_status(
                    proposal.proposal_id,
                    proposal.revision,
                    operation_id,
                ) != "done":
                    self.database.mark_outbox(operation_id, "done")
                report.already_applied.append(operation_id)
            else:
                pending_operations.append(operation)

        if resuming:
            self.database.reset_outbox_for_resume(
                proposal.proposal_id,
                proposal.revision,
                [item.change.operation_id for item in pending_operations],
            )

        for operation_index, operation in enumerate(pending_operations):
            operation_id = operation.change.operation_id
            try:
                # Revalidate immediately before every mutation. Once APPLYING
                # has started, drift is a recoverable failure rather than a
                # clean stale proposal, even if no operation in this process
                # has completed yet.
                verify_latest_knowledge(clean_stale_allowed=False)
                verify_latest_source()
                self.writer.apply(operation, receipt)
                self.database.heartbeat_apply_lease(lease_token)
                self.database.mark_outbox(operation_id, "done")
                report.applied.append(operation_id)
            except Exception as exc:  # adapter errors are persisted and surfaced to Telegram
                message = str(exc)
                self.database.mark_outbox(operation_id, "failed", message)
                report.failed[operation_id] = message
                for remaining in pending_operations[operation_index + 1 :]:
                    remaining_id = remaining.change.operation_id
                    blocked = "Not attempted after an earlier operation failed"
                    self.database.mark_outbox(remaining_id, "blocked", blocked)
                    report.failed[remaining_id] = blocked
                break

        if not report.failed:
            try:
                verify_latest_source()
            except ApprovalError as exc:
                report.failed["__source__"] = str(exc)

        if not report.failed and self.correction_overlay_publisher is not None:
            try:
                self.correction_overlay_publisher(proposal, receipt)
            except Exception as exc:
                report.failed["__wiki_overlay__"] = str(exc)

        if report.failed:
            proposal.status = ProposalStatus.FAILED
            self.database.save_proposal(proposal)
            self.database.audit(
                "apply_failed",
                {"applied": report.applied, "failed": report.failed},
                proposal.proposal_id,
                proposal.requested_by,
            )
            return report

        proposal.status = ProposalStatus.COMMITTED
        self.database.heartbeat_apply_lease(lease_token)
        self.database.finalize_success(
            proposal,
            drive_id=drive_id,
            item_id=item_id,
            version_id=current_source_version_id,
            file_hash=current_source_file_hash,
        )
        report.committed = True
        return report
