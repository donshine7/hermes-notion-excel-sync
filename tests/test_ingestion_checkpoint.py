from __future__ import annotations

import tempfile
from pathlib import Path

from notion_excel_sync.persistence.database import StateDatabase


def test_ingestion_checkpoint_initializes_once_and_updates_independently() -> None:
    with tempfile.TemporaryDirectory() as folder:
        database = StateDatabase(Path(folder) / "state.db")
        database.initialize()

        winner = database.initialize_ingestion_checkpoint(
            "wiki-bootstrap",
            {"t0": "2026-07-23T10:00:00+09:00", "phase": "started"},
            actor="123",
        )
        repeated = database.initialize_ingestion_checkpoint(
            "wiki-bootstrap",
            {"t0": "2099-01-01T00:00:00+09:00", "phase": "started"},
            actor="123",
        )
        database.save_ingestion_checkpoint(
            "wiki-data-folder",
            {"generation": "g1"},
            actor="123",
        )

        assert winner == repeated
        assert repeated["t0"] == "2026-07-23T10:00:00+09:00"
        assert database.get_ingestion_checkpoint("wiki-data-folder") == {
            "generation": "g1"
        }
        assert database.get_ingestion_checkpoint("notion-sync") is None
