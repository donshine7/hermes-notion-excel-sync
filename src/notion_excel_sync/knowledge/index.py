from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Iterator

from notion_excel_sync.knowledge.models import (
    CHUNKER_VERSION,
    EXTRACTOR_VERSION,
    TOKENIZER_VERSION,
    WIKI_SCHEMA_VERSION,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeHit,
    KnowledgeQuery,
    KnowledgeSnapshot,
    QuarantineRecord,
)
from notion_excel_sync.models import KnowledgeRef, canonical_json, sha256_json


class WikiIndexError(RuntimeError):
    pass


class WikiGenerationLockTimeout(WikiIndexError):
    pass


@dataclass(slots=True)
class WikiGenerationLease:
    """Capability proving that this thread owns an index generation lock."""

    _index_path: str
    _owner_thread_id: int
    _active: bool = True


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_ACTIVE_LEASES: dict[int, WikiGenerationLease] = {}
_CHUNK_LOCATOR_RE = re.compile(r"chars:(0|[1-9][0-9]*)-(0|[1-9][0-9]*)\Z")
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_AUTHORITY_RANK = {
    "official": 50,
    "official_candidate": 40,
    "program_rule": 35,
    "program_rule_candidate": 30,
    "office_policy": 25,
    "office_policy_candidate": 20,
    "reference": 10,
}
_STATUS_RANK = {"verified": 10, "unverified": 0}


def _effective_date_rank(value: object) -> int:
    normalized = str(value or "").replace("-", "")
    return int(normalized) if normalized.isdigit() else -1


def search_tokens(value: str, *, max_tokens: int = 512) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", value).casefold()
    tokens: set[str] = set()
    for word in _WORD_RE.findall(normalized):
        if len(word) <= 32:
            tokens.add(word)
        for width in (2, 3):
            if len(word) < width:
                continue
            tokens.update(word[index : index + width] for index in range(len(word) - width + 1))
        if len(tokens) >= max_tokens:
            break
    return tuple(sorted(tokens)[:max_tokens])


def chunk_text(
    item_id: str,
    text: str,
    *,
    chunk_chars: int,
    overlap_chars: int,
) -> tuple[KnowledgeChunk, ...]:
    if chunk_chars <= 0 or overlap_chars < 0 or overlap_chars >= chunk_chars:
        raise ValueError("invalid chunk limits")
    normalized = unicodedata.normalize("NFC", text).strip()
    if not normalized:
        return ()
    chunks: list[KnowledgeChunk] = []
    start = 0
    ordinal = 0
    while start < len(normalized):
        hard_end = min(len(normalized), start + chunk_chars)
        end = hard_end
        if hard_end < len(normalized):
            boundary_floor = start + max(1, int(chunk_chars * 0.6))
            candidates = [
                normalized.rfind("\n\n", boundary_floor, hard_end),
                normalized.rfind("\n", boundary_floor, hard_end),
                normalized.rfind(". ", boundary_floor, hard_end),
            ]
            boundary = max(candidates)
            if boundary >= boundary_floor:
                end = boundary + (2 if normalized[boundary : boundary + 2] == ". " else 1)
        value = normalized[start:end].strip()
        if value:
            chunk_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
            chunk_id = hashlib.sha256(
                f"{item_id}\0{ordinal}\0{chunk_hash}".encode("utf-8")
            ).hexdigest()
            chunks.append(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    item_id=item_id,
                    ordinal=ordinal,
                    locator=f"chars:{start}-{end}",
                    chunk_hash=chunk_hash,
                    text=value,
                    search_terms=" ".join(search_tokens(value)),
                )
            )
            ordinal += 1
        if end >= len(normalized):
            break
        next_start = end - overlap_chars
        start = next_start if next_start > start else end
    return tuple(chunks)


class WikiIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    @property
    def generation_lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.generation.lock")

    def _lock_key(self) -> str:
        return os.path.normcase(str(self.path))

    @staticmethod
    def _process_lock(key: str) -> threading.Lock:
        with _PROCESS_LOCKS_GUARD:
            return _PROCESS_LOCKS.setdefault(key, threading.Lock())

    @staticmethod
    def _try_lock_file(stream: BinaryIO) -> bool:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True

        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False
        return True

    @staticmethod
    def _unlock_file(stream: BinaryIO) -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def generation_lock(
        self,
        *,
        timeout_seconds: float = 3_600.0,
        poll_seconds: float = 0.05,
    ) -> Iterator[WikiGenerationLease]:
        """Serialize a complete generation transition across threads/processes."""

        if timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("invalid Wiki generation lock timeout")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        local_lock = self._process_lock(self._lock_key())
        if not local_lock.acquire(timeout=max(0.0, timeout_seconds)):
            raise WikiGenerationLockTimeout("Wiki generation is busy")
        stream: BinaryIO | None = None
        lease: WikiGenerationLease | None = None
        try:
            stream = self.generation_lock_path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            acquired = False
            while not acquired:
                acquired = self._try_lock_file(stream)
                if acquired:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WikiGenerationLockTimeout("Wiki generation is busy")
                time.sleep(min(poll_seconds, remaining))
            lease = WikiGenerationLease(
                _index_path=self._lock_key(),
                _owner_thread_id=threading.get_ident(),
            )
            with _PROCESS_LOCKS_GUARD:
                _ACTIVE_LEASES[id(lease)] = lease
            yield lease
        finally:
            if lease is not None:
                with _PROCESS_LOCKS_GUARD:
                    _ACTIVE_LEASES.pop(id(lease), None)
                lease._active = False
            try:
                if stream is not None:
                    try:
                        if lease is not None:
                            self._unlock_file(stream)
                    finally:
                        stream.close()
            finally:
                local_lock.release()

    def _assert_generation_lease(self, lease: WikiGenerationLease) -> None:
        with _PROCESS_LOCKS_GUARD:
            registered = _ACTIVE_LEASES.get(id(lease)) is lease
        if (
            not registered
            or not lease._active
            or lease._index_path != self._lock_key()
            or lease._owner_thread_id != threading.get_ident()
        ):
            raise WikiIndexError("Wiki generation lock lease is not valid")

    @staticmethod
    def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        if not read_only:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS document(
                item_id TEXT PRIMARY KEY,
                drive_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                etag TEXT NOT NULL,
                ctag TEXT NOT NULL,
                display_name TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                last_modified_at TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                category TEXT NOT NULL,
                authority TEXT NOT NULL,
                status TEXT NOT NULL,
                security_classification TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                effective_from TEXT,
                effective_to TEXT
            );
            CREATE TABLE IF NOT EXISTS chunk(
                chunk_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES document(item_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                locator TEXT NOT NULL,
                chunk_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                search_terms TEXT NOT NULL,
                UNIQUE(item_id, ordinal)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                chunk_id UNINDEXED,
                search_terms,
                tokenize='unicode61 remove_diacritics 0'
            );
            CREATE TABLE IF NOT EXISTS quarantine(
                item_id TEXT PRIMARY KEY,
                relative_path_hash TEXT NOT NULL,
                version_id TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_document_category_status
                ON document(category, status);
            CREATE INDEX IF NOT EXISTS idx_chunk_item ON chunk(item_id, ordinal);
            """
        )

    @staticmethod
    def _set_metadata(
        connection: sqlite3.Connection, key: str, value: object
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
        )

    @staticmethod
    def _delete_item(connection: sqlite3.Connection, item_id: str) -> None:
        chunk_ids = [
            str(row["chunk_id"])
            for row in connection.execute(
                "SELECT chunk_id FROM chunk WHERE item_id = ?", (item_id,)
            )
        ]
        if chunk_ids:
            connection.executemany(
                "DELETE FROM chunk_fts WHERE chunk_id = ?",
                ((chunk_id,) for chunk_id in chunk_ids),
            )
        connection.execute("DELETE FROM document WHERE item_id = ?", (item_id,))
        connection.execute("DELETE FROM quarantine WHERE item_id = ?", (item_id,))

    @staticmethod
    def _insert_document(
        connection: sqlite3.Connection,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        if not chunks:
            raise WikiIndexError("An indexed document must have at least one chunk")
        connection.execute(
            """
            INSERT INTO document(
                item_id, drive_id, version_id, etag, ctag, display_name,
                relative_path, content_hash, last_modified_at, size, mime_type,
                category, authority, status, security_classification,
                policy_version, extractor_version, effective_from, effective_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.item_id,
                document.drive_id,
                document.version_id,
                document.etag,
                document.ctag,
                document.display_name,
                document.relative_path,
                document.content_hash,
                document.last_modified_at,
                document.size,
                document.mime_type,
                document.category,
                document.authority,
                document.status,
                document.security_classification,
                document.policy_version,
                document.extractor_version,
                document.effective_from,
                document.effective_to,
            ),
        )
        for chunk in sorted(chunks, key=lambda item: (item.ordinal, item.chunk_id)):
            if chunk.item_id != document.item_id:
                raise WikiIndexError("Chunk/document item IDs do not match")
            connection.execute(
                """
                INSERT INTO chunk(
                    chunk_id, item_id, ordinal, locator, chunk_hash, text, search_terms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.chunk_id,
                    chunk.item_id,
                    chunk.ordinal,
                    chunk.locator,
                    chunk.chunk_hash,
                    chunk.text,
                    chunk.search_terms,
                ),
            )
            connection.execute(
                "INSERT INTO chunk_fts(chunk_id, search_terms) VALUES (?, ?)",
                (chunk.chunk_id, chunk.search_terms),
            )

    @staticmethod
    def _generation_digest(
        connection: sqlite3.Connection,
        *,
        drive_id: str,
        root_item_id: str,
        root_version_tag: str,
        policy_version: str,
        extractor_version: str = EXTRACTOR_VERSION,
        chunker_version: str = CHUNKER_VERSION,
        tokenizer_version: str = TOKENIZER_VERSION,
    ) -> str:
        documents = [
            dict(row)
            for row in connection.execute(
                """
                SELECT item_id, version_id, etag, ctag, relative_path, content_hash,
                       category, authority, status, security_classification,
                       effective_from, effective_to, extractor_version, policy_version
                FROM document ORDER BY item_id
                """
            )
        ]
        chunks = [
            dict(row)
            for row in connection.execute(
                "SELECT chunk_id, item_id, ordinal, locator, chunk_hash "
                "FROM chunk ORDER BY item_id, ordinal, chunk_id"
            )
        ]
        quarantines = [
            dict(row)
            for row in connection.execute(
                "SELECT item_id, relative_path_hash, version_id, reason "
                "FROM quarantine ORDER BY item_id"
            )
        ]
        return sha256_json(
            {
                "schema_version": WIKI_SCHEMA_VERSION,
                "drive_id": drive_id,
                "root_item_id": root_item_id,
                "root_version_tag": root_version_tag,
                "policy_version": policy_version,
                "extractor_version": extractor_version,
                "chunker_version": chunker_version,
                "tokenizer_version": tokenizer_version,
                "documents": documents,
                "chunks": chunks,
                "quarantines": quarantines,
            }
        )

    def apply_generation(
        self,
        *,
        drive_id: str,
        root_item_id: str,
        root_version_tag: str,
        policy_version: str,
        delta_link: str,
        upserts: Sequence[tuple[KnowledgeDocument, Sequence[KnowledgeChunk]]],
        deleted_item_ids: Iterable[str] = (),
        quarantines: Sequence[QuarantineRecord] = (),
        full_rebuild: bool = False,
        created_at: datetime | None = None,
        generation_lock: WikiGenerationLease | None = None,
    ) -> KnowledgeSnapshot:
        if generation_lock is None:
            with self.generation_lock() as acquired:
                return self.apply_generation(
                    drive_id=drive_id,
                    root_item_id=root_item_id,
                    root_version_tag=root_version_tag,
                    policy_version=policy_version,
                    delta_link=delta_link,
                    upserts=upserts,
                    deleted_item_ids=deleted_item_ids,
                    quarantines=quarantines,
                    full_rebuild=full_rebuild,
                    created_at=created_at,
                    generation_lock=acquired,
                )
        self._assert_generation_lease(generation_lock)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staging_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".staging", dir=self.path.parent
        )
        os.close(descriptor)
        staging = Path(staging_name)
        try:
            destination = self._connect(staging)
            try:
                if self.path.exists() and not full_rebuild:
                    # Never clone an index whose metadata, chunks, or FTS table
                    # no longer match its immutable generation digest.
                    self.verify_integrity()
                    source = self._connect(self.path, read_only=True)
                    try:
                        source.backup(destination)
                    finally:
                        source.close()
                self._initialize(destination)
                with destination:
                    if full_rebuild:
                        destination.execute("DELETE FROM chunk_fts")
                        destination.execute("DELETE FROM chunk")
                        destination.execute("DELETE FROM document")
                        destination.execute("DELETE FROM quarantine")
                        destination.execute("DELETE FROM metadata")
                    changed_ids = {
                        document.item_id for document, _ in upserts
                    } | {str(item_id) for item_id in deleted_item_ids}
                    for item_id in sorted(changed_ids):
                        self._delete_item(destination, item_id)
                    for record in sorted(quarantines, key=lambda item: item.item_id):
                        destination.execute(
                            "INSERT OR REPLACE INTO quarantine(" 
                            "item_id, relative_path_hash, version_id, reason"
                            ") VALUES (?, ?, ?, ?)",
                            (
                                record.item_id,
                                record.relative_path_hash,
                                record.version_id,
                                record.reason,
                            ),
                        )
                    for document, chunks in sorted(
                        upserts, key=lambda item: item[0].item_id
                    ):
                        self._insert_document(destination, document, chunks)
                    generation_digest = self._generation_digest(
                        destination,
                        drive_id=drive_id,
                        root_item_id=root_item_id,
                        root_version_tag=root_version_tag,
                        policy_version=policy_version,
                    )
                    created = (created_at or datetime.now(UTC)).astimezone(UTC)
                    metadata = {
                        "schema_version": WIKI_SCHEMA_VERSION,
                        "generation_digest": generation_digest,
                        "drive_id": drive_id,
                        "root_item_id": root_item_id,
                        "root_version_tag": root_version_tag,
                        "policy_version": policy_version,
                        "extractor_version": EXTRACTOR_VERSION,
                        "chunker_version": CHUNKER_VERSION,
                        "tokenizer_version": TOKENIZER_VERSION,
                        "created_at": created.isoformat(),
                        "delta_link": delta_link,
                    }
                    for key, value in metadata.items():
                        self._set_metadata(destination, key, value)
                self._verify_connection(destination)
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                destination.close()
            os.replace(staging, self.path)
            return self.verify_integrity()
        finally:
            staging.unlink(missing_ok=True)

    @staticmethod
    def _metadata(connection: sqlite3.Connection) -> dict[str, object]:
        result: dict[str, object] = {}
        for row in connection.execute("SELECT key, value FROM metadata"):
            try:
                result[str(row["key"])] = json.loads(str(row["value"]))
            except json.JSONDecodeError as exc:
                raise WikiIndexError("Wiki metadata is corrupt") from exc
        return result

    @classmethod
    def _verify_connection(cls, connection: sqlite3.Connection) -> KnowledgeSnapshot:
        integrity_rows = list(connection.execute("PRAGMA integrity_check"))
        if not integrity_rows or any(
            str(row[0]).casefold() != "ok" for row in integrity_rows
        ):
            raise WikiIndexError("Wiki SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise WikiIndexError("Wiki SQLite foreign-key check failed")

        metadata = cls._metadata(connection)
        required = {
            "schema_version",
            "generation_digest",
            "drive_id",
            "root_item_id",
            "root_version_tag",
            "policy_version",
            "extractor_version",
            "chunker_version",
            "tokenizer_version",
            "created_at",
        }
        if required - metadata.keys():
            raise WikiIndexError("Wiki metadata is incomplete")
        required_runtime_versions = {
            "schema_version": WIKI_SCHEMA_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "tokenizer_version": TOKENIZER_VERSION,
        }
        if any(
            metadata.get(key) != value
            for key, value in required_runtime_versions.items()
        ):
            raise WikiIndexError("Wiki metadata version does not match this runtime")
        drive_id = str(metadata["drive_id"])
        root_item_id = str(metadata["root_item_id"])
        root_version_tag = str(metadata["root_version_tag"])
        policy_version = str(metadata["policy_version"])
        extractor_version = str(metadata["extractor_version"])
        stored_digest = str(metadata["generation_digest"])
        if (
            not drive_id
            or not root_item_id
            or not root_version_tag
            or not policy_version
            or not extractor_version
            or _HEX_SHA256_RE.fullmatch(stored_digest) is None
        ):
            raise WikiIndexError("Wiki metadata contains invalid binding values")

        document_rows = list(
            connection.execute(
                "SELECT item_id, drive_id, policy_version, extractor_version "
                "FROM document ORDER BY item_id"
            )
        )
        for row in document_rows:
            if (
                str(row["drive_id"]) != drive_id
                or str(row["policy_version"]) != policy_version
                or str(row["extractor_version"]) != extractor_version
            ):
                raise WikiIndexError("Wiki document binding is inconsistent")

        chunk_rows = list(
            connection.execute(
                "SELECT chunk_id, item_id, ordinal, locator, chunk_hash, text, "
                "search_terms FROM chunk ORDER BY item_id, ordinal, chunk_id"
            )
        )
        expected_ordinal: dict[str, int] = {}
        expected_fts: Counter[tuple[str, str]] = Counter()
        for row in chunk_rows:
            item_id = str(row["item_id"])
            ordinal = int(row["ordinal"])
            locator = str(row["locator"])
            chunk_hash = str(row["chunk_hash"])
            text = str(row["text"])
            terms = str(row["search_terms"])
            locator_match = _CHUNK_LOCATOR_RE.fullmatch(locator)
            if locator_match is None or int(locator_match.group(1)) >= int(
                locator_match.group(2)
            ):
                raise WikiIndexError("Wiki chunk locator is invalid")
            if ordinal != expected_ordinal.get(item_id, 0):
                raise WikiIndexError("Wiki chunk ordinals are not contiguous")
            expected_ordinal[item_id] = ordinal + 1
            if not text or text != text.strip() or unicodedata.normalize("NFC", text) != text:
                raise WikiIndexError("Wiki chunk text is not canonical")
            recomputed_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if (
                _HEX_SHA256_RE.fullmatch(chunk_hash) is None
                or not hmac.compare_digest(recomputed_hash, chunk_hash)
            ):
                raise WikiIndexError("Wiki chunk hash does not match its text")
            recomputed_id = hashlib.sha256(
                f"{item_id}\0{ordinal}\0{chunk_hash}".encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(recomputed_id, str(row["chunk_id"])):
                raise WikiIndexError("Wiki chunk ID is invalid")
            expected_terms = " ".join(search_tokens(text))
            if expected_terms != terms:
                raise WikiIndexError("Wiki chunk search terms do not match its text")
            expected_fts[(str(row["chunk_id"]), terms)] += 1

        documents_without_chunks = connection.execute(
            "SELECT 1 FROM document LEFT JOIN chunk USING(item_id) "
            "WHERE chunk.item_id IS NULL LIMIT 1"
        ).fetchone()
        if documents_without_chunks:
            raise WikiIndexError("Wiki document has no chunks")
        actual_fts = Counter(
            (str(row["chunk_id"]), str(row["search_terms"]))
            for row in connection.execute(
                "SELECT chunk_id, search_terms FROM chunk_fts"
            )
        )
        if actual_fts != expected_fts:
            raise WikiIndexError("Wiki full-text index does not match its chunks")

        recomputed_digest = cls._generation_digest(
            connection,
            drive_id=drive_id,
            root_item_id=root_item_id,
            root_version_tag=root_version_tag,
            policy_version=policy_version,
            extractor_version=extractor_version,
            chunker_version=str(metadata["chunker_version"]),
            tokenizer_version=str(metadata["tokenizer_version"]),
        )
        if not hmac.compare_digest(recomputed_digest, stored_digest):
            raise WikiIndexError("Wiki generation digest does not match its contents")

        created_at = datetime.fromisoformat(str(metadata["created_at"]))
        if created_at.tzinfo is None:
            raise WikiIndexError("Wiki creation time must include a time zone")
        document_count = len(document_rows)
        chunk_count = len(chunk_rows)
        quarantine_count = int(
            connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        )
        return KnowledgeSnapshot(
            generation_digest=str(metadata["generation_digest"]),
            drive_id=drive_id,
            root_item_id=root_item_id,
            root_version_tag=root_version_tag,
            policy_version=policy_version,
            extractor_version=extractor_version,
            chunker_version=str(metadata["chunker_version"]),
            tokenizer_version=str(metadata["tokenizer_version"]),
            created_at=created_at,
            document_count=document_count,
            chunk_count=chunk_count,
            quarantine_count=quarantine_count,
            delta_link=str(metadata.get("delta_link") or ""),
        )

    def verify_integrity(self) -> KnowledgeSnapshot:
        """Verify physical and semantic integrity of a complete Wiki generation."""

        if not self.path.exists():
            raise WikiIndexError(f"Wiki index does not exist: {self.path}")
        try:
            connection = self._connect(self.path, read_only=True)
            try:
                return self._verify_connection(connection)
            finally:
                connection.close()
        except WikiIndexError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise WikiIndexError("Wiki index integrity verification failed") from exc

    def status(self) -> KnowledgeSnapshot:
        return self.verify_integrity()

    def query(self, query: KnowledgeQuery) -> tuple[KnowledgeHit, ...]:
        if query.top_k <= 0 or query.top_k > 50:
            raise ValueError("Wiki query top_k must be between 1 and 50")
        tokens = search_tokens(query.text, max_tokens=64)
        if not tokens:
            return ()
        expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        clauses = ["chunk_fts MATCH ?"]
        parameters: list[object] = [expression]
        if query.categories:
            placeholders = ",".join("?" for _ in query.categories)
            clauses.append(f"document.category IN ({placeholders})")
            parameters.extend(query.categories)
        if query.statuses:
            placeholders = ",".join("?" for _ in query.statuses)
            clauses.append(f"document.status IN ({placeholders})")
            parameters.extend(query.statuses)
        if query.security_classifications:
            placeholders = ",".join("?" for _ in query.security_classifications)
            clauses.append(f"document.security_classification IN ({placeholders})")
            parameters.extend(query.security_classifications)
        if query.effective_on:
            clauses.append("document.effective_from IS NOT NULL")
            clauses.append("document.effective_to IS NOT NULL")
            clauses.append("document.effective_from <= ?")
            clauses.append("document.effective_to >= ?")
            parameters.extend((query.effective_on, query.effective_on))
        parameters.append(query.top_k * 25)
        sql = f"""
            SELECT chunk.chunk_id, chunk.locator, chunk.chunk_hash, chunk.text,
                   document.*, bm25(chunk_fts) AS rank
            FROM chunk_fts
            JOIN chunk ON chunk.chunk_id = chunk_fts.chunk_id
            JOIN document ON document.item_id = chunk.item_id
            WHERE {' AND '.join(clauses)}
            ORDER BY rank ASC
            LIMIT ?
        """
        connection = self._connect(self.path, read_only=True)
        try:
            rows = list(connection.execute(sql, parameters))
        finally:
            connection.close()
        rows.sort(
            key=lambda row: (
                float(row["rank"]),
                -_AUTHORITY_RANK.get(str(row["authority"]), 0),
                -_STATUS_RANK.get(str(row["status"]), 0),
                -_effective_date_rank(row["effective_from"]),
                str(row["item_id"]),
                str(row["chunk_id"]),
            )
        )
        raw_words = _WORD_RE.findall(unicodedata.normalize("NFC", query.text).casefold())
        hits: list[KnowledgeHit] = []
        seen_documents: set[str] = set()
        for row in rows:
            item_id = str(row["item_id"])
            if item_id in seen_documents:
                continue
            seen_documents.add(item_id)
            text = str(row["text"])
            folded = text.casefold()
            positions = [folded.find(word) for word in raw_words if folded.find(word) >= 0]
            start = max(0, (min(positions) if positions else 0) - 100)
            excerpt = " ".join(text[start : start + 480].split())
            ref = KnowledgeRef(
                drive_id=str(row["drive_id"]),
                item_id=item_id,
                version_id=str(row["version_id"]),
                content_hash=str(row["content_hash"]),
                chunk_locator=str(row["locator"]),
                chunk_hash=str(row["chunk_hash"]),
                security_classification=str(row["security_classification"]),
                display_name=str(row["display_name"]),
                etag=str(row["etag"]),
                extractor_version=str(row["extractor_version"]),
                policy_version=str(row["policy_version"]),
                effective_from=(
                    str(row["effective_from"]) if row["effective_from"] else None
                ),
                effective_to=str(row["effective_to"]) if row["effective_to"] else None,
                authority=str(row["authority"]),
                status=str(row["status"]),
            )
            hits.append(
                KnowledgeHit(
                    ref=ref,
                    category=str(row["category"]),
                    relative_path=str(row["relative_path"]),
                    score=-float(row["rank"]),
                    untrusted_excerpt=excerpt,
                )
            )
            if len(hits) >= query.top_k:
                break
        return tuple(hits)

    def validate_refs(self, refs: Iterable[KnowledgeRef]) -> bool:
        refs = tuple(refs)
        if not refs:
            return True
        self.verify_integrity()
        connection = self._connect(self.path, read_only=True)
        try:
            for ref in refs:
                row = connection.execute(
                    """
                    SELECT document.drive_id, document.version_id,
                           document.content_hash, document.etag, document.display_name,
                           document.status, document.security_classification,
                           document.extractor_version, document.policy_version,
                           document.effective_from, document.effective_to,
                           document.authority, chunk.locator, chunk.chunk_hash
                    FROM chunk JOIN document ON document.item_id = chunk.item_id
                    WHERE document.item_id = ? AND chunk.locator = ?
                    """,
                    (ref.item_id, ref.chunk_locator),
                ).fetchone()
                if row is None:
                    return False
                effective_from = (
                    str(row["effective_from"]) if row["effective_from"] else None
                )
                effective_to = str(row["effective_to"]) if row["effective_to"] else None
                if (
                    str(row["drive_id"]) != ref.drive_id
                    or str(row["version_id"]) != ref.version_id
                    or str(row["content_hash"]) != ref.content_hash
                    or str(row["etag"]) != ref.etag
                    or str(row["display_name"]) != ref.display_name
                    or str(row["security_classification"])
                    != ref.security_classification
                    or str(row["extractor_version"]) != ref.extractor_version
                    or str(row["policy_version"]) != ref.policy_version
                    or effective_from != ref.effective_from
                    or effective_to != ref.effective_to
                    or str(row["authority"]) != ref.authority
                    or str(row["status"]) != ref.status
                    or str(row["locator"]) != ref.chunk_locator
                    or str(row["chunk_hash"]) != ref.chunk_hash
                    or ref.status not in {"unverified", "verified"}
                ):
                    return False
            return True
        finally:
            connection.close()

    def metadata_value(self, key: str) -> object | None:
        connection = self._connect(self.path, read_only=True)
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return json.loads(str(row["value"]))

    def document_versions(self) -> dict[str, tuple[str, str]]:
        if not self.path.exists():
            return {}
        connection = self._connect(self.path, read_only=True)
        try:
            return {
                str(row["item_id"]): (
                    str(row["version_id"]),
                    str(row["content_hash"]),
                )
                for row in connection.execute(
                    "SELECT item_id, version_id, content_hash FROM document"
                )
            }
        finally:
            connection.close()

    def document_inventory(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        connection = self._connect(self.path, read_only=True)
        try:
            return {
                str(row["item_id"]): {
                    "version_id": str(row["version_id"]),
                    "content_hash": str(row["content_hash"]),
                    "size": int(row["size"]),
                    "relative_path": str(row["relative_path"]),
                    "char_count": int(row["char_count"]),
                    "chunk_count": int(row["chunk_count"]),
                }
                for row in connection.execute(
                    "SELECT document.item_id, document.version_id, "
                    "document.content_hash, document.size, document.relative_path, "
                    "COALESCE(SUM(LENGTH(chunk.text)), 0) AS char_count, "
                    "COUNT(chunk.chunk_id) AS chunk_count "
                    "FROM document LEFT JOIN chunk ON chunk.item_id = document.item_id "
                    "GROUP BY document.item_id, document.version_id, "
                    "document.content_hash, document.size, document.relative_path"
                )
            }
        finally:
            connection.close()

    def canonical_summary(self) -> str:
        snapshot = self.status()
        return canonical_json(snapshot.binding())
