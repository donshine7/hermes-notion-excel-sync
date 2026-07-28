from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from notion_excel_sync.models import (
    ProposalAction,
    ProposalPurpose,
    ProposalStatus,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.hermes_gateway import (
    TelegramEventIdentity,
    _prepare_correction_proposal,
)
from notion_excel_sync.workflow.corrections import CorrectionRequest
from notion_excel_sync.workflow.notion_writer import (
    ApprovedNotionWriter,
    NotionSchemaError,
    build_correction_target_binding,
)


def _identity() -> TelegramEventIdentity:
    return TelegramEventIdentity(
        user_id="synthetic-user",
        chat_id="synthetic-chat",
        chat_type="private",
        thread_id="",
        message_id="synthetic-message",
        update_id=101,
        event_timestamp=datetime(2026, 7, 23, tzinfo=UTC).isoformat(),
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        onedrive=SimpleNamespace(drive_id="local-drive", item_id="workbook"),
        notion=SimpleNamespace(databases={"한국 특허 사건": "cases"}),
        approval=SimpleNamespace(ttl_seconds=1800),
    )


def _notion_page() -> dict[str, object]:
    return {
        "id": "synthetic-page",
        "last_edited_time": "2026-07-23T00:00:00.000Z",
        "parent": {
            "type": "data_source_id",
            "data_source_id": "cases",
        },
        "properties": {
            "당소 사건번호": {
                "type": "title",
                "title": [{"plain_text": "SS-SYNTHETIC-001"}],
            },
            "현재상태": {
                "type": "select",
                "select": {"name": "진행"},
            },
        },
    }


def _remote_schema() -> dict[str, object]:
    return {
        "properties": {
            "당소 사건번호": {
                "id": "title",
                "type": "title",
                "title": {},
            },
            "현재상태": {
                "id": "status",
                "type": "select",
                "select": {
                    "options": [
                        {"name": "진행"},
                        {"name": "보류"},
                    ]
                },
            },
        }
    }


def test_correction_target_parent_must_match_configured_destination() -> None:
    page = {
        **_notion_page(),
        "parent": {
            "type": "database_id",
            "database_id": "wrong-database",
        },
    }

    with pytest.raises(NotionSchemaError, match="different data source"):
        build_correction_target_binding(
            page,
            database_name="한국 특허 사건",
            data_source_id="cases",
            entity_key="SS-SYNTHETIC-001",
            property_name="현재상태",
        )


def test_correction_target_accepts_matching_legacy_database_parent() -> None:
    page = {
        **_notion_page(),
        "parent": {
            "type": "database_id",
            "database_id": "cases",
        },
    }

    binding = build_correction_target_binding(
        page,
        database_name="한국 특허 사건",
        data_source_id="cases",
        entity_key="SS-SYNTHETIC-001",
        property_name="현재상태",
    )

    assert binding["parent"] == {"type": "database_id", "id": "cases"}


def test_correction_preparation_is_read_only_idempotent_and_source_bound(
    tmp_path,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    request = CorrectionRequest(
        database="한국 특허 사건",
        entity_key="internal:synthetic-row-1",
        property_name="현재상태",
        value="보류",
        reason="사용자가 현재 상태를 직접 확인했습니다.",
        case_number="SS-SYNTHETIC-001",
    )
    notion = Mock()
    notion.find_page.return_value = _notion_page()
    notion.get_page.return_value = _notion_page()
    notion.get_data_source.return_value = _remote_schema()
    source = Mock()

    with (
        patch(
            "notion_excel_sync.security.hermes_gateway._source_client",
            return_value=source,
        ) as source_factory,
        patch(
            "notion_excel_sync.security.hermes_gateway._capture_current_source",
            return_value=("sha256:source-version", "a" * 64),
        ) as capture,
        patch(
            "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
            return_value=notion,
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._configured_data_sources",
            return_value={"한국 특허 사건": "cases"},
        ),
    ):
        proposal = _prepare_correction_proposal(
            _config(),
            database,
            _identity(),
            request,
        )
        repeated = _prepare_correction_proposal(
            _config(),
            database,
            _identity(),
            request,
        )

    assert repeated.proposal_id == proposal.proposal_id
    assert repeated.digest == proposal.digest
    assert proposal.status is ProposalStatus.PENDING_APPROVAL
    assert proposal.purpose is ProposalPurpose.USER_CORRECTION
    assert proposal.source_version_id == "sha256:source-version"
    assert proposal.source_file_hash == "a" * 64
    assert proposal.correction_binding["request_digest"] == request.digest
    assert proposal.correction_binding["case_number"] == "SS-SYNTHETIC-001"
    assert proposal.correction_binding["target"] == {
        "version": 1,
        "database": "한국 특허 사건",
        "data_source_id": "cases",
        "entity_key": "internal:synthetic-row-1",
        "property": "현재상태",
        "page_id": "synthetic-page",
        "parent": {"type": "data_source_id", "id": "cases"},
    }
    operation = proposal.operations[0]
    assert operation.action is ProposalAction.EDIT
    assert operation.user_override is True
    assert operation.change.current_value == "진행"
    assert operation.approved_value == "보류"
    assert operation.change.source_refs == []
    assert database.get_checkpoint("local-drive", "workbook") is None
    source_factory.assert_called_once()
    capture.assert_called_once_with(source, "local-drive", "workbook")
    assert notion.find_page.call_count == 1
    assert notion.find_page.call_args.args[2] == "SS-SYNTHETIC-001"
    notion.get_page.assert_called_once_with("synthetic-page")
    assert notion.upsert_page.call_count == 0
    with database.session() as connection:
        claims = connection.execute(
            "SELECT COUNT(*) FROM correction_gateway_event_claim"
        ).fetchone()[0]
        scope = connection.execute(
            "SELECT case_number, entity_key FROM mutation_scope_claim"
        ).fetchone()
    assert claims == 1
    assert scope["case_number"] == "SS-SYNTHETIC-001"
    assert scope["entity_key"] == "internal:synthetic-row-1"

    notion.upsert_page.return_value = SimpleNamespace(
        page_id="synthetic-page",
        created=False,
        page=_notion_page(),
    )
    writer = ApprovedNotionWriter(
        notion,
        database,
        proposal,
        {"한국 특허 사건": "cases"},
        require_exact_schema=True,
    )
    writer.apply(operation, Mock())
    assert notion.upsert_page.call_args.args[2] == "SS-SYNTHETIC-001"
    assert notion.upsert_page.call_args.kwargs["precondition"].page_id == (
        "synthetic-page"
    )


def test_correction_preparation_rejects_missing_notion_item(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    notion = Mock()
    notion.find_page.return_value = None
    request = CorrectionRequest(
        database="한국 특허 사건",
        entity_key="SS-MISSING",
        property_name="현재상태",
        value="보류",
        reason="합성 누락 항목 확인",
    )

    with (
        patch(
            "notion_excel_sync.security.hermes_gateway._source_client",
            return_value=Mock(),
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._capture_current_source",
            return_value=("sha256:source-version", "a" * 64),
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
            return_value=notion,
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._configured_data_sources",
            return_value={"한국 특허 사건": "cases"},
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway."
            "ApprovedNotionWriter"
        ) as writer,
    ):
        try:
            _prepare_correction_proposal(
                _config(),
                database,
                _identity(),
                request,
            )
        except PermissionError as exc:
            assert "was not found" in str(exc)
        else:
            raise AssertionError("missing Notion item was accepted")

    writer.assert_not_called()
    with database.session() as connection:
        proposals = connection.execute(
            "SELECT COUNT(*) FROM proposal"
        ).fetchone()[0]
    assert proposals == 0


@pytest.mark.parametrize(
    "replacement",
    [
        {
            **_notion_page(),
            "id": "replacement-page",
        },
        {
            **_notion_page(),
            "parent": {
                "type": "data_source_id",
                "data_source_id": "other-cases",
            },
        },
    ],
    ids=["page-id-changed", "parent-changed"],
)
def test_correction_writer_rejects_bound_page_replacement(
    tmp_path,
    replacement,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    request = CorrectionRequest(
        database="한국 특허 사건",
        entity_key="SS-SYNTHETIC-001",
        property_name="현재상태",
        value="보류",
        reason="사용자가 현재 상태를 직접 확인했습니다.",
    )
    preparation_gateway = Mock()
    preparation_gateway.find_page.return_value = _notion_page()
    preparation_gateway.get_page.return_value = _notion_page()
    preparation_gateway.get_data_source.return_value = _remote_schema()

    with (
        patch(
            "notion_excel_sync.security.hermes_gateway._source_client",
            return_value=Mock(),
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._capture_current_source",
            return_value=("sha256:source-version", "a" * 64),
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._notion_read_gateway",
            return_value=preparation_gateway,
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._configured_data_sources",
            return_value={"한국 특허 사건": "cases"},
        ),
    ):
        proposal = _prepare_correction_proposal(
            _config(),
            database,
            _identity(),
            request,
        )

    replacement_gateway = Mock()
    replacement_gateway.get_page.return_value = replacement
    replacement_gateway.get_data_source.return_value = _remote_schema()
    writer = ApprovedNotionWriter(
        replacement_gateway,
        database,
        proposal,
        {"한국 특허 사건": "cases"},
        require_exact_schema=True,
    )

    with pytest.raises(NotionSchemaError, match="identity changed|parent changed"):
        writer.inspect(proposal.operations[0])
    replacement_gateway.find_page.assert_not_called()
