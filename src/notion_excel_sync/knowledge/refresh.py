from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from notion_excel_sync.adapters.onedrive import (
    DriveEntry,
    DriveFolderDelta,
    OneDriveReadOnlyClient,
)
from notion_excel_sync.config import WikiConfig
from notion_excel_sync.knowledge.index import (
    WikiGenerationLease,
    WikiIndex,
    chunk_text,
)
from notion_excel_sync.knowledge.models import (
    CHUNKER_VERSION,
    EXTRACTOR_VERSION,
    TOKENIZER_VERSION,
    KnowledgeDocument,
    KnowledgeSnapshot,
    QuarantineRecord,
)
from notion_excel_sync.knowledge.policy import (
    WikiInclusionPolicy,
    hashed_locator,
    policy_fingerprint,
)
from notion_excel_sync.models import KnowledgeRef


class WikiRefreshError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


_DOCUMENT_EXTRACTION_REJECTION_CODES = frozenset(
    {
        "extractor_timeout",
        "extractor_rejected_document",
        "extractor_output_limit",
    }
)


_EXTRACTOR_PROCESS_MEMORY_BYTES = 512 * 1024 * 1024
_EXTRACTOR_DOCUMENT_REJECTION_EXIT_CODE = 20
_FULL_CATALOG_MAX_ATTEMPTS = 3
_FULL_CATALOG_MAX_ITEMS = 50_000
_DOCUMENT_MAX_WORKERS = 4


def _include_root_keys(names: Iterable[str]) -> frozenset[str]:
    keys: set[str] = set()
    for raw_name in names:
        name = unicodedata.normalize("NFC", str(raw_name))
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise WikiRefreshError("Wiki include root name is not canonical")
        key = name.casefold()
        if key in keys:
            raise WikiRefreshError("Wiki include root names are duplicate or ambiguous")
        keys.add(key)
    return frozenset(keys)


def _entry_is_included(entry: DriveEntry, include_root_keys: frozenset[str]) -> bool:
    path = unicodedata.normalize("NFC", entry.relative_path.replace("\\", "/"))
    path = path.strip("/")
    top_root = path.split("/", 1)[0] if path else ""
    return bool(top_root and top_root.casefold() in include_root_keys)


def _extractor_python_runtime() -> tuple[Path, Path]:
    """Return the real interpreter and this environment's trusted package path.

    On Windows, a virtual-environment ``python.exe`` can be a redirector that
    starts the base interpreter as a second process.  Calling that redirector
    inside an active-process-limited Job Object would either fail or require a
    weaker two-process allowance.  Run the base interpreter directly and let
    the isolated worker add only the current environment's package directory.
    """

    raw_executable = getattr(sys, "_base_executable", None) or sys.executable
    try:
        executable = Path(raw_executable).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WikiRefreshError("extractor_runtime_unavailable") from exc
    if not executable.is_file():
        raise WikiRefreshError("extractor_runtime_unavailable")

    raw_runtime_site = sysconfig.get_path("purelib")
    if not raw_runtime_site:
        raise WikiRefreshError("extractor_runtime_site_unavailable")
    try:
        runtime_site = Path(raw_runtime_site).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WikiRefreshError("extractor_runtime_site_unavailable") from exc
    if not runtime_site.is_dir():
        raise WikiRefreshError("extractor_runtime_site_unavailable")
    return executable, runtime_site


def _run_extractor_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    """Run the parser with a one-process Windows Job memory boundary.

    This is resource containment for a same-user worker, not an AppContainer
    or an operating-system identity boundary.
    """

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name != "nt":
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            creationflags=creation_flags,
        )

    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise WikiRefreshError("extractor_job_creation_failed")
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00000008 | 0x00000100 | 0x00002000
    information.BasicLimitInformation.ActiveProcessLimit = 1
    information.ProcessMemoryLimit = _EXTRACTOR_PROCESS_MEMORY_BYTES
    if not kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        kernel32.CloseHandle(job)
        raise WikiRefreshError("extractor_job_policy_failed")

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
        process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            process.kill()
            process.wait()
            raise WikiRefreshError("extractor_job_assignment_failed")
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            raise WikiRefreshError("extractor_timeout") from exc
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        kernel32.CloseHandle(job)


@dataclass(slots=True)
class WikiRefreshReport:
    snapshot: KnowledgeSnapshot
    full_rebuild: bool
    catalog_entries: int
    changed_entries: int
    downloaded_documents: int
    downloaded_bytes: int
    indexed_documents: int
    quarantined: int
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    extraction_warnings: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _PreparedDocument:
    data_size: int
    content_hash: str | None = None
    text: str | None = None
    warnings: tuple[str, ...] = ()
    rejection_reason: str | None = None


def _isolated_extract(
    data: bytes,
    extension: str,
    *,
    work_dir: Path,
    config: WikiConfig,
) -> tuple[str, tuple[str, ...]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    descriptor, source_name = tempfile.mkstemp(
        prefix="wiki-source-", suffix=extension, dir=work_dir
    )
    source = Path(source_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        worker = Path(__file__).with_name("extractor_worker.py")
        executable, runtime_site = _extractor_python_runtime()
        environment = {
            key: os.environ[key]
            for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = _run_extractor_process(
                [
                    str(executable),
                    "-B",
                    "-I",
                    str(worker),
                    "--root",
                    str(work_dir),
                    "--input",
                    str(source),
                    "--runtime-site",
                    str(runtime_site),
                    "--extension",
                    extension,
                    "--max-input-bytes",
                    str(config.max_file_bytes),
                    "--max-output-chars",
                    str(config.max_output_chars),
                ],
                cwd=work_dir,
                environment=environment,
                timeout_seconds=config.extractor_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise WikiRefreshError("extractor_timeout") from exc
        if len(completed.stdout) > config.max_output_chars * 4 + 16_384:
            raise WikiRefreshError("extractor_worker_failed")
        if completed.returncode == _EXTRACTOR_DOCUMENT_REJECTION_EXIT_CODE:
            try:
                rejection = json.loads(completed.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WikiRefreshError("extractor_worker_failed") from exc
            if (
                not isinstance(rejection, dict)
                or set(rejection)
                != {"schema_version", "status", "code"}
                or rejection.get("schema_version") != 1
                or rejection.get("status") != "document_rejected"
                or rejection.get("code")
                not in {
                    "extractor_rejected_document",
                    "extractor_output_limit",
                }
            ):
                raise WikiRefreshError("extractor_worker_failed")
            raise WikiRefreshError(str(rejection["code"]))
        if completed.returncode != 0:
            # Only the dedicated, validated document-rejection protocol above
            # is recoverable. Import/guard/runtime/code failures are fatal.
            raise WikiRefreshError("extractor_worker_failed")
        try:
            payload = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WikiRefreshError("extractor_invalid_output") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise WikiRefreshError("extractor_schema_mismatch")
        text = payload.get("text")
        warnings = payload.get("warnings", [])
        if not isinstance(text, str) or not text or not isinstance(warnings, list):
            raise WikiRefreshError("extractor_incomplete_output")
        return text, tuple(str(item) for item in warnings)
    finally:
        try:
            if source.exists():
                source.write_bytes(b"")
        finally:
            source.unlink(missing_ok=True)


def _effective_dates(entry: DriveEntry, text: str) -> tuple[str | None, str | None]:
    """Extract only a conservative business year hint for unverified references."""

    sample = f"{entry.relative_path}\n{text[:10_000]}"
    years = sorted(
        {
            int(value)
            for value in re.findall(r"(?<!\d)(20\d{2})\s*년(?:도)?", sample)
            if 2000 <= int(value) <= 2100
        }
    )
    if len(years) != 1:
        return None, None
    year = years[0]
    return f"{year:04d}-01-01", f"{year:04d}-12-31"


class OneDriveWikiRefreshService:
    """Build a protected Wiki generation without mutating OneDrive or Notion."""

    def __init__(
        self,
        *,
        config: WikiConfig,
        onedrive: OneDriveReadOnlyClient,
        index: WikiIndex,
        default_drive_id: str,
        work_dir: Path | None = None,
        document_max_workers: int = _DOCUMENT_MAX_WORKERS,
    ) -> None:
        if document_max_workers <= 0 or document_max_workers > _DOCUMENT_MAX_WORKERS:
            raise ValueError("Wiki document_max_workers must be between 1 and 4")
        self.config = config
        self.onedrive = onedrive
        self.index = index
        self.default_drive_id = default_drive_id
        self.work_dir = (work_dir or index.path.parent / "work").resolve()
        self.document_max_workers = document_max_workers

    def _scope(self) -> tuple[str, str]:
        if not self.config.enabled:
            raise WikiRefreshError("Wiki is disabled in configuration")
        drive_id = self.config.drive_id or self.default_drive_id
        root_item_id = self.config.root_item_id or ""
        if self.config.root_path:
            resolved = self.onedrive.get_item_by_path(drive_id, self.config.root_path)
            if not resolved.is_folder:
                raise WikiRefreshError("Configured Wiki root path is not a folder")
            if root_item_id and resolved.item_id != root_item_id:
                raise WikiRefreshError("Configured Wiki path and root item ID disagree")
            root_item_id = resolved.item_id
        if not drive_id or not root_item_id:
            raise WikiRefreshError("Wiki drive and root item IDs are required")
        return drive_id, root_item_id

    def _existing(self) -> KnowledgeSnapshot | None:
        if not self.index.path.exists():
            return None
        return self.index.status()

    @staticmethod
    def _graph_path_key(value: str | None) -> str:
        if not value:
            return ""
        return unicodedata.normalize("NFC", value.replace("\\", "/")).rstrip(
            "/"
        ).casefold()

    def _validate_download_binding(
        self,
        *,
        drive_id: str,
        root_item_id: str,
        root: DriveEntry,
        expected: DriveEntry,
        current: DriveEntry,
        data_size: int,
    ) -> None:
        if (
            root.drive_id != drive_id
            or root.item_id != root_item_id
            or not root.is_folder
        ):
            raise WikiRefreshError("OneDrive Wiki root binding mismatch")
        root_parent = self._graph_path_key(root.parent_path)
        root_name = unicodedata.normalize("NFC", root.name).casefold()
        if not root_parent or not root_name:
            raise WikiRefreshError("OneDrive Wiki root binding mismatch")
        root_path = f"{root_parent}/{root_name}"
        expected_parent = self._graph_path_key(expected.parent_path)
        current_parent = self._graph_path_key(current.parent_path)
        under_root = current_parent == root_path or current_parent.startswith(
            root_path + "/"
        )
        if (
            expected.drive_id != drive_id
            or expected.is_folder
            or current.drive_id != drive_id
            or current.item_id != expected.item_id
            or current.is_folder
            or not under_root
            or not expected.parent_item_id
            or current.parent_item_id != expected.parent_item_id
            or not expected_parent
            or current_parent != expected_parent
            or unicodedata.normalize("NFC", current.name)
            != unicodedata.normalize("NFC", expected.name)
            or not expected.version_tag
            or current.version_tag != expected.version_tag
            or not expected.etag
            or not current.etag
            or current.etag != expected.etag
            or expected.size is None
            or current.size != expected.size
            or data_size != expected.size
            or (current.mime_type or "").casefold()
            != (expected.mime_type or "").casefold()
        ):
            raise WikiRefreshError("OneDrive document binding changed during Wiki refresh")

    def _prepare_document(
        self,
        *,
        drive_id: str,
        root_item_id: str,
        root: DriveEntry,
        entry: DriveEntry,
    ) -> _PreparedDocument:
        data = self.onedrive.download_current(drive_id, entry.item_id)
        if not isinstance(data, bytes):
            raise WikiRefreshError("OneDrive document download returned an invalid type")
        try:
            current_entry = self.onedrive.get_entry(drive_id, entry.item_id)
            self._validate_download_binding(
                drive_id=drive_id,
                root_item_id=root_item_id,
                root=root,
                expected=entry,
                current=current_entry,
                data_size=len(data),
            )
            content_hash = hashlib.sha256(data).hexdigest()
            extension = PurePosixPath(entry.name).suffix.casefold()
            try:
                text, extraction_warnings = _isolated_extract(
                    data,
                    extension,
                    work_dir=self.work_dir,
                    config=self.config,
                )
            except WikiRefreshError as exc:
                if exc.code not in _DOCUMENT_EXTRACTION_REJECTION_CODES:
                    raise
                return _PreparedDocument(
                    data_size=len(data),
                    rejection_reason=exc.code,
                )
            return _PreparedDocument(
                data_size=len(data),
                content_hash=content_hash,
                text=text,
                warnings=extraction_warnings,
            )
        finally:
            # Never retain downloaded source bytes in Future results.
            del data

    def _read_consistent_full_catalog(
        self,
        drive_id: str,
        root_item_id: str,
        *,
        progress: Callable[[dict[str, object]], None] | None,
        max_attempts: int = _FULL_CATALOG_MAX_ATTEMPTS,
    ) -> DriveFolderDelta:
        """Walk the current tree and close the race with a latest delta cursor.

        A normal initial delta request can enumerate the drive's entire retained
        change history.  Instead, capture a ``token=latest`` cursor, walk only
        the pinned hierarchy, and merge the short catch-up delta.  Folder
        changes require a retry because Graph does not promise that a folder
        event is accompanied by every descendant whose canonical path changed.
        """

        if max_attempts <= 0:
            raise ValueError("Full Wiki catalog max_attempts must be positive")
        include_root_keys = _include_root_keys(self.config.include_roots)
        for attempt in range(1, max_attempts + 1):
            checkpoint = self.onedrive.get_folder_delta_checkpoint(
                drive_id,
                root_item_id,
            )
            walked = self.onedrive.walk_folder(
                drive_id,
                root_item_id,
                max_items=_FULL_CATALOG_MAX_ITEMS,
                include_root_names=self.config.include_roots,
                max_workers=4,
            )
            catalog: dict[str, DriveEntry] = {}
            for entry in walked:
                if not _entry_is_included(entry, include_root_keys):
                    raise WikiRefreshError(
                        "OneDrive Wiki walk returned an entry outside include roots"
                    )
                previous = catalog.get(entry.item_id)
                if previous is not None and previous != entry:
                    raise WikiRefreshError(
                        "OneDrive Wiki walk returned one item at multiple paths"
                    )
                catalog[entry.item_id] = entry

            catch_up = self.onedrive.read_folder_delta(
                drive_id,
                root_item_id,
                delta_link=checkpoint.delta_link,
                max_items=_FULL_CATALOG_MAX_ITEMS,
            )
            affected_deletions: set[str] = set()
            folder_changed = False
            for entry in catch_up.entries:
                previous = catalog.get(entry.item_id)
                is_included = _entry_is_included(entry, include_root_keys)
                if entry.is_folder:
                    if (previous is not None and previous.is_folder) or is_included:
                        folder_changed = True
                        break
                    if previous is not None:
                        # A drive item cannot normally change between file and
                        # folder facets. Treat such a result as unstable.
                        folder_changed = True
                        break
                    continue
                if previous is not None and previous.is_folder:
                    folder_changed = True
                    break
                if is_included:
                    catalog[entry.item_id] = entry
                elif previous is not None:
                    catalog.pop(entry.item_id, None)
                    affected_deletions.add(entry.item_id)
            if not folder_changed:
                for item_id in catch_up.deleted_item_ids:
                    previous = catalog.get(item_id)
                    if previous is None:
                        continue
                    if previous.is_folder:
                        folder_changed = True
                        break
                    catalog.pop(item_id, None)
                    affected_deletions.add(item_id)
            if folder_changed:
                if progress:
                    progress(
                        {
                            "stage": "catalog_retry",
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "reason": "folder_changed",
                        }
                    )
                continue
            if len(catalog) > _FULL_CATALOG_MAX_ITEMS:
                raise WikiRefreshError("OneDrive Wiki catalog exceeds max_items")
            final_root = self.onedrive.get_entry(drive_id, root_item_id)
            if (
                final_root.drive_id != drive_id
                or final_root.item_id != root_item_id
                or not final_root.is_folder
            ):
                raise WikiRefreshError("The pinned OneDrive Wiki root is no longer valid")
            if final_root.version_tag != catch_up.root.version_tag:
                if progress:
                    progress(
                        {
                            "stage": "catalog_retry",
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "reason": "root_changed",
                        }
                    )
                continue
            return DriveFolderDelta(
                root=final_root,
                entries=tuple(
                    sorted(
                        catalog.values(),
                        key=lambda item: (item.relative_path.casefold(), item.item_id),
                    )
                ),
                deleted_item_ids=tuple(sorted(affected_deletions)),
                delta_link=catch_up.delta_link,
            )
        raise WikiRefreshError(
            "OneDrive Wiki folders changed repeatedly during the full catalog"
        )

    def refresh(
        self,
        *,
        progress: Callable[[dict[str, object]], None] | None = None,
        force_full: bool = False,
    ) -> WikiRefreshReport:
        # The lease spans discovery, downloads, extraction, final source-root
        # validation, and atomic publication. This prevents two refreshes from
        # deriving updates from the same old generation and losing one result.
        with self.index.generation_lock() as generation_lock:
            return self._refresh_under_generation_lock(
                progress=progress,
                force_full=force_full,
                generation_lock=generation_lock,
            )

    def _refresh_under_generation_lock(
        self,
        *,
        progress: Callable[[dict[str, object]], None] | None,
        force_full: bool,
        generation_lock: WikiGenerationLease,
    ) -> WikiRefreshReport:
        drive_id, root_item_id = self._scope()
        policy_version = policy_fingerprint(self.config)
        # An explicit full rebuild is also the recovery path for an obsolete or
        # damaged prior schema, so it must not depend on reading that index.
        existing = None if force_full else self._existing()
        full_rebuild = force_full or existing is None
        if existing is not None and (
            existing.drive_id != drive_id
            or existing.root_item_id != root_item_id
            or existing.policy_version != policy_version
            or existing.extractor_version != EXTRACTOR_VERSION
            or existing.chunker_version != CHUNKER_VERSION
            or existing.tokenizer_version != TOKENIZER_VERSION
        ):
            full_rebuild = True
        delta_link = None if full_rebuild or existing is None else existing.delta_link
        if full_rebuild:
            delta = self._read_consistent_full_catalog(
                drive_id,
                root_item_id,
                progress=progress,
            )
        else:
            delta = self.onedrive.read_folder_delta(
                drive_id,
                root_item_id,
                delta_link=delta_link or None,
                max_items=_FULL_CATALOG_MAX_ITEMS,
            )
        if progress:
            progress(
                {
                    "stage": "cataloged",
                    "entries": len(delta.entries),
                    "incremental": not full_rebuild,
                }
            )
        if not full_rebuild and any(entry.is_folder for entry in delta.entries):
            # A folder rename can change descendants' paths without returning
            # every descendant in an incremental delta. Rebuild once so all
            # protected citations retain canonical paths.
            return self._refresh_under_generation_lock(
                progress=progress,
                force_full=True,
                generation_lock=generation_lock,
            )
        policy = WikiInclusionPolicy(self.config)
        removals = set(delta.deleted_item_ids)
        quarantines: list[QuarantineRecord] = []
        candidates: list[tuple[DriveEntry, object]] = []
        reasons: Counter[str] = Counter()
        for entry in delta.entries:
            if entry.is_folder:
                continue
            decision = policy.decide(entry)
            if not decision.include_content or self.config.rollout_mode == "inventory":
                reason = (
                    "inventory_only"
                    if decision.include_content and self.config.rollout_mode == "inventory"
                    else decision.reason
                )
                reasons[reason] += 1
                removals.add(entry.item_id)
                quarantines.append(
                    QuarantineRecord(
                        item_id=entry.item_id,
                        relative_path_hash=hashed_locator(entry.relative_path),
                        version_id=entry.version_tag,
                        reason=reason,
                    )
                )
                continue
            candidates.append((entry, decision))

        inventory = {} if full_rebuild else self.index.document_inventory()
        current_count = len(inventory)
        current_bytes = sum(int(item["size"]) for item in inventory.values())
        current_chars = sum(int(item["char_count"]) for item in inventory.values())
        current_chunks = sum(int(item["chunk_count"]) for item in inventory.values())
        changed_ids = {entry.item_id for entry in delta.entries} | set(
            delta.deleted_item_ids
        )
        for item_id in changed_ids:
            previous = inventory.get(item_id)
            if previous:
                current_count -= 1
                current_bytes -= int(previous["size"])
                current_chars -= int(previous["char_count"])
                current_chunks -= int(previous["chunk_count"])
        if not full_rebuild and (
            current_count > self.config.max_documents
            or current_bytes > self.config.max_total_bytes
            or current_chars > self.config.max_generation_chars
            or current_chunks > self.config.max_generation_chunks
        ):
            # A prior implementation or manually imported generation may have
            # been created under an incompatible counting rule.  Do not carry
            # an already-over-limit unchanged subset forward; rebuild it under
            # the current deterministic policy while retaining this lease.
            return self._refresh_under_generation_lock(
                progress=progress,
                force_full=True,
                generation_lock=generation_lock,
            )
        root_order = {
            unicodedata.normalize("NFC", name).casefold(): index
            for index, name in enumerate(self.config.include_roots)
        }
        candidates.sort(
            key=lambda pair: (
                root_order.get(
                    policy.top_root(pair[0].relative_path).casefold(),
                    999,
                ),
                pair[0].relative_path.casefold(),
                pair[0].item_id,
            )
        )
        selected: list[tuple[DriveEntry, object]] = []
        for entry, decision in candidates:
            candidate_count = current_count + 1
            candidate_bytes = current_bytes + int(entry.size or 0)
            if (
                candidate_count > self.config.max_documents
                or candidate_bytes > self.config.max_total_bytes
            ):
                reasons["generation_capacity_limit"] += 1
                removals.add(entry.item_id)
                quarantines.append(
                    QuarantineRecord(
                        item_id=entry.item_id,
                        relative_path_hash=hashed_locator(entry.relative_path),
                        version_id=entry.version_tag,
                        reason="generation_capacity_limit",
                    )
                )
                continue
            current_count = candidate_count
            current_bytes = candidate_bytes
            selected.append((entry, decision))

        upserts = []
        warnings: Counter[str] = Counter()
        downloaded_bytes = 0
        with ThreadPoolExecutor(
            max_workers=self.document_max_workers,
            thread_name_prefix="wiki-document-read",
        ) as executor:
            for offset in range(0, len(selected), self.document_max_workers):
                selected_chunk = selected[
                    offset : offset + self.document_max_workers
                ]
                futures = [
                    executor.submit(
                        self._prepare_document,
                        drive_id=drive_id,
                        root_item_id=root_item_id,
                        root=delta.root,
                        entry=entry,
                    )
                    for entry, _ in selected_chunk
                ]
                done, not_done = wait(futures, return_when=FIRST_EXCEPTION)
                failed = next(
                    (
                        future
                        for future in futures
                        if future in done and future.exception() is not None
                    ),
                    None,
                )
                if failed is not None:
                    for future in not_done:
                        future.cancel()
                    failed.result()

                for chunk_position, ((entry, decision), future) in enumerate(
                    zip(selected_chunk, futures, strict=True),
                    start=1,
                ):
                    position = offset + chunk_position
                    prepared = future.result()
                    downloaded_bytes += prepared.data_size
                    if prepared.rejection_reason is not None:
                        reason = prepared.rejection_reason
                        reasons[reason] += 1
                        removals.add(entry.item_id)
                        quarantines.append(
                            QuarantineRecord(
                                item_id=entry.item_id,
                                relative_path_hash=hashed_locator(entry.relative_path),
                                version_id=entry.version_tag,
                                reason=reason,
                            )
                        )
                        del prepared
                        continue
                    if prepared.text is None or prepared.content_hash is None:
                        raise WikiRefreshError(
                            "Wiki document worker returned incomplete output"
                        )
                    warnings.update(prepared.warnings)
                    text = prepared.text
                    effective_from, effective_to = _effective_dates(entry, text)
                    document = KnowledgeDocument(
                        drive_id=drive_id,
                        item_id=entry.item_id,
                        version_id=entry.version_tag,
                        etag=entry.etag or "",
                        ctag=entry.ctag or "",
                        display_name=entry.name,
                        relative_path=entry.relative_path,
                        content_hash=prepared.content_hash,
                        last_modified_at=entry.last_modified_at.isoformat(),
                        size=prepared.data_size,
                        mime_type=entry.mime_type or "application/octet-stream",
                        category=decision.category,
                        authority=decision.authority,
                        status=decision.status,
                        security_classification=decision.security_classification,
                        policy_version=policy_version,
                        effective_from=effective_from,
                        effective_to=effective_to,
                    )
                    chunks = chunk_text(
                        entry.item_id,
                        text,
                        chunk_chars=self.config.chunk_chars,
                        overlap_chars=self.config.chunk_overlap_chars,
                    )
                    del text
                    if not chunks:
                        reasons["no_chunks"] += 1
                        removals.add(entry.item_id)
                        quarantines.append(
                            QuarantineRecord(
                                item_id=entry.item_id,
                                relative_path_hash=hashed_locator(entry.relative_path),
                                version_id=entry.version_tag,
                                reason="no_chunks",
                            )
                        )
                        del prepared
                        continue
                    # Count exactly the chunk text that will be persisted,
                    # including intentional overlap. Capacity decisions remain
                    # in selected order on this coordinator thread.
                    persisted_chars = sum(len(chunk.text) for chunk in chunks)
                    if (
                        current_chars + persisted_chars
                        > self.config.max_generation_chars
                        or current_chunks + len(chunks)
                        > self.config.max_generation_chunks
                    ):
                        reasons["generation_text_capacity_limit"] += 1
                        removals.add(entry.item_id)
                        quarantines.append(
                            QuarantineRecord(
                                item_id=entry.item_id,
                                relative_path_hash=hashed_locator(entry.relative_path),
                                version_id=entry.version_tag,
                                reason="generation_text_capacity_limit",
                            )
                        )
                        del chunks
                        del prepared
                        continue
                    current_chars += persisted_chars
                    current_chunks += len(chunks)
                    upserts.append((document, chunks))
                    removals.discard(entry.item_id)
                    del prepared
                    if progress and (
                        position == 1
                        or position % 10 == 0
                        or position == len(selected)
                    ):
                        progress(
                            {
                                "stage": "extracting",
                                "processed": position,
                                "selected": len(selected),
                                "indexed": len(upserts),
                            }
                        )
                futures.clear()
                done.clear()
                not_done.clear()
                del future

        final_root = self.onedrive.get_entry(drive_id, root_item_id)
        if (
            final_root.drive_id != drive_id
            or final_root.item_id != root_item_id
            or not final_root.is_folder
            or not delta.root.version_tag
            or final_root.version_tag != delta.root.version_tag
        ):
            raise WikiRefreshError("OneDrive Wiki root changed during refresh")
        snapshot = self.index.apply_generation(
            drive_id=drive_id,
            root_item_id=root_item_id,
            root_version_tag=final_root.version_tag,
            policy_version=policy_version,
            delta_link=delta.delta_link,
            upserts=upserts,
            deleted_item_ids=removals,
            quarantines=quarantines,
            full_rebuild=full_rebuild,
            generation_lock=generation_lock,
        )
        return WikiRefreshReport(
            snapshot=snapshot,
            full_rebuild=full_rebuild,
            catalog_entries=len(delta.entries),
            changed_entries=len(delta.entries) + len(delta.deleted_item_ids),
            downloaded_documents=len(selected),
            downloaded_bytes=downloaded_bytes,
            indexed_documents=len(upserts),
            quarantined=len(quarantines),
            skipped_reasons=dict(sorted(reasons.items())),
            extraction_warnings=dict(sorted(warnings.items())),
        )

    def live_binding(
        self,
        refs: Iterable[KnowledgeRef] = (),
        *,
        verify_content: bool = False,
    ) -> dict[str, str | int]:
        drive_id, root_item_id = self._scope()
        snapshot = self.index.verify_integrity()
        if snapshot.drive_id != drive_id or snapshot.root_item_id != root_item_id:
            raise WikiRefreshError("Wiki index scope does not match configuration")
        if snapshot.policy_version != policy_fingerprint(self.config):
            raise WikiRefreshError("Wiki index policy does not match configuration")
        refs = tuple(refs)
        if refs and not self.index.validate_refs(refs):
            raise WikiRefreshError("Wiki references do not match the protected index")
        documents: dict[str, KnowledgeRef] = {}
        for ref in refs:
            if ref.drive_id != drive_id:
                raise WikiRefreshError("Wiki reference attempted to cross drives")
            documents.setdefault(ref.item_id, ref)
        for item_id, ref in sorted(documents.items()):
            current = self.onedrive.get_entry(drive_id, item_id)
            if (
                current.drive_id != drive_id
                or current.item_id != item_id
                or current.is_folder
                or not ref.version_id
                or not current.version_tag
                or current.version_tag != ref.version_id
                or (ref.etag and current.etag != ref.etag)
            ):
                raise WikiRefreshError("A cited OneDrive document changed")
            if verify_content:
                data = self.onedrive.download_current(drive_id, item_id)
                if current.size is not None and len(data) != current.size:
                    raise WikiRefreshError("A cited OneDrive document size changed")
                if hashlib.sha256(data).hexdigest() != ref.content_hash:
                    raise WikiRefreshError("A cited OneDrive document content changed")
        root = self.onedrive.get_entry(drive_id, root_item_id)
        if (
            root.drive_id != drive_id
            or root.item_id != root_item_id
            or not root.is_folder
        ):
            raise WikiRefreshError("The pinned OneDrive Wiki root is no longer valid")
        binding = snapshot.binding()
        binding["root_version_tag"] = root.version_tag
        return binding
