from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Mapping

from notion_excel_sync.models import KnowledgeRef


WIKI_SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "safe-ooxml-text-v4"
CHUNKER_VERSION = "nfc-char-window-v1"
TOKENIZER_VERSION = "ko-char-ngram-v1"


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    drive_id: str
    item_id: str
    version_id: str
    etag: str
    ctag: str
    display_name: str
    relative_path: str
    content_hash: str
    last_modified_at: str
    size: int
    mime_type: str
    category: str
    authority: str
    status: str
    security_classification: str
    policy_version: str
    extractor_version: str = EXTRACTOR_VERSION
    effective_from: str | None = None
    effective_to: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    chunk_id: str
    item_id: str
    ordinal: int
    locator: str
    chunk_hash: str
    text: str = field(repr=False)
    search_terms: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    generation_digest: str
    drive_id: str
    root_item_id: str
    root_version_tag: str
    policy_version: str
    extractor_version: str
    chunker_version: str
    tokenizer_version: str
    created_at: datetime
    document_count: int
    chunk_count: int
    quarantine_count: int
    delta_link: str = field(default="", repr=False)

    def binding(self) -> dict[str, str | int]:
        return {
            "schema_version": WIKI_SCHEMA_VERSION,
            "generation_digest": self.generation_digest,
            "drive_id": self.drive_id,
            "root_item_id": self.root_item_id,
            "root_version_tag": self.root_version_tag,
            "policy_version": self.policy_version,
            "extractor_version": self.extractor_version,
            "chunker_version": self.chunker_version,
            "tokenizer_version": self.tokenizer_version,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    ref: KnowledgeRef
    category: str
    relative_path: str
    score: float
    untrusted_excerpt: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    text: str
    topics: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ("unverified", "verified")
    security_classifications: tuple[str, ...] = ("internal_reference",)
    effective_on: str | None = None
    top_k: int = 5


@dataclass(frozen=True, slots=True)
class KnowledgeBundle:
    generation_digest: str
    refs: tuple[KnowledgeRef, ...] = ()
    conflicts: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    item_id: str
    relative_path_hash: str
    reason: str
    version_id: str = ""


def utc_now() -> datetime:
    return datetime.now(UTC)
