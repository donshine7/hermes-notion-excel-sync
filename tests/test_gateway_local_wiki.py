from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from notion_excel_sync.security.hermes_gateway import (
    HermesGatewayApprovalError,
    TelegramEventIdentity,
    _refresh_local_wiki_data,
)


def _identity() -> TelegramEventIdentity:
    return TelegramEventIdentity(
        user_id="123456789",
        chat_id="chat-1",
        chat_type="private",
        thread_id="",
        message_id="message-1",
        update_id=10,
        event_timestamp=datetime(2026, 7, 23, tzinfo=UTC).isoformat(),
    )


def _config(tmp_path: Path) -> SimpleNamespace:
    source_root = tmp_path / "source"
    source_root.mkdir()
    return SimpleNamespace(
        source=SimpleNamespace(mode="local_filesystem"),
        wiki=SimpleNamespace(
            source_root=source_root,
            output_root=tmp_path / "wiki-output",
            index_db=tmp_path / "runtime" / "wiki.db",
        ),
    )


def test_gateway_refreshes_local_data_before_analysis(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database = Mock()
    projection = SimpleNamespace(generation="generation-1", document_count=4)
    orchestrator = Mock()
    orchestrator.refresh.return_value = SimpleNamespace(
        projection=projection,
        initial=True,
    )
    output = Mock()
    baseline = {"t0": "2026-07-23T09:00:00+00:00"}

    with (
        patch(
            "notion_excel_sync.security.hermes_gateway."
            "LocalWikiRefreshOrchestrator",
            return_value=orchestrator,
        ) as factory,
        patch(
            "notion_excel_sync.security.hermes_gateway.WikiOutputStore",
            return_value=output,
        ) as output_factory,
    ):
        _refresh_local_wiki_data(config, database, _identity(), baseline)

    factory.assert_called_once_with(
        source=config.source,
        wiki=config.wiki,
        checkpoints=database,
        runtime_root=config.wiki.index_db.parent,
    )
    orchestrator.refresh.assert_called_once_with(
        baseline_at=baseline["t0"],
        actor="123456789",
    )
    output_factory.assert_called_once_with(
        config.wiki.output_root,
        source_root=config.wiki.source_root,
    )
    output.publish_root_readme.assert_called_once()


def test_gateway_fails_closed_when_local_data_refresh_fails(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    orchestrator = Mock()
    orchestrator.refresh.side_effect = RuntimeError("source changed")

    with (
        patch(
            "notion_excel_sync.security.hermes_gateway."
            "LocalWikiRefreshOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway.WikiOutputStore"
        ) as output_factory,
        pytest.raises(
            HermesGatewayApprovalError,
            match="Excel and Notion processing did not start",
        ),
    ):
        _refresh_local_wiki_data(
            config,
            Mock(),
            _identity(),
            {"t0": "2026-07-23T09:00:00+00:00"},
        )

    output_factory.assert_not_called()
