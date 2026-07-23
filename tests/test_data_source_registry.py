from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from notion_excel_sync.config import (
    AppConfig,
    ApprovalConfig,
    ExcelConfig,
    NotionConfig,
    OneDriveConfig,
    WikiConfig,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.workflow.data_sources import (
    DataSourceBindingError,
    configured_data_sources,
)


class DataSourceRegistryTest(unittest.TestCase):
    def _config(self, databases: dict[str, str]) -> AppConfig:
        return AppConfig(
            onedrive=OneDriveConfig("drive", "item"),
            excel=ExcelConfig(["2026"]),
            notion=NotionConfig(databases=databases),
            approval=ApprovalConfig(allowed_telegram_users={"1"}),
            wiki=WikiConfig(),
            state_db=Path("state.db"),
            work_dir=Path("work"),
            initial_cutoff=datetime(2026, 7, 16, tzinfo=UTC),
        )

    def test_placeholder_is_overlaid_by_verified_installation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = StateDatabase(Path(folder) / "state.db")
            database.initialize()
            with database.session() as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    """
                    INSERT INTO notion_schema_installation(
                        logical_database_name, database_id, data_source_id,
                        parent_page_id, schema_digest, schema_proposal_id,
                        revision, installed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "정부지원사업 증빙",
                        "1" * 32,
                        "2" * 32,
                        "3" * 32,
                        "4" * 64,
                        "SP-test",
                        1,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            result = configured_data_sources(
                self._config(
                    {"정부지원사업 증빙": "REPLACE_WITH_DATABASE_OR_DATA_SOURCE_ID"}
                ),
                database,
            )
            self.assertEqual(result["정부지원사업 증빙"], "2" * 32)

    def test_static_and_verified_binding_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = StateDatabase(Path(folder) / "state.db")
            database.initialize()
            with database.session() as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    """
                    INSERT INTO notion_schema_installation(
                        logical_database_name, database_id, data_source_id,
                        parent_page_id, schema_digest, schema_proposal_id,
                        revision, installed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "정부지원사업 증빙",
                        "1" * 32,
                        "2" * 32,
                        "3" * 32,
                        "4" * 64,
                        "SP-test",
                        1,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            with self.assertRaises(DataSourceBindingError):
                configured_data_sources(
                    self._config({"정부지원사업 증빙": "5" * 32}),
                    database,
                )

    def test_legacy_state_can_be_inspected_without_running_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = StateDatabase(Path(folder) / "state.db")
            with database.session() as connection:
                connection.execute("CREATE TABLE legacy_checkpoint(id TEXT PRIMARY KEY)")

            self.assertFalse(database.schema_installation_registry_exists())
            self.assertEqual(
                configured_data_sources(
                    self._config(
                        {
                            "정부지원사업 증빙": (
                                "REPLACE_WITH_DATABASE_OR_DATA_SOURCE_ID"
                            )
                        }
                    )
                ),
                {},
            )

            # The read-only check must not create or migrate any table.
            with database.session() as connection:
                names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            self.assertEqual(names, {"legacy_checkpoint"})


if __name__ == "__main__":
    unittest.main()
