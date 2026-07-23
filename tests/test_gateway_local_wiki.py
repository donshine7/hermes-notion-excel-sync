from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from notion_excel_sync.config import EmailConfig, WikiConfig
from notion_excel_sync.security.hermes_gateway import (
    HermesGatewayApprovalError,
    TelegramEventIdentity,
    _prepare_authenticated_sync,
    _refresh_hiworks_mail,
    _refresh_local_wiki_data,
)
from notion_excel_sync.workflow.mail_ingestion import (
    HiworksMailIngestionResult,
    HiworksMailIngestionStatus,
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


def test_gateway_does_not_resolve_mail_secret_before_bootstrap_is_complete(
    tmp_path: Path,
) -> None:
    secret = Mock(side_effect=AssertionError("mail secret must not be read"))
    config = SimpleNamespace(
        email=EmailConfig(enabled=True),
        wiki=WikiConfig(
            enabled=True,
            source_root=tmp_path / "source",
            output_root=tmp_path / "wiki-output",
        ),
        secret=secret,
    )
    database = Mock()
    database.get_ingestion_checkpoint.return_value = {
        "phase": "started",
        "t0": "2026-07-23T09:00:00+00:00",
    }

    _refresh_hiworks_mail(
        config,
        database,
        _identity(),
        {"phase": "started", "t0": "2026-07-23T09:00:00+00:00"},
    )

    secret.assert_not_called()
    database.save_ingestion_checkpoint.assert_not_called()


def test_gateway_runs_hiworks_ingestion_for_completed_bootstrap(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        email=EmailConfig(enabled=True),
        wiki=WikiConfig(
            enabled=True,
            source_root=tmp_path / "source",
            output_root=tmp_path / "wiki-output",
        ),
        secret=Mock(return_value="protected-password"),
    )
    service = Mock()
    service.ingest.return_value = HiworksMailIngestionResult(
        status=HiworksMailIngestionStatus.COMPLETED,
        new_count=2,
        generation="mail-generation-1",
        remaining=0,
    )

    with patch(
        "notion_excel_sync.security.hermes_gateway.HiworksMailIngestionService",
        return_value=service,
    ) as service_type:
        _refresh_hiworks_mail(
            config,
            Mock(),
            _identity(),
            {"phase": "complete", "t0": "2026-07-23T09:00:00+00:00"},
        )

    assert service_type.call_args.kwargs["email"] is config.email
    assert service_type.call_args.kwargs["wiki"] is config.wiki
    service.ingest.assert_called_once_with(actor="123456789")


def test_hiworks_failure_prevents_source_and_notion_processing() -> None:
    config = SimpleNamespace()
    database = Mock()
    baseline = {"phase": "complete", "t0": "2026-07-23T09:00:00+00:00"}

    with (
        patch(
            "notion_excel_sync.security.hermes_gateway._ensure_local_wiki_baseline",
            return_value=baseline,
        ),
        patch(
            "notion_excel_sync.security.hermes_gateway._refresh_local_wiki_data"
        ) as data_refresh,
        patch(
            "notion_excel_sync.security.hermes_gateway._refresh_hiworks_mail",
            side_effect=HermesGatewayApprovalError("mail failed"),
        ) as mail_refresh,
        patch(
            "notion_excel_sync.security.hermes_gateway._source_client"
        ) as source_client,
        pytest.raises(HermesGatewayApprovalError, match="mail failed"),
    ):
        _prepare_authenticated_sync(config, database, _identity())

    data_refresh.assert_called_once_with(config, database, _identity(), baseline)
    mail_refresh.assert_called_once_with(config, database, _identity(), baseline)
    source_client.assert_not_called()
