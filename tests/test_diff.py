from __future__ import annotations

import unittest
from datetime import UTC, datetime

from notion_excel_sync.models import ChangeKind, SourceRecord, WorkbookSnapshot
from notion_excel_sync.workflow.diff import diff_snapshots


def snapshot(version: str, records: list[SourceRecord]) -> WorkbookSnapshot:
    return WorkbookSnapshot("d", "i", version, version, datetime.now(UTC), records)


class SnapshotDiffTest(unittest.TestCase):
    def test_create_update_delete_and_reorder(self) -> None:
        before = snapshot(
            "1",
            [
                SourceRecord("A", "2026", 2, {"상태": "접수"}),
                SourceRecord("B", "2026", 3, {"상태": "착수"}),
                SourceRecord("D", "2026", 4, {"상태": "완료"}),
            ],
        )
        after = snapshot(
            "2",
            [
                SourceRecord("B", "2026", 20, {"상태": "출원"}),
                SourceRecord("A", "2026", 21, {"상태": "접수"}),
                SourceRecord("C", "2026", 22, {"상태": "신규"}),
            ],
        )
        changes = diff_snapshots(before, after)
        self.assertEqual(
            [(item.key, item.kind) for item in changes],
            [
                ("B", ChangeKind.UPDATE),
                ("C", ChangeKind.CREATE),
                ("D", ChangeKind.DELETE_CANDIDATE),
            ],
        )


if __name__ == "__main__":
    unittest.main()

