from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from notion_excel_sync.models import (
    ApprovalReceipt,
    ChangeKind,
    ProposalAction,
    ProposalOperation,
    ProposalRevision,
    ProposedChange,
    SourceRef,
)
from notion_excel_sync.security.hermes_gateway import (
    _correction_overlay_publisher,
)


def _proposal(*, user_override: bool = True) -> ProposalRevision:
    change = ProposedChange(
        target_database="한국 특허 사건",
        entity_key="SS-SYNTHETIC-001",
        property_name="현재상태",
        kind=ChangeKind.UPDATE,
        current_value="접수",
        proposed_value="진행",
        analyzer="synthetic",
        analyzer_version="1",
        confidence=1.0,
        reason="텔레그램에서 사용자가 확인한 합성 수정입니다.",
        source_refs=[
            SourceRef(
                drive_id="local-source",
                item_id="synthetic-workbook",
                version_id="sha256:source",
                file_hash="a" * 64,
                sheet="2026",
                row=2,
                cells="H2",
                raw_value="진행",
            )
        ],
        operation_id="operation-synthetic-1",
    )
    return ProposalRevision(
        proposal_id="P-SYNTHETIC-1",
        revision=2,
        source_version_id="sha256:source",
        source_file_hash="a" * 64,
        requested_by="telegram-user",
        chat_id="telegram-chat",
        operations=[
            ProposalOperation(
                change=change,
                action=ProposalAction.EDIT,
                approved_value="사용자 승인값",
                user_override=user_override,
            )
        ],
    )


def _receipt(proposal: ProposalRevision) -> ApprovalReceipt:
    approved_at = datetime(2026, 7, 23, 12, tzinfo=UTC)
    return ApprovalReceipt(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision,
        proposal_digest=proposal.digest,
        source_version_id=proposal.source_version_id,
        source_file_hash=proposal.source_file_hash,
        telegram_user_id="synthetic-user",
        chat_id="synthetic-chat",
        issued_at=approved_at,
        expires_at=approved_at,
        nonce="not-persisted-in-overlay",
        signature="not-persisted-in-overlay",
    )


def test_gateway_builds_overlay_event_only_for_completed_user_override(
    tmp_path,
) -> None:
    config = SimpleNamespace(
        wiki=SimpleNamespace(enabled=True, output_root=tmp_path / "wiki")
    )
    database = Mock()
    database.operation_outbox_state.return_value = {"status": "done"}
    proposal = _proposal()
    store = Mock()

    with patch(
        "notion_excel_sync.security.hermes_gateway.CorrectionOverlayStore",
        return_value=store,
    ) as store_type:
        _correction_overlay_publisher(config, database)(
            proposal,
            _receipt(proposal),
        )

    store_type.assert_called_once_with(config.wiki.output_root)
    event = store.publish.call_args.args[0]
    assert event.proposal_id == proposal.proposal_id
    assert event.proposal_digest == proposal.digest
    assert event.case_number == "SS-SYNTHETIC-001"
    assert event.current_value == "접수"
    assert event.approved_value == "사용자 승인값"
    payload = event.as_dict()
    assert "nonce" not in payload
    assert "signature" not in payload


def test_gateway_does_not_publish_unedited_analysis_value(tmp_path) -> None:
    config = SimpleNamespace(
        wiki=SimpleNamespace(enabled=True, output_root=tmp_path / "wiki")
    )

    with patch(
        "notion_excel_sync.security.hermes_gateway.CorrectionOverlayStore"
    ) as store_type:
        _correction_overlay_publisher(config, Mock())(
            _proposal(user_override=False),
            _receipt(_proposal(user_override=False)),
        )

    store_type.assert_not_called()
