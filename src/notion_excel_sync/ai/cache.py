from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from notion_excel_sync.ai.models import AIAnalysis, analysis_from_cache


class AIAnalysisCache:
    """Local output-only cache keyed by exact input and prompt digests."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("AI cache path must be absolute")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    request_digest TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    output_digest TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_cache_source
                ON analysis_cache(task_type, source_hash, prompt_version);
                """
            )

    def get(self, request_digest: str) -> AIAnalysis | None:
        if not self.path.is_file():
            return None
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT output_digest, output_json FROM analysis_cache "
                "WHERE request_digest = ?",
                (request_digest,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[1]))
        if not isinstance(payload, dict):
            raise ValueError("Stored AI cache payload is invalid")
        analysis = analysis_from_cache(payload)
        if analysis.request_digest != request_digest:
            raise ValueError("Stored AI cache binding is invalid")
        if analysis.output_digest != str(row[0]):
            raise ValueError("Stored AI cache output digest is invalid")
        return analysis

    def put(self, analysis: AIAnalysis) -> None:
        self.initialize()
        payload = analysis.public_payload(include_model=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO analysis_cache(
                    request_digest, task_type, source_hash, prompt_version,
                    provider, model, output_digest, output_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis.request_digest,
                    analysis.task_type,
                    analysis.source_hash,
                    analysis.prompt_version,
                    analysis.provider,
                    analysis.model,
                    analysis.output_digest,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            return sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        return sqlite3.connect(self.path)
