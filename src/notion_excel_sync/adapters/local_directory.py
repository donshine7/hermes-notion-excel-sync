from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from notion_excel_sync.adapters.local_files import (
    HydrationPolicy,
    LocalFileReadOnlyAdapter,
)
from notion_excel_sync.adapters.onedrive import (
    DriveEntry,
    DriveFolderDelta,
    OneDriveError,
)


_MANIFEST_SCHEMA_VERSION = 1
_DELTA_TOKEN_RE = re.compile(
    r"localdelta:v1:([0-9a-f]{64}):([0-9a-f]{64})\Z"
)
_DEFAULT_MAX_ITEMS = 50_000
_DEFAULT_MAX_DEPTH = 64


class LocalDirectoryError(OneDriveError):
    """Raised when a local directory cannot satisfy the Graph-like read contract."""


class LocalDirectoryPathError(LocalDirectoryError):
    """Raised when a requested path escapes or ambiguously traverses the source root."""


class LocalDirectoryDeltaError(LocalDirectoryError):
    """Raised when a local delta cursor or its immutable manifest is invalid."""


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    root: DriveEntry
    entries: tuple[DriveEntry, ...]
    paths: dict[str, Path]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _paths_overlap(first: Path, second: Path) -> bool:
    def comparable(path: Path) -> str:
        value = os.fspath(path)
        if os.name == "nt":
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
        return os.path.normcase(os.path.abspath(value))

    first_value = comparable(first)
    second_value = comparable(second)
    try:
        common = os.path.commonpath((first_value, second_value))
    except ValueError:
        return False
    return os.path.normcase(common) in {first_value, second_value}


def _prospective_resolve(path: Path) -> Path:
    """Resolve an absent path without creating it through an unexpected symlink."""

    missing: list[str] = []
    current = path
    while not current.exists():
        if current.parent == current:
            raise LocalDirectoryPathError("Runtime directory has no existing parent")
        missing.append(current.name)
        current = current.parent
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LocalDirectoryPathError("Runtime directory parent is not accessible") from exc
    for name in reversed(missing):
        resolved /= name
    return Path(os.path.abspath(resolved))


def _normalized_relative_path(value: str | Path, root_name: str) -> tuple[str, ...]:
    raw = str(value).replace("\\", "/")
    if (
        not raw
        or raw.startswith("/")
        or "\x00" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise LocalDirectoryPathError(
            "Local directory path must be a non-empty canonical relative path"
        )
    parts = tuple(unicodedata.normalize("NFC", part) for part in raw.split("/"))
    if len(parts[0]) == 2 and parts[0][1] == ":":
        raise LocalDirectoryPathError("Local directory path must be relative")
    if parts[0].casefold() == unicodedata.normalize("NFC", root_name).casefold():
        return parts[1:]
    return parts


def _usable_inode(source_stat: os.stat_result, *, is_folder: bool) -> bool:
    inode = int(getattr(source_stat, "st_ino", 0) or 0)
    if inode <= 0:
        return False
    # A file with multiple hard links has one inode but multiple Graph-like
    # hierarchy entries. Use its path fallback so IDs remain unambiguous.
    links = int(getattr(source_stat, "st_nlink", 1) or 1)
    return is_folder or links <= 1


class LocalDirectoryReadOnlyAdapter:
    """Expose a local directory through the Wiki service's Graph GET-only surface.

    Source files are opened only through :class:`LocalFileReadOnlyAdapter`, which
    permits binary reads and metadata checks only. Immutable delta manifests are
    stored under a separate runtime directory and never below the source root.
    """

    def __init__(
        self,
        source_root: str | Path,
        runtime_dir: str | Path,
        *,
        drive_id: str = "local-directory",
        hydration_policy: HydrationPolicy | str = HydrationPolicy.DENY,
        max_items: int = _DEFAULT_MAX_ITEMS,
        max_depth: int = _DEFAULT_MAX_DEPTH,
    ) -> None:
        runtime_requested = Path(runtime_dir)
        if not runtime_requested.is_absolute():
            raise LocalDirectoryPathError("Local runtime directory must be absolute")
        if not str(drive_id).strip():
            raise ValueError("drive_id must not be empty")
        if max_items <= 0 or max_depth <= 0:
            raise ValueError("Local directory traversal limits must be positive")

        self._reader = LocalFileReadOnlyAdapter(
            source_root,
            hydration_policy=hydration_policy,
        )
        self.root = self._reader.root
        self.drive_id = str(drive_id)
        self.max_items = max_items
        self.max_depth = max_depth
        self._logical_root_name = self.root.name or "local-root"

        runtime_absolute = Path(os.path.abspath(runtime_requested))
        runtime_prospective = _prospective_resolve(runtime_absolute)
        if _paths_overlap(self.root, runtime_prospective):
            raise LocalDirectoryPathError(
                "Runtime directory must be separate from the local source root"
            )
        try:
            runtime_absolute.mkdir(parents=True, exist_ok=True)
            runtime_resolved = runtime_absolute.resolve(strict=True)
        except OSError as exc:
            raise LocalDirectoryPathError(
                "Local runtime directory cannot be created"
            ) from exc
        if not runtime_resolved.is_dir() or _paths_overlap(self.root, runtime_resolved):
            raise LocalDirectoryPathError(
                "Runtime directory must be separate from the local source root"
            )
        self.runtime_dir = runtime_resolved

        self._cache_lock = threading.RLock()
        self._path_cache: dict[str, Path] = {}
        root_stat = self._safe_stat(self.root)
        self.root_item_id = self._stable_item_id(
            root_stat,
            self._logical_relative(self.root),
            is_folder=True,
        )
        self._drive_root_item_id = "local-drive-" + hashlib.sha256(
            f"{self.drive_id}\0drive-root".encode()
        ).hexdigest()[:40]
        self._path_cache[self.root_item_id] = self.root

    def _assert_drive(self, drive_id: str) -> None:
        if str(drive_id) != self.drive_id:
            raise LocalDirectoryError("Local directory drive binding mismatch")

    @staticmethod
    def _safe_stat(path: Path) -> os.stat_result:
        try:
            return path.stat()
        except OSError as exc:
            raise LocalDirectoryError("Local source metadata is not accessible") from exc

    def _assert_contained(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise LocalDirectoryPathError(
                "Local source path attempted to leave the configured root"
            ) from exc
        return resolved

    def _logical_relative(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        parts = (self._logical_root_name, *relative.parts)
        return "/".join(unicodedata.normalize("NFC", part) for part in parts)

    def _stable_item_id(
        self,
        source_stat: os.stat_result,
        logical_path: str,
        *,
        is_folder: bool,
    ) -> str:
        if _usable_inode(source_stat, is_folder=is_folder):
            identity = (
                f"inode\0{self.drive_id}\0{int(source_stat.st_dev)}"
                f"\0{int(source_stat.st_ino)}\0{int(is_folder)}"
            )
            kind = "inode"
        else:
            canonical = unicodedata.normalize("NFC", logical_path)
            identity = f"path\0{self.drive_id}\0{canonical}\0{int(is_folder)}"
            kind = "path"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
        return f"local-{kind}-{digest}"

    def _metadata_version(
        self,
        item_id: str,
        logical_path: str,
        source_stat: os.stat_result,
    ) -> str:
        payload = {
            "item_id": item_id,
            "logical_path": unicodedata.normalize("NFC", logical_path),
            "mode": int(source_stat.st_mode),
            "size": int(source_stat.st_size),
            "mtime_ns": int(source_stat.st_mtime_ns),
            "ctime_ns": int(source_stat.st_ctime_ns),
        }
        return "local-meta:" + hashlib.sha256(_canonical_json(payload)).hexdigest()

    def _parent_path(self, path: Path) -> str:
        base = f"/drives/{quote(self.drive_id, safe='')}/root:"
        logical_path = self._logical_relative(path)
        if "/" not in logical_path:
            return base
        logical_parent = logical_path.rsplit("/", 1)[0]
        return f"{base}/{logical_parent}"

    def _parent_item_id(self, path: Path) -> str:
        if path == self.root:
            return self._drive_root_item_id
        parent = path.parent
        parent_stat = self._safe_stat(parent)
        return self._stable_item_id(
            parent_stat,
            self._logical_relative(parent),
            is_folder=True,
        )

    def _entry(
        self,
        path: Path,
        source_stat: os.stat_result,
        *,
        pinned_root: Path,
        is_folder: bool,
    ) -> DriveEntry:
        logical_path = self._logical_relative(path)
        item_id = self._stable_item_id(
            source_stat,
            logical_path,
            is_folder=is_folder,
        )
        version = self._metadata_version(item_id, logical_path, source_stat)
        if path == pinned_root:
            relative_path = logical_path
        else:
            relative_path = path.relative_to(pinned_root).as_posix()
        mime_type = None
        if not is_folder:
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return DriveEntry(
            drive_id=self.drive_id,
            item_id=item_id,
            name=path.name,
            relative_path=unicodedata.normalize("NFC", relative_path),
            parent_item_id=self._parent_item_id(path),
            parent_path=self._parent_path(path),
            is_folder=is_folder,
            version_tag=version,
            last_modified_at=datetime.fromtimestamp(
                int(source_stat.st_mtime_ns) / 1_000_000_000,
                tz=UTC,
            ),
            size=0 if is_folder else int(source_stat.st_size),
            web_url=None,
            mime_type=mime_type,
            etag=version,
            ctag=version,
        )

    def _resolve_requested_path(self, value: str | Path) -> Path:
        parts = _normalized_relative_path(value, self._logical_root_name)
        candidate = self.root.joinpath(*parts)
        current = self.root
        for part in parts:
            current /= part
            if current.is_symlink():
                raise LocalDirectoryPathError(
                    "Symbolic links are not allowed in a local Wiki source"
                )
        return self._assert_contained(candidate)

    @staticmethod
    def _directory_cycle_key(path: Path, source_stat: os.stat_result) -> tuple[object, ...]:
        inode = int(getattr(source_stat, "st_ino", 0) or 0)
        if inode > 0:
            return ("inode", int(source_stat.st_dev), inode)
        return ("path", os.path.normcase(os.fspath(path)))

    def _scan_folder(
        self,
        folder: Path,
        *,
        max_items: int,
        max_depth: int,
    ) -> _DirectorySnapshot:
        pinned_root = self._assert_contained(folder)
        if pinned_root.is_symlink():
            raise LocalDirectoryPathError(
                "Symbolic links are not allowed in a local Wiki source"
            )
        root_stat = self._safe_stat(pinned_root)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise LocalDirectoryPathError("Pinned local Wiki root is not a directory")

        root_entry = self._entry(
            pinned_root,
            root_stat,
            pinned_root=pinned_root,
            is_folder=True,
        )
        paths: dict[str, Path] = {root_entry.item_id: pinned_root}
        entries: list[DriveEntry] = []
        seen_directories = {self._directory_cycle_key(pinned_root, root_stat)}

        def visit(current: Path, depth: int) -> None:
            try:
                with os.scandir(current) as stream:
                    children = sorted(
                        list(stream),
                        key=lambda item: (
                            unicodedata.normalize("NFC", item.name).casefold(),
                            unicodedata.normalize("NFC", item.name),
                        ),
                    )
            except OSError as exc:
                raise LocalDirectoryError(
                    "Local source directory cannot be enumerated"
                ) from exc
            for child in children:
                child_path = Path(child.path)
                if child.is_symlink():
                    raise LocalDirectoryPathError(
                        "Symbolic links are not allowed in a local Wiki source"
                    )
                resolved = self._assert_contained(child_path)
                try:
                    # Use the same stat surface as get/download.  On Windows,
                    # DirEntry.stat() may omit the stable file identifier that
                    # Path.stat()/os.stat() exposes, which would make one scan
                    # path-based and the next inode-based.
                    source_stat = os.stat(resolved, follow_symlinks=False)
                except OSError as exc:
                    raise LocalDirectoryError(
                        "Local source metadata changed during traversal"
                    ) from exc
                is_folder = stat.S_ISDIR(source_stat.st_mode)
                if not is_folder and not stat.S_ISREG(source_stat.st_mode):
                    raise LocalDirectoryPathError(
                        "Local Wiki sources may contain only files and directories"
                    )
                entry = self._entry(
                    resolved,
                    source_stat,
                    pinned_root=pinned_root,
                    is_folder=is_folder,
                )
                existing = paths.get(entry.item_id)
                if existing is not None and existing != resolved:
                    raise LocalDirectoryPathError(
                        "Local source contains an ambiguous stable item identity"
                    )
                paths[entry.item_id] = resolved
                entries.append(entry)
                if len(entries) > max_items:
                    raise LocalDirectoryError(
                        "Local directory hierarchy exceeds max_items"
                    )
                if not is_folder:
                    continue
                child_depth = depth + 1
                if child_depth >= max_depth:
                    raise LocalDirectoryError(
                        "Local directory hierarchy exceeds max_depth"
                    )
                cycle_key = self._directory_cycle_key(resolved, source_stat)
                if cycle_key in seen_directories:
                    raise LocalDirectoryPathError(
                        "Local directory hierarchy contains a cycle"
                    )
                seen_directories.add(cycle_key)
                visit(resolved, child_depth)

        visit(pinned_root, 0)
        entries.sort(
            key=lambda item: (
                unicodedata.normalize("NFC", item.relative_path).casefold(),
                item.relative_path,
                item.item_id,
            )
        )
        tree_projection = {
            "root": self._entry_to_dict(root_entry),
            "entries": [self._entry_to_dict(entry) for entry in entries],
        }
        tree_version = "local-tree:" + hashlib.sha256(
            _canonical_json(tree_projection)
        ).hexdigest()
        root_entry = replace(
            root_entry,
            version_tag=tree_version,
            etag=tree_version,
            ctag=tree_version,
        )
        with self._cache_lock:
            self._path_cache.update(paths)
        return _DirectorySnapshot(root_entry, tuple(entries), paths)

    def _cached_path(self, item_id: str) -> Path | None:
        with self._cache_lock:
            candidate = self._path_cache.get(item_id)
        if candidate is None:
            return None
        try:
            resolved = self._assert_contained(candidate)
            if resolved.is_symlink():
                return None
            source_stat = self._safe_stat(resolved)
            is_folder = stat.S_ISDIR(source_stat.st_mode)
            if not is_folder and not stat.S_ISREG(source_stat.st_mode):
                return None
            actual_id = self._stable_item_id(
                source_stat,
                self._logical_relative(resolved),
                is_folder=is_folder,
            )
        except LocalDirectoryError:
            return None
        return resolved if actual_id == item_id else None

    def _locate(self, item_id: str) -> Path:
        cached = self._cached_path(str(item_id))
        if cached is not None:
            return cached
        snapshot = self._scan_folder(
            self.root,
            max_items=self.max_items,
            max_depth=self.max_depth,
        )
        try:
            return snapshot.paths[str(item_id)]
        except KeyError as exc:
            raise LocalDirectoryError("Local directory item was not found") from exc

    def get_entry(self, drive_id: str, item_id: str) -> DriveEntry:
        self._assert_drive(drive_id)
        path = self._locate(item_id)
        source_stat = self._safe_stat(path)
        if stat.S_ISDIR(source_stat.st_mode):
            return self._scan_folder(
                path,
                max_items=self.max_items,
                max_depth=self.max_depth,
            ).root
        if not stat.S_ISREG(source_stat.st_mode):
            raise LocalDirectoryPathError("Local directory item is not a regular file")
        return self._entry(
            path,
            source_stat,
            pinned_root=self.root,
            is_folder=False,
        )

    def get_item_by_path(self, drive_id: str, path: str) -> DriveEntry:
        self._assert_drive(drive_id)
        resolved = self._resolve_requested_path(path)
        source_stat = self._safe_stat(resolved)
        if stat.S_ISDIR(source_stat.st_mode):
            return self._scan_folder(
                resolved,
                max_items=self.max_items,
                max_depth=self.max_depth,
            ).root
        if not stat.S_ISREG(source_stat.st_mode):
            raise LocalDirectoryPathError("Local directory item is not a regular file")
        entry = self._entry(
            resolved,
            source_stat,
            pinned_root=self.root,
            is_folder=False,
        )
        with self._cache_lock:
            self._path_cache[entry.item_id] = resolved
        return entry

    @staticmethod
    def _include_roots(
        entries: tuple[DriveEntry, ...],
        names: Iterable[str] | None,
    ) -> tuple[DriveEntry, ...]:
        if names is None:
            return entries
        requested: dict[str, str] = {}
        for raw_name in names:
            name = unicodedata.normalize("NFC", str(raw_name))
            if not name or "/" in name or "\\" in name or name in {".", ".."}:
                raise LocalDirectoryPathError(
                    "Local include root name is not canonical"
                )
            key = name.casefold()
            if key in requested:
                raise LocalDirectoryPathError(
                    "Local include root names are duplicate or ambiguous"
                )
            requested[key] = name

        direct: dict[str, DriveEntry] = {}
        for entry in entries:
            if "/" in entry.relative_path:
                continue
            key = unicodedata.normalize("NFC", entry.name).casefold()
            if key not in requested:
                continue
            if key in direct:
                raise LocalDirectoryPathError(
                    "Local include root folders are duplicate or ambiguous"
                )
            if not entry.is_folder:
                raise LocalDirectoryPathError(
                    "Local include root matched a non-folder item"
                )
            direct[key] = entry

        selected = set(direct)
        return tuple(
            entry
            for entry in entries
            if (
                unicodedata.normalize(
                    "NFC",
                    entry.relative_path.split("/", 1)[0],
                ).casefold()
                in selected
            )
        )

    def walk_folder(
        self,
        drive_id: str,
        root_item_id: str,
        *,
        max_items: int = _DEFAULT_MAX_ITEMS,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        include_root_names: Iterable[str] | None = None,
        max_workers: int = 4,
    ) -> list[DriveEntry]:
        self._assert_drive(drive_id)
        if max_items <= 0 or max_depth <= 0:
            raise ValueError("Folder traversal limits must be positive")
        if max_workers <= 0 or max_workers > 4:
            raise ValueError("Folder traversal max_workers must be between 1 and 4")
        root = self._locate(root_item_id)
        snapshot = self._scan_folder(
            root,
            max_items=max_items,
            max_depth=max_depth,
        )
        return list(self._include_roots(snapshot.entries, include_root_names))

    @staticmethod
    def _entry_to_dict(entry: DriveEntry) -> dict[str, object]:
        return {
            "drive_id": entry.drive_id,
            "item_id": entry.item_id,
            "name": entry.name,
            "relative_path": entry.relative_path,
            "parent_item_id": entry.parent_item_id,
            "parent_path": entry.parent_path,
            "is_folder": entry.is_folder,
            "version_tag": entry.version_tag,
            "last_modified_at": entry.last_modified_at.isoformat(),
            "size": entry.size,
            "web_url": entry.web_url,
            "mime_type": entry.mime_type,
            "etag": entry.etag,
            "ctag": entry.ctag,
        }

    @staticmethod
    def _entry_from_dict(value: object) -> DriveEntry:
        if not isinstance(value, dict):
            raise LocalDirectoryDeltaError("Local delta manifest entry is invalid")
        try:
            modified = datetime.fromisoformat(str(value["last_modified_at"]))
            if modified.tzinfo is None:
                raise ValueError("naive timestamp")
            entry = DriveEntry(
                drive_id=str(value["drive_id"]),
                item_id=str(value["item_id"]),
                name=str(value["name"]),
                relative_path=str(value["relative_path"]),
                parent_item_id=(
                    str(value["parent_item_id"])
                    if value.get("parent_item_id") is not None
                    else None
                ),
                parent_path=(
                    str(value["parent_path"])
                    if value.get("parent_path") is not None
                    else None
                ),
                is_folder=bool(value["is_folder"]),
                version_tag=str(value["version_tag"]),
                last_modified_at=modified.astimezone(UTC),
                size=int(value["size"]) if value.get("size") is not None else None,
                web_url=(
                    str(value["web_url"]) if value.get("web_url") is not None else None
                ),
                mime_type=(
                    str(value["mime_type"])
                    if value.get("mime_type") is not None
                    else None
                ),
                etag=str(value["etag"]) if value.get("etag") is not None else None,
                ctag=str(value["ctag"]) if value.get("ctag") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalDirectoryDeltaError(
                "Local delta manifest entry is invalid"
            ) from exc
        if (
            not entry.item_id
            or not entry.name
            or not entry.version_tag
            or "/" in entry.name
            or "\\" in entry.name
        ):
            raise LocalDirectoryDeltaError("Local delta manifest entry is incomplete")
        return entry

    def _snapshot_payload(self, snapshot: _DirectorySnapshot) -> dict[str, object]:
        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "drive_id": self.drive_id,
            "root_item_id": snapshot.root.item_id,
            "root": self._entry_to_dict(snapshot.root),
            "entries": [self._entry_to_dict(entry) for entry in snapshot.entries],
        }

    def _scope_digest(self, root_item_id: str) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "drive_id": self.drive_id,
                    "root_item_id": str(root_item_id),
                }
            )
        ).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            # Keep the temporary basename short.  The immutable manifest path
            # already contains two SHA-256 components and a target-derived
            # prefix can exceed the legacy Windows MAX_PATH boundary.
            prefix=".tmp-",
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

    def _persist_snapshot(self, snapshot: _DirectorySnapshot) -> str:
        data = _canonical_json(self._snapshot_payload(snapshot))
        digest = hashlib.sha256(data).hexdigest()
        scope = self._scope_digest(snapshot.root.item_id)
        path = self.runtime_dir / "local-directory-delta" / scope / f"{digest}.json"
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise LocalDirectoryDeltaError(
                    "Stored local delta manifest cannot be read"
                ) from exc
            if existing != data:
                raise LocalDirectoryDeltaError(
                    "Stored local delta manifest failed its immutable binding"
                )
        else:
            self._atomic_write(path, data)
        return f"localdelta:v1:{scope}:{digest}"

    def _load_snapshot(
        self,
        delta_link: str,
        *,
        root_item_id: str,
        max_items: int,
    ) -> _DirectorySnapshot:
        match = _DELTA_TOKEN_RE.fullmatch(str(delta_link))
        if match is None:
            raise LocalDirectoryDeltaError("Local delta cursor is invalid")
        scope, digest = match.groups()
        if scope != self._scope_digest(root_item_id):
            raise LocalDirectoryDeltaError(
                "Local delta cursor attempted to cross root scopes"
            )
        path = self.runtime_dir / "local-directory-delta" / scope / f"{digest}.json"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LocalDirectoryDeltaError(
                "Local delta manifest is unavailable"
            ) from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise LocalDirectoryDeltaError(
                "Local delta manifest failed its content binding"
            )
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalDirectoryDeltaError("Local delta manifest is invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _MANIFEST_SCHEMA_VERSION
            or payload.get("drive_id") != self.drive_id
            or payload.get("root_item_id") != root_item_id
            or not isinstance(payload.get("entries"), list)
        ):
            raise LocalDirectoryDeltaError("Local delta manifest scope is invalid")
        raw_entries = payload["entries"]
        if len(raw_entries) > max_items:
            raise LocalDirectoryDeltaError(
                "Local delta manifest exceeds max_items"
            )
        root = self._entry_from_dict(payload.get("root"))
        entries = tuple(self._entry_from_dict(value) for value in raw_entries)
        if (
            root.drive_id != self.drive_id
            or root.item_id != root_item_id
            or not root.is_folder
            or any(entry.drive_id != self.drive_id for entry in entries)
        ):
            raise LocalDirectoryDeltaError("Local delta manifest binding is invalid")
        ids = [entry.item_id for entry in entries]
        if root.item_id in ids or len(ids) != len(set(ids)):
            raise LocalDirectoryDeltaError(
                "Local delta manifest contains duplicate item identities"
            )
        return _DirectorySnapshot(root, entries, {})

    def get_folder_delta_checkpoint(
        self,
        drive_id: str,
        root_item_id: str,
    ) -> DriveFolderDelta:
        self._assert_drive(drive_id)
        root = self._locate(root_item_id)
        snapshot = self._scan_folder(
            root,
            max_items=self.max_items,
            max_depth=self.max_depth,
        )
        delta_link = self._persist_snapshot(snapshot)
        return DriveFolderDelta(
            root=snapshot.root,
            entries=(),
            deleted_item_ids=(),
            delta_link=delta_link,
        )

    def read_folder_delta(
        self,
        drive_id: str,
        root_item_id: str,
        *,
        delta_link: str | None = None,
        max_items: int = _DEFAULT_MAX_ITEMS,
    ) -> DriveFolderDelta:
        self._assert_drive(drive_id)
        if max_items <= 0:
            raise ValueError("Folder delta max_items must be positive")
        root = self._locate(root_item_id)
        current = self._scan_folder(
            root,
            max_items=max_items,
            max_depth=self.max_depth,
        )
        current_by_id = {entry.item_id: entry for entry in current.entries}
        if delta_link is None:
            changes = tuple(current.entries)
            deleted: tuple[str, ...] = ()
        else:
            previous = self._load_snapshot(
                delta_link,
                root_item_id=root_item_id,
                max_items=max_items,
            )
            previous_by_id = {entry.item_id: entry for entry in previous.entries}
            changes = tuple(
                entry
                for entry in current.entries
                if previous_by_id.get(entry.item_id) != entry
            )
            deleted = tuple(sorted(set(previous_by_id) - set(current_by_id)))
        next_link = self._persist_snapshot(current)
        return DriveFolderDelta(
            root=current.root,
            entries=changes,
            deleted_item_ids=deleted,
            delta_link=next_link,
        )

    def download_current(self, drive_id: str, item_id: str) -> bytes:
        self._assert_drive(drive_id)
        path = self._locate(item_id)
        source_stat = self._safe_stat(path)
        if not stat.S_ISREG(source_stat.st_mode):
            raise LocalDirectoryPathError(
                "Only regular local files can be downloaded"
            )
        before_id = self._stable_item_id(
            source_stat,
            self._logical_relative(path),
            is_folder=False,
        )
        if before_id != item_id:
            raise LocalDirectoryError("Local file identity changed before download")
        result = self._reader.read(path)
        after_stat = self._safe_stat(path)
        after_id = self._stable_item_id(
            after_stat,
            self._logical_relative(path),
            is_folder=False,
        )
        if after_id != item_id:
            raise LocalDirectoryError("Local file identity changed during download")
        return result.content


__all__ = [
    "LocalDirectoryDeltaError",
    "LocalDirectoryError",
    "LocalDirectoryPathError",
    "LocalDirectoryReadOnlyAdapter",
]
