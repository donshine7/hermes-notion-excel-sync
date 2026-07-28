from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from notion_excel_sync.models import JsonValue


CORRECTION_AUTHORITY = "user_approved_correction"
CORRECTION_SCHEMA_VERSION = 1

_EVENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_BOUND_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_VALUE_DEPTH = 20
_MAX_VALUE_ITEMS = 10_000
_MAX_VALUE_BYTES = 64 * 1024
_FORBIDDEN_VALUE_KEYS = frozenset(
    {
        "access_token",
        "approval_nonce",
        "authorization_token",
        "email_body",
        "email_raw",
        "mail_body",
        "mail_raw",
        "nonce",
        "refresh_token",
        "raw_email",
        "token",
    }
)

_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


class CorrectionOverlayError(RuntimeError):
    """Raised when an approved-correction overlay cannot be safely published."""


class CorrectionValidationError(CorrectionOverlayError, ValueError):
    """Raised when a correction event is not a bounded JSON-safe record."""


class CorrectionEventConflict(CorrectionOverlayError):
    """Raised when an existing event ID is presented with different content."""


class CorrectionOverlayBusy(CorrectionOverlayError):
    """Raised when another process retains the correction publication lock."""


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CorrectionValidationError("Correction payload is not JSON-safe") from exc
    return rendered.encode("utf-8")


def _bounded_text(
    value: object,
    *,
    label: str,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise CorrectionValidationError(f"{label} must be a string")
    if not value or len(value) > maximum:
        raise CorrectionValidationError(
            f"{label} must contain between 1 and {maximum} characters"
        )
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise CorrectionValidationError(f"{label} contains control characters")
    return value


def _bound_identifier(value: object, *, label: str) -> str:
    text = _bounded_text(value, label=label, maximum=128)
    if _BOUND_ID_RE.fullmatch(text) is None or text in {".", ".."}:
        raise CorrectionValidationError(f"{label} is not a safe identifier")
    return text


def _digest(value: object, *, label: str) -> str:
    text = _bounded_text(value, label=label, maximum=64)
    if _DIGEST_RE.fullmatch(text) is None:
        raise CorrectionValidationError(f"{label} must be lowercase SHA-256")
    return text


def _validate_json_value(
    value: JsonValue,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if depth > _MAX_VALUE_DEPTH:
        raise CorrectionValidationError("Correction value exceeds maximum nesting")
    observed = counter if counter is not None else [0]
    observed[0] += 1
    if observed[0] > _MAX_VALUE_ITEMS:
        raise CorrectionValidationError("Correction value exceeds maximum item count")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CorrectionValidationError(
                "Correction value contains a non-finite number"
            )
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1, counter=observed)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CorrectionValidationError(
                    "Correction object keys must be strings"
                )
            folded = key.strip().casefold()
            if folded in _FORBIDDEN_VALUE_KEYS:
                raise CorrectionValidationError(
                    "Correction values must not contain tokens, approval nonces, "
                    "or raw email content"
                )
            _validate_json_value(item, depth=depth + 1, counter=observed)
        return
    raise CorrectionValidationError("Correction value is not JSON-safe")


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CorrectionValidationError(
            "approved_at must be a timezone-aware datetime"
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ApprovedCorrectionEvent:
    """One exact, user-approved operation suitable for an append-only overlay."""

    event_id: str
    proposal_id: str
    proposal_revision: int
    proposal_digest: str
    operation_id: str
    case_number: str
    target_database: str
    entity_key: str
    property_name: str
    current_value: JsonValue
    approved_value: JsonValue
    reason: str
    approved_by_user_id: str
    approved_in_chat_id: str
    approved_at: datetime
    source_refs_digest: str
    schema_version: int = field(default=CORRECTION_SCHEMA_VERSION, init=False)
    authority: str = field(default=CORRECTION_AUTHORITY, init=False)

    def __post_init__(self) -> None:
        if _EVENT_ID_RE.fullmatch(str(self.event_id)) is None:
            raise CorrectionValidationError(
                "event_id must be a lowercase path-safe identifier"
            )
        _bound_identifier(self.proposal_id, label="proposal_id")
        _bound_identifier(self.operation_id, label="operation_id")
        if (
            isinstance(self.proposal_revision, bool)
            or not isinstance(self.proposal_revision, int)
            or self.proposal_revision < 1
        ):
            raise CorrectionValidationError(
                "proposal_revision must be a positive integer"
            )
        _digest(self.proposal_digest, label="proposal_digest")
        _digest(self.source_refs_digest, label="source_refs_digest")
        _bounded_text(self.case_number, label="case_number", maximum=256)
        _bounded_text(
            self.target_database,
            label="target_database",
            maximum=256,
        )
        _bounded_text(self.entity_key, label="entity_key", maximum=512)
        _bounded_text(self.property_name, label="property_name", maximum=256)
        _bounded_text(self.reason, label="reason", maximum=4_000)
        _bounded_text(
            self.approved_by_user_id,
            label="approved_by_user_id",
            maximum=128,
        )
        _bounded_text(
            self.approved_in_chat_id,
            label="approved_in_chat_id",
            maximum=128,
        )
        _utc_text(self.approved_at)
        _validate_json_value(self.current_value)
        _validate_json_value(self.approved_value)
        if len(_canonical_json(self.current_value)) > _MAX_VALUE_BYTES:
            raise CorrectionValidationError("current_value exceeds maximum size")
        if len(_canonical_json(self.approved_value)) > _MAX_VALUE_BYTES:
            raise CorrectionValidationError("approved_value exceeds maximum size")

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "authority": self.authority,
            "event_id": self.event_id,
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "proposal_digest": self.proposal_digest,
            "operation_id": self.operation_id,
            "case_number": self.case_number,
            "target_database": self.target_database,
            "entity_key": self.entity_key,
            "property_name": self.property_name,
            "current_value": self.current_value,
            "approved_value": self.approved_value,
            "reason": self.reason,
            "approved_by_user_id": self.approved_by_user_id,
            "approved_in_chat_id": self.approved_in_chat_id,
            "approved_at": _utc_text(self.approved_at),
            "source_refs_digest": self.source_refs_digest,
        }

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.as_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> ApprovedCorrectionEvent:
        if not isinstance(value, dict):
            raise CorrectionValidationError("Correction event JSON must be an object")
        expected = {
            "schema_version",
            "authority",
            "event_id",
            "proposal_id",
            "proposal_revision",
            "proposal_digest",
            "operation_id",
            "case_number",
            "target_database",
            "entity_key",
            "property_name",
            "current_value",
            "approved_value",
            "reason",
            "approved_by_user_id",
            "approved_in_chat_id",
            "approved_at",
            "source_refs_digest",
        }
        if set(value) != expected:
            raise CorrectionValidationError(
                "Correction event JSON has unexpected or missing fields"
            )
        if (
            value.get("schema_version") != CORRECTION_SCHEMA_VERSION
            or value.get("authority") != CORRECTION_AUTHORITY
        ):
            raise CorrectionValidationError(
                "Correction event schema or authority is invalid"
            )
        raw_approved_at = value.get("approved_at")
        if not isinstance(raw_approved_at, str):
            raise CorrectionValidationError("approved_at must be an ISO timestamp")
        try:
            approved_at = datetime.fromisoformat(
                raw_approved_at[:-1] + "+00:00"
                if raw_approved_at.endswith("Z")
                else raw_approved_at
            )
        except ValueError as exc:
            raise CorrectionValidationError(
                "approved_at must be an ISO timestamp"
            ) from exc
        return cls(
            event_id=value["event_id"],
            proposal_id=value["proposal_id"],
            proposal_revision=value["proposal_revision"],
            proposal_digest=value["proposal_digest"],
            operation_id=value["operation_id"],
            case_number=value["case_number"],
            target_database=value["target_database"],
            entity_key=value["entity_key"],
            property_name=value["property_name"],
            current_value=value["current_value"],
            approved_value=value["approved_value"],
            reason=value["reason"],
            approved_by_user_id=value["approved_by_user_id"],
            approved_in_chat_id=value["approved_in_chat_id"],
            approved_at=approved_at,
            source_refs_digest=value["source_refs_digest"],
        )


@dataclass(frozen=True, slots=True)
class CorrectionOverlayPublishResult:
    event_id: str
    event_digest: str
    event_created: bool
    generation: str
    event_count: int
    event_path: Path
    catalog_path: Path
    current_path: Path


def _prospective_resolve(path: Path) -> Path:
    missing: list[str] = []
    current = path
    while not current.exists():
        if current.parent == current:
            raise CorrectionOverlayError("Output root has no accessible parent")
        missing.append(current.name)
        current = current.parent
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise CorrectionOverlayError("Output root parent is not accessible") from exc
    for name in reversed(missing):
        resolved /= name
    return Path(os.path.abspath(resolved))


class CorrectionOverlayStore:
    """Publish immutable correction events and deterministic catalog generations."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        requested = Path(output_root)
        if not requested.is_absolute():
            raise CorrectionOverlayError("Correction output root must be absolute")
        if (
            not math.isfinite(lock_timeout_seconds)
            or lock_timeout_seconds <= 0
            or lock_timeout_seconds > 300
        ):
            raise ValueError(
                "lock_timeout_seconds must be greater than zero and at most 300"
            )
        prospective = _prospective_resolve(Path(os.path.abspath(requested)))
        try:
            prospective.mkdir(parents=True, exist_ok=True)
            output = prospective.resolve(strict=True)
        except OSError as exc:
            raise CorrectionOverlayError(
                "Correction output root cannot be created"
            ) from exc
        if not output.is_dir():
            raise CorrectionOverlayError("Correction output root is not a directory")
        corrections = output / "corrections"
        if corrections.is_symlink():
            raise CorrectionOverlayError(
                "Correction layer must not be a symbolic link"
            )
        try:
            corrections.mkdir(parents=True, exist_ok=True)
            corrections = corrections.resolve(strict=True)
            corrections.relative_to(output)
        except (OSError, ValueError) as exc:
            raise CorrectionOverlayError(
                "Correction layer escaped the output root"
            ) from exc
        self.output_root = output
        self.corrections_root = corrections
        self.lock_timeout_seconds = float(lock_timeout_seconds)

    @staticmethod
    def _process_lock(path: Path) -> threading.Lock:
        key = os.path.normcase(os.fspath(path))
        with _PROCESS_LOCK_GUARD:
            return _PROCESS_LOCKS.setdefault(key, threading.Lock())

    @staticmethod
    def _try_file_lock(stream: object) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    @staticmethod
    def _unlock_file(stream: object) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _publication_lock(self) -> Iterator[None]:
        lock_path = self.corrections_root / ".publish.lock"
        local_lock = self._process_lock(lock_path)
        if not local_lock.acquire(timeout=self.lock_timeout_seconds):
            raise CorrectionOverlayBusy("Correction publication is busy")
        stream = None
        acquired = False
        try:
            stream = lock_path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            deadline = time.monotonic() + self.lock_timeout_seconds
            while not acquired:
                acquired = self._try_file_lock(stream)
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise CorrectionOverlayBusy("Correction publication is busy")
                time.sleep(0.05)
            yield
        finally:
            if acquired and stream is not None:
                try:
                    self._unlock_file(stream)
                except OSError:
                    pass
            if stream is not None:
                stream.close()
            local_lock.release()

    @staticmethod
    def _assert_managed_directory(path: Path, *, root: Path) -> None:
        if path.is_symlink():
            raise CorrectionOverlayError(
                "Correction storage directories must not be symbolic links"
            )
        try:
            path.mkdir(parents=True, exist_ok=True)
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise CorrectionOverlayError(
                "Correction storage escaped its managed root"
            ) from exc
        if not resolved.is_dir():
            raise CorrectionOverlayError(
                "Correction storage path is not a directory"
            )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
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

    @staticmethod
    def _read_regular_file(path: Path, *, label: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise CorrectionOverlayError(f"{label} is not a regular file")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise CorrectionOverlayError(f"{label} cannot be read") from exc

    def _persist_event(
        self,
        event: ApprovedCorrectionEvent,
        events_root: Path,
    ) -> tuple[Path, bytes, bool]:
        event_path = events_root / f"{event.event_id}.json"
        expected = _canonical_json(event.as_dict())
        if event_path.exists() or event_path.is_symlink():
            existing = self._read_regular_file(
                event_path,
                label="Existing correction event",
            )
            if existing != expected:
                raise CorrectionEventConflict(
                    "The correction event ID already exists with different content"
                )
            return event_path, existing, False
        self._atomic_write(event_path, expected)
        persisted = self._read_regular_file(
            event_path,
            label="Published correction event",
        )
        if persisted != expected:
            raise CorrectionOverlayError(
                "Published correction event failed verification"
            )
        return event_path, persisted, True

    def _catalog_events(self, events_root: Path) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        try:
            children = sorted(events_root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise CorrectionOverlayError(
                "Correction event directory cannot be enumerated"
            ) from exc
        for path in children:
            if path.name.startswith(".tmp-") and path.name.endswith(".tmp"):
                continue
            if path.suffix != ".json" or _EVENT_ID_RE.fullmatch(path.stem) is None:
                raise CorrectionOverlayError(
                    "Correction event directory contains an unexpected entry"
                )
            data = self._read_regular_file(path, label="Correction event")
            try:
                raw = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CorrectionOverlayError(
                    "Correction event JSON is invalid"
                ) from exc
            event = ApprovedCorrectionEvent.from_dict(raw)
            canonical = _canonical_json(event.as_dict())
            if event.event_id != path.stem or data != canonical:
                raise CorrectionOverlayError(
                    "Correction event failed its filename or canonical binding"
                )
            rows.append(
                {
                    "event_id": event.event_id,
                    "payload_digest": hashlib.sha256(data).hexdigest(),
                }
            )
        return rows

    def _publish_catalog(
        self,
        rows: list[dict[str, str]],
        generations_root: Path,
    ) -> tuple[str, Path]:
        seed = {
            "schema_version": CORRECTION_SCHEMA_VERSION,
            "authority": CORRECTION_AUTHORITY,
            "event_count": len(rows),
            "events": rows,
        }
        generation = hashlib.sha256(_canonical_json(seed)).hexdigest()
        catalog = {**seed, "generation": generation}
        expected = _canonical_json(catalog)
        target = generations_root / generation
        catalog_path = target / "catalog.json"
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise CorrectionOverlayError(
                    "Correction catalog generation target is invalid"
                )
            existing = self._read_regular_file(
                catalog_path,
                label="Correction catalog",
            )
            if existing != expected:
                raise CorrectionOverlayError(
                    "Correction catalog generation failed its immutable binding"
                )
            return generation, catalog_path

        staging = generations_root / f".staging-{uuid4().hex}"
        try:
            staging.mkdir(parents=False, exist_ok=False)
            self._atomic_write(staging / "catalog.json", expected)
            os.replace(staging, target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        persisted = self._read_regular_file(
            catalog_path,
            label="Published correction catalog",
        )
        if persisted != expected:
            raise CorrectionOverlayError(
                "Published correction catalog failed verification"
            )
        return generation, catalog_path

    def publish(
        self,
        event: ApprovedCorrectionEvent,
    ) -> CorrectionOverlayPublishResult:
        if not isinstance(event, ApprovedCorrectionEvent):
            raise CorrectionValidationError(
                "publish requires an ApprovedCorrectionEvent"
            )
        with self._publication_lock():
            events_root = self.corrections_root / "events"
            generations_root = self.corrections_root / "generations"
            self._assert_managed_directory(
                events_root,
                root=self.corrections_root,
            )
            self._assert_managed_directory(
                generations_root,
                root=self.corrections_root,
            )
            event_path, event_bytes, event_created = self._persist_event(
                event,
                events_root,
            )
            rows = self._catalog_events(events_root)
            generation, catalog_path = self._publish_catalog(
                rows,
                generations_root,
            )
            current_path = self.corrections_root / "CURRENT"
            if current_path.is_symlink() or current_path.is_dir():
                raise CorrectionOverlayError(
                    "Correction CURRENT pointer is not a regular file"
                )
            self._atomic_write(current_path, f"{generation}\n".encode("ascii"))
            if self._read_regular_file(
                current_path,
                label="Correction CURRENT pointer",
            ) != f"{generation}\n".encode("ascii"):
                raise CorrectionOverlayError(
                    "Correction CURRENT pointer failed verification"
                )
            return CorrectionOverlayPublishResult(
                event_id=event.event_id,
                event_digest=hashlib.sha256(event_bytes).hexdigest(),
                event_created=event_created,
                generation=generation,
                event_count=len(rows),
                event_path=event_path,
                catalog_path=catalog_path,
                current_path=current_path,
            )


__all__ = [
    "ApprovedCorrectionEvent",
    "CORRECTION_AUTHORITY",
    "CORRECTION_SCHEMA_VERSION",
    "CorrectionEventConflict",
    "CorrectionOverlayBusy",
    "CorrectionOverlayError",
    "CorrectionOverlayPublishResult",
    "CorrectionOverlayStore",
    "CorrectionValidationError",
]
