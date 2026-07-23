from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from notion_excel_sync.adapters.local_directory import (
    LocalDirectoryReadOnlyAdapter,
)
from notion_excel_sync.adapters.local_files import HydrationPolicy
from notion_excel_sync.config import SourceConfig, WikiConfig
from notion_excel_sync.knowledge.index import WikiIndex
from notion_excel_sync.knowledge.projection import WikiOutputStore, WikiProjection
from notion_excel_sync.knowledge.refresh import (
    OneDriveWikiRefreshService,
    WikiRefreshReport,
)


LOCAL_WIKI_CHECKPOINT_KEY = "wiki-data-folder"
LOCAL_WIKI_DRIVE_ID = "local"


class LocalWikiConfigurationError(ValueError):
    """Raised when generated Wiki state could overlap an immutable source."""


class IngestionCheckpointStore(Protocol):
    def get_ingestion_checkpoint(self, stream_key: str) -> dict[str, Any] | None: ...

    def save_ingestion_checkpoint(
        self,
        stream_key: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LocalWikiRefreshResult:
    report: WikiRefreshReport
    projection: WikiProjection
    checkpoint: Mapping[str, Any]
    initial: bool
    hydration_policy: HydrationPolicy


def _resolved_absolute(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise LocalWikiConfigurationError(f"{label} must be absolute")
    return path.resolve(strict=False)


def _overlaps(first: Path, second: Path) -> bool:
    try:
        common = Path(os.path.commonpath((first, second)))
    except ValueError:
        return False
    normalized = os.path.normcase(os.fspath(common))
    return normalized in {
        os.path.normcase(os.fspath(first)),
        os.path.normcase(os.fspath(second)),
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _baseline_text(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise LocalWikiConfigurationError(
                "Local Wiki baseline must be an ISO-8601 timestamp"
            ) from exc
    if parsed.tzinfo is None:
        raise LocalWikiConfigurationError(
            "Local Wiki baseline must include an explicit timezone"
        )
    return parsed.astimezone(UTC).isoformat()


class LocalWikiRefreshOrchestrator:
    """Refresh a local read-only Wiki and checkpoint only its published view."""

    def __init__(
        self,
        *,
        source: SourceConfig,
        wiki: WikiConfig,
        checkpoints: IngestionCheckpointStore,
        runtime_root: Path | None = None,
        checkpoint_key: str = LOCAL_WIKI_CHECKPOINT_KEY,
        drive_id: str = LOCAL_WIKI_DRIVE_ID,
        adapter_factory: Callable[..., Any] = LocalDirectoryReadOnlyAdapter,
        index_factory: Callable[[Path], Any] = WikiIndex,
        service_factory: Callable[..., Any] = OneDriveWikiRefreshService,
        output_store_factory: Callable[..., Any] = WikiOutputStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if source.mode != "local_filesystem":
            raise LocalWikiConfigurationError(
                "Local Wiki refresh requires source.mode=local_filesystem"
            )
        if not source.immutable:
            raise LocalWikiConfigurationError("Local source must be immutable")
        if not wiki.enabled:
            raise LocalWikiConfigurationError("Local Wiki must be enabled")
        if source.root_dir is None or wiki.source_root is None:
            raise LocalWikiConfigurationError("Local Wiki source root is required")
        if wiki.output_root is None:
            raise LocalWikiConfigurationError("Local Wiki output root is required")
        if source.steady_state_auto_hydrate or wiki.steady_state_auto_hydrate:
            raise LocalWikiConfigurationError(
                "Steady-state local Wiki hydration must remain disabled"
            )
        if not checkpoint_key.strip() or not drive_id.strip():
            raise LocalWikiConfigurationError(
                "Local Wiki checkpoint key and drive ID are required"
            )

        source_root = _resolved_absolute(source.root_dir, label="source.root_dir")
        wiki_source = _resolved_absolute(
            wiki.source_root,
            label="wiki.source_root",
        )
        output_root = _resolved_absolute(
            wiki.output_root,
            label="wiki.output_root",
        )
        index_path = _resolved_absolute(wiki.index_db, label="wiki.index_db")
        runtime = _resolved_absolute(
            runtime_root or index_path.parent,
            label="Local Wiki runtime root",
        )

        if not source_root.is_dir() or not wiki_source.is_dir():
            raise LocalWikiConfigurationError(
                "Local Wiki source roots must be existing directories"
            )
        if not _is_within(wiki_source, source_root):
            raise LocalWikiConfigurationError(
                "wiki.source_root must be inside source.root_dir"
            )
        if not _is_within(index_path, runtime) or index_path == runtime:
            raise LocalWikiConfigurationError(
                "wiki.index_db must be inside the local Wiki runtime"
            )
        for first, second in (
            (source_root, output_root),
            (source_root, runtime),
            (output_root, runtime),
        ):
            if _overlaps(first, second):
                raise LocalWikiConfigurationError(
                    "Local source, Wiki output, and Wiki runtime must not overlap"
                )

        self.source = source
        self.wiki = replace(
            wiki,
            source_root=wiki_source,
            output_root=output_root,
            index_db=index_path,
        )
        self.checkpoints = checkpoints
        self.runtime_root = runtime
        self.checkpoint_key = checkpoint_key
        self.drive_id = drive_id
        self.adapter_factory = adapter_factory
        self.index_factory = index_factory
        self.service_factory = service_factory
        self.output_store_factory = output_store_factory
        self.clock = clock

    def refresh(
        self,
        *,
        baseline_at: datetime | str,
        actor: str | None = None,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> LocalWikiRefreshResult:
        previous = self.checkpoints.get_ingestion_checkpoint(self.checkpoint_key)
        initial = previous is None
        hydration_policy = (
            HydrationPolicy.READ_ONLY
            if initial and self.wiki.initial_auto_hydrate
            else HydrationPolicy.DENY
        )
        baseline = _baseline_text(
            previous.get("baseline_at", baseline_at)
            if previous is not None
            else baseline_at
        )

        adapter = self.adapter_factory(
            self.wiki.source_root,
            self.runtime_root / "directory-manifests",
            drive_id=self.drive_id,
            hydration_policy=hydration_policy,
        )
        local_config = replace(
            self.wiki,
            drive_id=self.drive_id,
            root_item_id=adapter.root_item_id,
            root_path="",
        )
        index = self.index_factory(local_config.index_db)
        service = self.service_factory(
            config=local_config,
            onedrive=adapter,
            index=index,
            default_drive_id=self.drive_id,
            work_dir=self.runtime_root / "work",
        )
        report = service.refresh(progress=progress, force_full=initial)

        output = self.output_store_factory(
            local_config.output_root,
            source_root=local_config.source_root,
        )
        projection = output.publish_data_status(
            generation=report.snapshot.generation_digest,
            baseline_at=baseline,
            catalog_entries=report.catalog_entries,
            indexed_documents=report.snapshot.document_count,
            quarantined=report.snapshot.quarantine_count,
            full_rebuild=report.full_rebuild,
            skipped_reasons=report.skipped_reasons,
        )

        now = self.clock()
        if now.tzinfo is None:
            raise RuntimeError("Local Wiki clock must return a timezone-aware datetime")
        checkpoint = {
            "schema_version": 1,
            "baseline_at": baseline,
            "generation": report.snapshot.generation_digest,
            "root_version_tag": report.snapshot.root_version_tag,
            "document_count": report.snapshot.document_count,
            "quarantine_count": report.snapshot.quarantine_count,
            "full_rebuild": report.full_rebuild,
            "source_scope_hash": hashlib.sha256(
                os.path.normcase(os.fspath(local_config.source_root)).encode("utf-8")
            ).hexdigest(),
            "updated_at": now.astimezone(UTC).isoformat(),
        }
        self.checkpoints.save_ingestion_checkpoint(
            self.checkpoint_key,
            checkpoint,
            actor=actor,
        )
        return LocalWikiRefreshResult(
            report=report,
            projection=projection,
            checkpoint=checkpoint,
            initial=initial,
            hydration_policy=hydration_policy,
        )


__all__ = [
    "LOCAL_WIKI_CHECKPOINT_KEY",
    "LOCAL_WIKI_DRIVE_ID",
    "IngestionCheckpointStore",
    "LocalWikiConfigurationError",
    "LocalWikiRefreshOrchestrator",
    "LocalWikiRefreshResult",
]
