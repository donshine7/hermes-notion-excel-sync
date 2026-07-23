from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from notion_excel_sync.adapters.local_files import HydrationPolicy
from notion_excel_sync.config import SourceConfig, WikiConfig
from notion_excel_sync.knowledge.projection import WikiProjection
from notion_excel_sync.workflow.local_wiki import (
    LOCAL_WIKI_CHECKPOINT_KEY,
    LocalWikiConfigurationError,
    LocalWikiRefreshOrchestrator,
)


class _Checkpoints:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.saved: list[tuple[str, dict[str, Any], str | None]] = []
        self.events: list[str] = []

    def get_ingestion_checkpoint(self, stream_key: str) -> dict[str, Any] | None:
        assert stream_key == LOCAL_WIKI_CHECKPOINT_KEY
        return self.payload

    def save_ingestion_checkpoint(
        self,
        stream_key: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> None:
        self.events.append("checkpoint")
        self.payload = dict(payload)
        self.saved.append((stream_key, dict(payload), actor))


@dataclass
class _Dependencies:
    checkpoints: _Checkpoints
    hydration_policies: list[HydrationPolicy]
    force_full: list[bool]
    events: list[str]
    fail_publish: bool = False

    def adapter_factory(
        self,
        source_root: Path,
        runtime_dir: Path,
        *,
        drive_id: str,
        hydration_policy: HydrationPolicy,
    ) -> Any:
        assert source_root.is_dir()
        assert runtime_dir.name == "directory-manifests"
        assert drive_id == "local"
        self.hydration_policies.append(hydration_policy)
        return SimpleNamespace(root_item_id="local-root")

    @staticmethod
    def index_factory(path: Path) -> Any:
        return SimpleNamespace(path=path)

    def service_factory(self, **kwargs: Any) -> Any:
        assert kwargs["config"].root_path == ""
        assert kwargs["config"].root_item_id == "local-root"
        assert kwargs["default_drive_id"] == "local"

        dependencies = self

        class Service:
            def refresh(self, *, progress: Any, force_full: bool) -> Any:
                dependencies.events.append("refresh")
                dependencies.force_full.append(force_full)
                generation = f"generation-{len(dependencies.force_full)}"
                snapshot = SimpleNamespace(
                    generation_digest=generation,
                    root_version_tag=f"root-{len(dependencies.force_full)}",
                    document_count=4,
                    quarantine_count=1,
                )
                return SimpleNamespace(
                    snapshot=snapshot,
                    catalog_entries=5,
                    full_rebuild=force_full,
                    skipped_reasons={"extension": 1},
                )

        return Service()

    def output_store_factory(self, output_root: Path, *, source_root: Path) -> Any:
        assert output_root != source_root
        dependencies = self

        class Output:
            def publish_data_status(self, **kwargs: Any) -> WikiProjection:
                dependencies.events.append("publish")
                if dependencies.fail_publish:
                    raise RuntimeError("publish failed")
                return WikiProjection(
                    layer="data",
                    generation=kwargs["generation"],
                    output_path=output_root / "data" / kwargs["generation"],
                    document_count=kwargs["indexed_documents"],
                )

        return Output()


def _configs(tmp_path: Path) -> tuple[SourceConfig, WikiConfig, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    workbook = source_root / "workbook.xlsx"
    workbook.write_bytes(b"not read by this test")
    runtime = tmp_path / "runtime"
    output = tmp_path / "output"
    source = SourceConfig(
        mode="local_filesystem",
        root_dir=source_root,
        excel_path=workbook,
        immutable=True,
        initial_auto_hydrate=True,
        steady_state_auto_hydrate=False,
        snapshot_dir=tmp_path / "snapshots",
    )
    wiki = WikiConfig(
        enabled=True,
        source_root=source_root,
        output_root=output,
        index_db=runtime / "index.db",
        initial_auto_hydrate=True,
        steady_state_auto_hydrate=False,
    )
    return source, wiki, runtime


def _orchestrator(
    tmp_path: Path,
    dependencies: _Dependencies,
) -> LocalWikiRefreshOrchestrator:
    source, wiki, runtime = _configs(tmp_path)
    return LocalWikiRefreshOrchestrator(
        source=source,
        wiki=wiki,
        checkpoints=dependencies.checkpoints,
        runtime_root=runtime,
        adapter_factory=dependencies.adapter_factory,
        index_factory=dependencies.index_factory,
        service_factory=dependencies.service_factory,
        output_store_factory=dependencies.output_store_factory,
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )


def test_initial_read_only_hydration_then_delta_with_deny(tmp_path: Path) -> None:
    checkpoints = _Checkpoints()
    dependencies = _Dependencies(checkpoints, [], [], checkpoints.events)
    orchestrator = _orchestrator(tmp_path, dependencies)

    first = orchestrator.refresh(
        baseline_at=datetime(2026, 7, 23, 9, 0, tzinfo=UTC),
        actor="telegram-user",
    )
    second = orchestrator.refresh(
        baseline_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        actor="telegram-user",
    )

    assert dependencies.hydration_policies == [
        HydrationPolicy.READ_ONLY,
        HydrationPolicy.DENY,
    ]
    assert dependencies.force_full == [True, False]
    assert first.initial is True
    assert second.initial is False
    assert second.checkpoint["baseline_at"] == first.checkpoint["baseline_at"]
    assert [item[2] for item in checkpoints.saved] == [
        "telegram-user",
        "telegram-user",
    ]
    assert checkpoints.events == [
        "refresh",
        "publish",
        "checkpoint",
        "refresh",
        "publish",
        "checkpoint",
    ]


def test_publish_failure_does_not_advance_checkpoint(tmp_path: Path) -> None:
    checkpoints = _Checkpoints()
    dependencies = _Dependencies(
        checkpoints,
        [],
        [],
        checkpoints.events,
        fail_publish=True,
    )
    orchestrator = _orchestrator(tmp_path, dependencies)

    with pytest.raises(RuntimeError, match="publish failed"):
        orchestrator.refresh(baseline_at="2026-07-23T09:00:00+09:00")

    assert checkpoints.payload is None
    assert checkpoints.saved == []
    assert checkpoints.events == ["refresh", "publish"]


@pytest.mark.parametrize("overlap", ["output", "runtime"])
def test_generated_state_cannot_overlap_source(
    tmp_path: Path,
    overlap: str,
) -> None:
    source, wiki, runtime = _configs(tmp_path)
    if overlap == "output":
        wiki.output_root = source.root_dir / "generated"  # type: ignore[operator]
    else:
        runtime = source.root_dir / "runtime"  # type: ignore[operator]
        wiki.index_db = runtime / "index.db"

    with pytest.raises(LocalWikiConfigurationError, match="must not overlap"):
        LocalWikiRefreshOrchestrator(
            source=source,
            wiki=wiki,
            checkpoints=_Checkpoints(),
            runtime_root=runtime,
        )


def test_output_and_runtime_cannot_overlap(tmp_path: Path) -> None:
    source, wiki, runtime = _configs(tmp_path)
    wiki.output_root = runtime

    with pytest.raises(LocalWikiConfigurationError, match="must not overlap"):
        LocalWikiRefreshOrchestrator(
            source=source,
            wiki=wiki,
            checkpoints=_Checkpoints(),
            runtime_root=runtime,
        )


def test_steady_state_hydration_is_rejected(tmp_path: Path) -> None:
    source, wiki, runtime = _configs(tmp_path)
    wiki.steady_state_auto_hydrate = True

    with pytest.raises(
        LocalWikiConfigurationError,
        match="Steady-state local Wiki hydration",
    ):
        LocalWikiRefreshOrchestrator(
            source=source,
            wiki=wiki,
            checkpoints=_Checkpoints(),
            runtime_root=runtime,
        )


def test_naive_baseline_is_rejected_before_source_read(tmp_path: Path) -> None:
    checkpoints = _Checkpoints()
    dependencies = _Dependencies(checkpoints, [], [], checkpoints.events)
    orchestrator = _orchestrator(tmp_path, dependencies)

    with pytest.raises(LocalWikiConfigurationError, match="explicit timezone"):
        orchestrator.refresh(baseline_at="2026-07-23T09:00:00")

    assert dependencies.hydration_policies == []
