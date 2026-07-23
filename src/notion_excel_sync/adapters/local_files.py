from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO


FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
WINDOWS_ONLINE_ONLY_ATTRIBUTE_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

_READ_CHUNK_BYTES = 1024 * 1024


class LocalFileError(RuntimeError):
    """Raised when a local source cannot be read without violating its binding."""


class LocalFilePathError(LocalFileError):
    """Raised when a source path is invalid or escapes the configured root."""


class LocalFileChangedError(LocalFileError):
    """Raised when a source changes while it is being read."""


class LocalFileHydrationRequired(LocalFileError):
    """Raised when reading would hydrate an online-only file without permission."""


class HydrationPolicy(StrEnum):
    """Whether an online-only placeholder may be opened for a read."""

    DENY = "deny"
    READ_ONLY = "read_only"


def is_windows_online_only(file_attributes: int | None) -> bool:
    """Return whether Windows attributes indicate recall of file content."""

    if file_attributes is None:
        return False
    return bool(int(file_attributes) & WINDOWS_ONLINE_ONLY_ATTRIBUTE_MASK)


def _windows_file_attributes(stat_result: os.stat_result) -> int | None:
    value = getattr(stat_result, "st_file_attributes", None)
    return int(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class _StableStat:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def capture(cls, stat_result: os.stat_result) -> _StableStat:
        return cls(
            device=int(stat_result.st_dev),
            inode=int(stat_result.st_ino),
            mode=int(stat_result.st_mode),
            size=int(stat_result.st_size),
            mtime_ns=int(stat_result.st_mtime_ns),
            ctime_ns=int(stat_result.st_ctime_ns),
        )


@dataclass(frozen=True, slots=True)
class LocalFileManifest:
    """Content-bound, JSON-serializable metadata for one local source file."""

    relative_path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    revision: str
    windows_file_attributes: int | None
    online_only_before_read: bool
    schema_version: int = 1
    source_type: str = "local_file"
    revision_algorithm: str = "sha256"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_type": self.source_type,
            "relative_path": self.relative_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "file_identity": {
                "device": self.device,
                "inode": self.inode,
            },
            "revision": {
                "algorithm": self.revision_algorithm,
                "value": self.revision,
            },
            "windows": {
                "file_attributes": self.windows_file_attributes,
                "online_only_before_read": self.online_only_before_read,
            },
        }


@dataclass(frozen=True, slots=True)
class LocalFileRead:
    content: bytes
    manifest: LocalFileManifest


class LocalFileReadOnlyAdapter:
    """Read files below one absolute root without mutating the source tree."""

    def __init__(
        self,
        root: str | Path,
        *,
        hydration_policy: HydrationPolicy | str = HydrationPolicy.DENY,
    ) -> None:
        requested_root = Path(root)
        if not requested_root.is_absolute():
            raise LocalFilePathError("Local source root must be an absolute path")
        try:
            resolved_root = requested_root.resolve(strict=True)
        except OSError as exc:
            raise LocalFilePathError("Local source root does not exist") from exc
        if not resolved_root.is_dir():
            raise LocalFilePathError("Local source root must be a directory")
        try:
            policy = HydrationPolicy(hydration_policy)
        except ValueError as exc:
            raise ValueError(
                "hydration_policy must be 'deny' or 'read_only'"
            ) from exc
        self.root = resolved_root
        self.hydration_policy = policy

    def _resolve_file(self, path: str | Path) -> Path:
        requested = Path(path)
        if not requested.is_absolute():
            raise LocalFilePathError("Local source file must be an absolute path")
        try:
            resolved = requested.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise LocalFilePathError(
                "Local source file must exist below the configured root"
            ) from exc
        try:
            source_stat = resolved.stat()
        except OSError as exc:
            raise LocalFilePathError("Local source file is not accessible") from exc
        if not stat.S_ISREG(source_stat.st_mode):
            raise LocalFilePathError("Local source path must identify a regular file")
        return resolved

    @staticmethod
    def _assert_stable(*states: _StableStat) -> None:
        if not states or any(state != states[0] for state in states[1:]):
            raise LocalFileChangedError("Local source changed while it was being read")

    def _read(
        self,
        path: str | Path,
        *,
        retain_content: bool,
    ) -> tuple[bytes | None, LocalFileManifest]:
        source = self._resolve_file(path)
        try:
            path_stat_before = source.stat()
        except OSError as exc:
            raise LocalFileChangedError(
                "Local source became unavailable before it could be read"
            ) from exc
        before = _StableStat.capture(path_stat_before)
        attributes_before = _windows_file_attributes(path_stat_before)
        online_only = is_windows_online_only(attributes_before)
        if online_only and self.hydration_policy is not HydrationPolicy.READ_ONLY:
            raise LocalFileHydrationRequired(
                "Online-only local source requires explicit read-only hydration policy"
            )

        digest = hashlib.sha256()
        content = bytearray() if retain_content else None
        bytes_read = 0
        try:
            with source.open("rb") as stream:
                handle_before = _StableStat.capture(os.fstat(stream.fileno()))
                self._assert_stable(before, handle_before)
                bytes_read = self._consume(stream, digest, content)
                handle_after = _StableStat.capture(os.fstat(stream.fileno()))
        except LocalFileError:
            raise
        except OSError as exc:
            raise LocalFileError("Local source could not be read") from exc

        try:
            source_after = self._resolve_file(path)
            path_stat_after = source_after.stat()
        except LocalFilePathError as exc:
            raise LocalFileChangedError(
                "Local source path changed while it was being read"
            ) from exc
        except OSError as exc:
            raise LocalFileChangedError(
                "Local source became unavailable after it was read"
            ) from exc
        if source_after != source:
            raise LocalFileChangedError("Local source path changed while it was being read")
        after = _StableStat.capture(path_stat_after)
        self._assert_stable(before, handle_before, handle_after, after)
        if bytes_read != after.size:
            raise LocalFileChangedError("Local source size changed while it was being read")

        manifest = LocalFileManifest(
            relative_path=source.relative_to(self.root).as_posix(),
            size=after.size,
            mtime_ns=after.mtime_ns,
            ctime_ns=after.ctime_ns,
            device=after.device,
            inode=after.inode,
            revision=digest.hexdigest(),
            windows_file_attributes=attributes_before,
            online_only_before_read=online_only,
        )
        return (bytes(content) if content is not None else None), manifest

    @staticmethod
    def _consume(
        stream: BinaryIO,
        digest: object,
        content: bytearray | None,
    ) -> int:
        total = 0
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return total
            digest.update(chunk)
            total += len(chunk)
            if content is not None:
                content.extend(chunk)

    def read(self, path: str | Path) -> LocalFileRead:
        content, manifest = self._read(path, retain_content=True)
        if content is None:  # pragma: no cover - protected by retain_content=True
            raise AssertionError("Local file read did not retain content")
        return LocalFileRead(content=content, manifest=manifest)

    def manifest(self, path: str | Path) -> LocalFileManifest:
        """Hash a file and return metadata without retaining its content."""

        _, manifest = self._read(path, retain_content=False)
        return manifest
