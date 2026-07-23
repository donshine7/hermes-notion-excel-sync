from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from notion_excel_sync.adapters.local_files import (
    HydrationPolicy,
    LocalFileError,
    LocalFileReadOnlyAdapter,
)
from notion_excel_sync.adapters.onedrive import DriveItem, DriveVersion


class LocalWorkbookError(RuntimeError):
    """Raised when a local workbook cannot be captured without ambiguity."""


@dataclass(frozen=True, slots=True)
class LocalWorkbookCapture:
    version_id: str
    file_hash: str
    captured_at: datetime
    size: int
    content: bytes


class LocalWorkbookReadOnlyClient:
    """OneDrive-compatible read facade for one immutable local source path.

    The source is opened only in binary read mode. Historical captures are
    written to a separate, Git-ignored runtime directory so a later sync can
    compare with the last committed checkpoint without modifying the workbook.
    """

    def __init__(
        self,
        source_path: str | Path,
        snapshot_dir: str | Path,
        *,
        drive_id: str,
        item_id: str,
        source_root: str | Path | None = None,
        hydration_policy: HydrationPolicy | str = HydrationPolicy.DENY,
        max_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        source = Path(source_path)
        snapshots = Path(snapshot_dir)
        if not source.is_absolute() or not snapshots.is_absolute():
            raise ValueError("Local workbook and snapshot paths must be absolute")
        if not source.is_file():
            raise LocalWorkbookError("Configured local workbook does not exist")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        source_absolute = Path(os.path.abspath(source))
        snapshots_absolute = Path(os.path.abspath(snapshots))
        try:
            common = os.path.commonpath((source_absolute, snapshots_absolute))
        except ValueError:
            common = ""
        if os.path.normcase(common) == os.path.normcase(os.fspath(source_absolute.parent)):
            raise LocalWorkbookError(
                "Snapshot storage must not be inside the source workbook directory"
            )
        self.source_path = source_absolute
        self.snapshot_dir = snapshots_absolute
        self.drive_id = drive_id
        self.item_id = item_id
        self.max_bytes = max_bytes
        try:
            self._reader = LocalFileReadOnlyAdapter(
                source_root or source_absolute.parent,
                hydration_policy=hydration_policy,
            )
        except LocalFileError as exc:
            raise LocalWorkbookError(str(exc)) from exc

    def _assert_scope(self, drive_id: str, item_id: str) -> None:
        if drive_id != self.drive_id or item_id != self.item_id:
            raise LocalWorkbookError("Local workbook source binding mismatch")

    @staticmethod
    def _hash_from_version(version_id: str) -> str:
        prefix = "sha256:"
        if not version_id.startswith(prefix):
            raise LocalWorkbookError("Invalid local workbook version identifier")
        digest = version_id[len(prefix) :]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise LocalWorkbookError("Invalid local workbook version digest")
        return digest

    def _snapshot_path(self, version_id: str) -> Path:
        return self.snapshot_dir / f"{self._hash_from_version(version_id)}.snapshot"

    def _metadata_path(self, version_id: str) -> Path:
        return self.snapshot_dir / f"{self._hash_from_version(version_id)}.json"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def capture(self) -> LocalWorkbookCapture:
        try:
            source_read = self._reader.read(self.source_path)
        except LocalFileError as exc:
            raise LocalWorkbookError(str(exc)) from exc
        content = source_read.content
        if len(content) > self.max_bytes:
            raise LocalWorkbookError("Local workbook exceeds the configured size limit")
        file_hash = source_read.manifest.revision
        version_id = f"sha256:{file_hash}"
        snapshot_path = self._snapshot_path(version_id)
        metadata_path = self._metadata_path(version_id)
        captured_at = datetime.now(UTC)
        if snapshot_path.exists() and metadata_path.exists():
            existing = snapshot_path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != file_hash:
                raise LocalWorkbookError("Cached workbook snapshot failed integrity check")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                datetime.fromisoformat(str(metadata["captured_at"]))
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                raise LocalWorkbookError("Cached workbook metadata is invalid") from exc
        else:
            self._atomic_write(snapshot_path, bytes(content))
        # Refresh only runtime metadata so a source that legitimately reverts
        # to an older content hash is still identified as the current version.
        self._atomic_write(
            metadata_path,
            json.dumps(
                {
                    "version_id": version_id,
                    "file_hash": file_hash,
                    "captured_at": captured_at.isoformat(),
                    "size": len(content),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        return LocalWorkbookCapture(
            version_id=version_id,
            file_hash=file_hash,
            captured_at=captured_at,
            size=len(content),
            content=content,
        )

    def _stored_versions(self) -> list[DriveVersion]:
        if not self.snapshot_dir.exists():
            return []
        versions: list[DriveVersion] = []
        for metadata_path in sorted(self.snapshot_dir.glob("*.json")):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                version_id = str(metadata["version_id"])
                file_hash = self._hash_from_version(version_id)
                if str(metadata["file_hash"]) != file_hash:
                    raise ValueError("hash mismatch")
                captured_at = datetime.fromisoformat(str(metadata["captured_at"]))
                if captured_at.tzinfo is None:
                    raise ValueError("naive timestamp")
                size = int(metadata["size"])
                snapshot_path = self._snapshot_path(version_id)
                if not snapshot_path.is_file() or snapshot_path.stat().st_size != size:
                    raise ValueError("snapshot size mismatch")
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LocalWorkbookError(
                    f"Invalid local snapshot metadata: {metadata_path.name}"
                ) from exc
            versions.append(
                DriveVersion(
                    id=version_id,
                    last_modified_at=captured_at.astimezone(UTC),
                    size=size,
                    etag=file_hash,
                )
            )
        return versions

    def list_versions(self, drive_id: str, item_id: str) -> list[DriveVersion]:
        self._assert_scope(drive_id, item_id)
        self.capture()
        versions = self._stored_versions()
        versions.sort(key=lambda value: (value.last_modified_at, value.id))
        return versions

    def select_initial_baseline(
        self,
        drive_id: str,
        item_id: str,
        _cutoff: datetime,
    ) -> DriveVersion:
        """Use command-time state as the first baseline.

        ``SyncPreparationService(first_run_full_reconcile=True)`` turns every
        current row into a reconciliation candidate. No historical date is
        consulted.
        """

        versions = self.list_versions(drive_id, item_id)
        if not versions:
            raise LocalWorkbookError("Local workbook produced no readable version")
        return versions[-1]

    def get_item(self, drive_id: str, item_id: str) -> DriveItem:
        self._assert_scope(drive_id, item_id)
        capture = self.capture()
        return DriveItem(
            drive_id=drive_id,
            item_id=item_id,
            name=self.source_path.name,
            version_tag=capture.version_id,
            last_modified_at=capture.captured_at,
            size=capture.size,
            web_url=None,
        )

    def download_current(self, drive_id: str, item_id: str) -> bytes:
        self._assert_scope(drive_id, item_id)
        return self.capture().content

    def download_version(
        self,
        drive_id: str,
        item_id: str,
        version_id: str,
    ) -> bytes:
        self._assert_scope(drive_id, item_id)
        expected_hash = self._hash_from_version(version_id)
        snapshot_path = self._snapshot_path(version_id)
        try:
            content = snapshot_path.read_bytes()
        except FileNotFoundError as exc:
            raise LocalWorkbookError(
                "Committed local workbook snapshot is unavailable"
            ) from exc
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise LocalWorkbookError("Cached workbook snapshot failed integrity check")
        return content
