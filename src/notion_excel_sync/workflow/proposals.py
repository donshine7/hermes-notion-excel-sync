from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Iterable, Mapping
from uuid import uuid4

from notion_excel_sync.models import (
    ApprovalReceipt,
    CORRECTION_OVERLAY_RECOVERY_MODE,
    ProposalAction,
    ProposalOperation,
    ProposalPurpose,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    source_basis_digest,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.approval import ApprovalService
from notion_excel_sync.workflow.review_cards import (
    HISTORY_DATABASE,
    unmerged_history_operation_ids,
)


class ProposalError(ValueError):
    pass


_UNSET = object()
_OVERRIDE_RETAINED_REASON = (
    "[approved_override_retained] 동일한 Excel 셀 근거가 유지되어 "
    "이전에 승인된 사용자 수정값을 제안값으로 유지합니다."
)
_OVERRIDE_CONFLICT_REASON = (
    "[approved_override_source_conflict] 이전 사용자 수정 이후 해당 Excel "
    "셀 근거가 변경되었습니다. Excel 분석값을 유지했으며 이번 검토에서 "
    "적용·수정·제외·보류를 다시 선택해야 합니다."
)


def _append_reason(reason: str, note: str) -> str:
    normalized = str(reason).strip()
    return f"{normalized} | {note}" if normalized else note


def _is_overlay_only_correction_recovery(
    proposal: ProposalRevision,
) -> bool:
    recovery = proposal.correction_binding.get("recovery")
    return (
        proposal.purpose is ProposalPurpose.USER_CORRECTION
        and isinstance(recovery, dict)
        and recovery.get("mode") == CORRECTION_OVERLAY_RECOVERY_MODE
    )


class ProposalService:
    def __init__(
        self,
        database: StateDatabase,
        approval_service: ApprovalService | None = None,
        ttl_seconds: int = 1800,
        notion_data_sources: Mapping[str, str] | None = None,
    ) -> None:
        self.database = database
        self.approval_service = approval_service
        self.ttl_seconds = ttl_seconds
        self.notion_data_sources = notion_data_sources

    def create(
        self,
        source_version_id: str,
        source_file_hash: str,
        requested_by: str,
        chat_id: str,
        changes: Iterable[ProposedChange],
        defer_databases: set[str] | None = None,
        notion_binding: dict[str, object] | None = None,
        knowledge_binding: dict[str, object] | None = None,
        purpose: ProposalPurpose = ProposalPurpose.EXCEL_SYNC,
        proposal_id: str | None = None,
        correction_binding: dict[str, object] | None = None,
    ) -> ProposalRevision:
        deferred = defer_databases or set()
        change_list = list(changes)
        if any(item.knowledge_refs for item in change_list) and not knowledge_binding:
            raise ProposalError(
                "Wiki-backed changes require an immutable knowledge binding"
            )
        resolved_proposal_id = (
            proposal_id
            or f"P-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:8]}"
        )
        identity_operations = [
            ProposalOperation(
                change=change,
                action=(
                    ProposalAction.EDIT
                    if purpose is ProposalPurpose.USER_CORRECTION
                    else (
                        ProposalAction.DEFER
                        if change.target_database in deferred
                        else ProposalAction.APPLY
                    )
                ),
                approved_value=change.proposed_value,
                user_override=purpose is ProposalPurpose.USER_CORRECTION,
            )
            for change in change_list
        ]
        identity_context = ProposalRevision(
            proposal_id=resolved_proposal_id,
            revision=1,
            source_version_id=source_version_id,
            source_file_hash=source_file_hash,
            requested_by=str(requested_by),
            chat_id=str(chat_id),
            operations=identity_operations,
            notion_binding=dict(notion_binding or {}),
            knowledge_binding=dict(knowledge_binding or {}),
            status=ProposalStatus.DRAFT,
            purpose=purpose,
            correction_binding=dict(correction_binding or {}),
        )
        effective_changes: list[ProposedChange] = []
        operations: list[ProposalOperation] = []
        retained_override_operation_ids: set[str] = set()
        for identity_operation in identity_operations:
            change = identity_operation.change
            action = identity_operation.action
            approved_value = identity_operation.approved_value
            user_override = identity_operation.user_override
            if purpose is ProposalPurpose.EXCEL_SYNC:
                override = self.database.get_approved_override_for_operation(
                    identity_context,
                    identity_operation,
                )
                if override is not None:
                    observed_basis = (
                        source_basis_digest(change.source_refs)
                        if change.source_refs
                        else ""
                    )
                    stored_basis = str(override["source_basis_digest"])
                    change = deepcopy(change)
                    if (
                        stored_basis
                        and observed_basis
                        and stored_basis == observed_basis
                    ):
                        approved_value = override["approved_value"]
                        # This is not a new user edit, so it must not publish a
                        # duplicate correction overlay. The exact effective
                        # value remains bound by this proposal's fresh
                        # Telegram approval.
                        user_override = False
                        retained_override_operation_ids.add(
                            change.operation_id
                        )
                        change.reason = _append_reason(
                            change.reason,
                            _OVERRIDE_RETAINED_REASON,
                        )
                        if change.current_value == approved_value:
                            action = ProposalAction.EXCLUDE
                    else:
                        change.reason = _append_reason(
                            change.reason,
                            _OVERRIDE_CONFLICT_REASON,
                        )
            effective_changes.append(change)
            operations.append(
                ProposalOperation(
                    change=change,
                    action=action,
                    approved_value=approved_value,
                    user_override=user_override,
                )
            )
        change_list = effective_changes
        if notion_binding is None and self.notion_data_sources is not None:
            from notion_excel_sync.workflow.notion_writer import build_notion_binding

            notion_binding = build_notion_binding(
                change_list,
                self.notion_data_sources,
            )
        proposal = ProposalRevision(
            proposal_id=resolved_proposal_id,
            revision=1,
            source_version_id=source_version_id,
            source_file_hash=source_file_hash,
            requested_by=str(requested_by),
            chat_id=str(chat_id),
            operations=operations,
            notion_binding=dict(notion_binding or {}),
            knowledge_binding=dict(knowledge_binding or {}),
            status=ProposalStatus.DRAFT,
            purpose=purpose,
            correction_binding=dict(correction_binding or {}),
        )
        for operation in proposal.operations:
            if (
                operation.user_override
                or operation.change.operation_id
                in retained_override_operation_ids
            ):
                self._cascade_history(proposal, operation)
        self.database.save_proposal(proposal)
        self.database.audit(
            "proposal_created",
            {
                "revision": 1,
                "operations": len(proposal.operations),
                "deferred_unavailable_databases": sorted(deferred),
                "digest": proposal.digest,
                "purpose": proposal.purpose.value,
            },
            proposal.proposal_id,
            requested_by,
        )
        return proposal

    def edit(
        self,
        proposal_id: str,
        operation_id: str,
        action: ProposalAction,
        approved_value: object = _UNSET,
    ) -> ProposalRevision:
        current = self.database.load_proposal(proposal_id)
        if current.status in {
            ProposalStatus.APPROVED,
            ProposalStatus.APPLYING,
            ProposalStatus.COMMITTED,
            ProposalStatus.REJECTED,
            ProposalStatus.EXPIRED,
        }:
            raise ProposalError(f"Proposal cannot be edited in status {current.status.value}")
        if _is_overlay_only_correction_recovery(current):
            raise ProposalError(
                "An overlay-only correction recovery cannot be revised; "
                "it must be finalized with its exact approval"
            )
        if (
            current.purpose is ProposalPurpose.USER_CORRECTION
            and action not in {
                ProposalAction.EDIT,
                ProposalAction.EXCLUDE,
            }
        ):
            raise ProposalError(
                "A user-correction proposal may only be edited or excluded"
            )
        revised = deepcopy(current)
        revised.revision += 1
        revised.created_at = datetime.now(UTC)
        revised.expires_at = None
        revised.status = ProposalStatus.DRAFT
        target = next(
            (item for item in revised.operations if item.change.operation_id == operation_id), None
        )
        if target is None:
            raise ProposalError(f"Operation not found: {operation_id}")
        if target.change.target_database == HISTORY_DATABASE:
            raise ProposalError(
                "Generated history operations cannot be edited directly"
            )
        target.action = action
        target.user_override = action is ProposalAction.EDIT
        if action is ProposalAction.EDIT:
            if approved_value is _UNSET:
                raise ProposalError("Edited operations require an approved value")
            self._validate_approved_value(target, approved_value)
            target.approved_value = approved_value
        elif action is ProposalAction.APPLY:
            target.approved_value = target.change.proposed_value
        self._cascade_history(revised, target)
        self.database.save_proposal(revised)
        self.database.audit(
            "proposal_revised",
            {
                "revision": revised.revision,
                "operation_id": operation_id,
                "action": action.value,
                "digest": revised.digest,
            },
            proposal_id,
            current.requested_by,
        )
        return revised

    @staticmethod
    def _validate_approved_value(
        operation: ProposalOperation,
        approved_value: object,
    ) -> None:
        """Reject edits that cannot be encoded for the declared Notion schema."""

        from notion_excel_sync.workflow.notion_writer import NOTION_DEFINITIONS

        change = operation.change
        definition = NOTION_DEFINITIONS.get(change.target_database)
        if definition is None:
            raise ProposalError(f"Unknown target database: {change.target_database}")
        property_type = definition.properties.get(change.property_name)
        if property_type is None:
            raise ProposalError(
                f"Unknown target property: {change.target_database}.{change.property_name}"
            )
        if (
            change.property_name == definition.title_property
            and approved_value in (None, "")
        ):
            raise ProposalError("A Notion title cannot be empty")
        if approved_value is None:
            return
        if property_type == "number":
            if isinstance(approved_value, bool) or not isinstance(
                approved_value, (int, float)
            ):
                raise ProposalError("A Notion number edit must be a JSON number or null")
            return
        if property_type == "checkbox":
            if not isinstance(approved_value, bool):
                raise ProposalError("A Notion checkbox edit must be true or false")
            return
        if property_type in {"multi_select", "relation"}:
            values = approved_value if isinstance(approved_value, list) else [approved_value]
            if not all(isinstance(item, (str, int)) for item in values):
                raise ProposalError(
                    f"A Notion {property_type} edit must contain string identifiers"
                )
            return
        if property_type in {
            "title",
            "rich_text",
            "select",
            "date",
            "url",
            "email",
            "phone_number",
        } and not isinstance(approved_value, str):
            raise ProposalError(f"A Notion {property_type} edit must be a JSON string or null")

    @staticmethod
    def _cascade_history(
        proposal: ProposalRevision, source_operation: ProposalOperation
    ) -> None:
        """Keep generated history in the exact scope of its source operation."""

        change = source_operation.change
        if change.target_database == "변경이력":
            return
        history_key = f"history:{change.operation_id}"
        for history in proposal.operations:
            if (
                history.change.target_database != "변경이력"
                or history.change.entity_key != history_key
            ):
                continue
            if source_operation.action in {
                ProposalAction.EXCLUDE,
                ProposalAction.DEFER,
            }:
                history.action = source_operation.action
                history.user_override = False
                continue
            history.action = ProposalAction.APPLY
            history.approved_value = history.change.proposed_value
            if history.change.property_name == "새값":
                history.user_override = source_operation.user_override
                history.approved_value = _display_value(source_operation.approved_value)
            else:
                history.user_override = False

    def request_approval(self, proposal_id: str) -> ProposalRevision:
        proposal = self.database.load_proposal(proposal_id)
        if proposal.status not in {ProposalStatus.DRAFT, ProposalStatus.PENDING_APPROVAL}:
            raise ProposalError(f"Cannot request approval in status {proposal.status.value}")
        unmerged_history = unmerged_history_operation_ids(
            proposal.operations
        )
        if unmerged_history:
            raise ProposalError(
                "Proposal contains generated history operations that are not "
                "displayed on any review card"
            )
        proposal.status = ProposalStatus.PENDING_APPROVAL
        proposal.expires_at = datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)
        self.database.save_proposal(proposal)
        self.database.audit(
            "approval_requested",
            {
                "revision": proposal.revision,
                "digest": proposal.digest,
                "expires_at": proposal.expires_at.isoformat(),
            },
            proposal_id,
            proposal.requested_by,
        )
        return proposal

    def correction_remote_writes_complete(
        self,
        proposal: ProposalRevision,
    ) -> bool:
        """Return true only when a correction has no Notion write left to run."""

        if proposal.purpose is not ProposalPurpose.USER_CORRECTION:
            return False
        observed_completed_write = False
        for operation in proposal.operations:
            state = self.database.operation_outbox_state(
                operation.change.operation_id
            )
            if operation.action in {
                ProposalAction.APPLY,
                ProposalAction.EDIT,
            }:
                if state is None or state["status"] != "done":
                    return False
                observed_completed_write = True
                continue
            if (
                operation.user_override
                and state is not None
                and state["status"] == "done"
            ):
                observed_completed_write = True
        return observed_completed_write

    def create_recovery_revision(
        self,
        proposal_id: str,
        *,
        source_rebind: tuple[str, str] | None = None,
    ) -> ProposalRevision:
        current = self.database.load_proposal(proposal_id)
        if current.status is not ProposalStatus.FAILED:
            raise ProposalError("Only a failed apply can create a recovery revision")
        overlay_only_recovery = source_rebind is not None
        if overlay_only_recovery:
            if (
                len(source_rebind) != 2
                or not all(
                    isinstance(value, str) and value.strip()
                    for value in source_rebind
                )
            ):
                raise ProposalError("Recovery source rebind is invalid")
            if not self.correction_remote_writes_complete(current):
                raise ProposalError(
                    "Only a fully applied independent correction may rebind "
                    "its recovery source"
                )
        revised = deepcopy(current)
        revised.revision += 1
        revised.created_at = datetime.now(UTC)
        revised.expires_at = None
        revised.status = ProposalStatus.DRAFT
        if overlay_only_recovery:
            assert source_rebind is not None
            revised.source_version_id, revised.source_file_hash = source_rebind
            revised.knowledge_binding = {}
            for operation in revised.operations:
                operation.change.source_refs = []
                operation.change.knowledge_refs = []
            revised.correction_binding["recovery"] = {
                "mode": CORRECTION_OVERLAY_RECOVERY_MODE,
                "notion_writes": "already_done",
                "source_rebound": True,
            }
        skipped_done: list[str] = []
        retried: list[str] = []
        for operation in revised.operations:
            if operation.action not in {ProposalAction.APPLY, ProposalAction.EDIT}:
                continue
            state = self.database.operation_outbox_state(
                operation.change.operation_id
            )
            if state and state["status"] == "done":
                operation.action = ProposalAction.EXCLUDE
                skipped_done.append(operation.change.operation_id)
            else:
                retried.append(operation.change.operation_id)
        self.database.save_proposal(revised)
        self.database.audit(
            "recovery_revision_created",
            {
                "revision": revised.revision,
                "skipped_done": skipped_done,
                "retried": retried,
                "digest": revised.digest,
            },
            revised.proposal_id,
            revised.requested_by,
        )
        return revised

    def approve(
        self,
        proposal_id: str,
        revision: int,
        telegram_user_id: str,
        chat_id: str,
        digest: str,
    ) -> ApprovalReceipt:
        if self.approval_service is None:
            raise ProposalError(
                "Approval receipts may be issued only by the trusted Hermes gateway"
            )
        proposal = self.database.load_proposal(proposal_id)
        if unmerged_history_operation_ids(proposal.operations):
            raise ProposalError(
                "Proposal contains generated history operations that are not "
                "displayed on any review card"
            )
        if proposal.revision != revision:
            raise ProposalError(
                "Only the current final proposal revision can be approved"
            )
        receipt = self.approval_service.issue(
            proposal,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            confirmed_digest=digest,
        )
        self.database.save_receipt(receipt)
        proposal.status = ProposalStatus.APPROVED
        self.database.save_proposal(proposal)
        self.database.audit(
            "proposal_approved",
            {"revision": revision, "digest": digest, "nonce": receipt.nonce},
            proposal_id,
            telegram_user_id,
        )
        return receipt

    def reject(
        self,
        proposal_id: str,
        actor: str,
        chat_id: str | None = None,
    ) -> None:
        proposal = self.database.load_proposal(proposal_id)
        if str(actor) != proposal.requested_by:
            raise ProposalError("Only the request owner can reject the proposal")
        if chat_id is not None and str(chat_id) != proposal.chat_id:
            raise ProposalError("Proposal must be rejected from the original Telegram chat")
        if any(
            (
                state := self.database.operation_outbox_state(
                    operation.change.operation_id
                )
            )
            and state["proposal_id"] == proposal.proposal_id
            for operation in proposal.operations
        ):
            raise ProposalError(
                "A proposal with a started apply must be recovered and finalized "
                "before it can close"
            )
        if proposal.status in {
            ProposalStatus.APPLYING,
            ProposalStatus.COMMITTED,
            ProposalStatus.REJECTED,
        }:
            raise ProposalError(f"Proposal cannot be rejected in {proposal.status.value}")
        proposal.status = ProposalStatus.REJECTED
        self.database.save_proposal(proposal)
        self.database.audit("proposal_rejected", {}, proposal_id, actor)


def _display_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
