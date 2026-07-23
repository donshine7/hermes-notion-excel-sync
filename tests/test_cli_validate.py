from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from notion_excel_sync.adapters import BaselineVersionNotFound, DriveVersion
from notion_excel_sync.cli import _initial_baseline_status


class _BaselineClient:
    def __init__(self, result: DriveVersion | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str, datetime]] = []

    def select_initial_baseline(
        self,
        drive_id: str,
        item_id: str,
        cutoff: datetime,
    ) -> DriveVersion:
        self.calls.append((drive_id, item_id, cutoff))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ValidateBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cutoff = datetime(
            2026,
            7,
            16,
            tzinfo=timezone(timedelta(hours=9)),
        )
        self.config = SimpleNamespace(
            onedrive=SimpleNamespace(drive_id="drive", item_id="item"),
            initial_cutoff=self.cutoff,
        )

    def test_reports_selected_initial_baseline(self) -> None:
        baseline = DriveVersion(
            id="238.0",
            last_modified_at=datetime(2026, 7, 15, 12, tzinfo=UTC),
        )
        client = _BaselineClient(baseline)

        payload, error = _initial_baseline_status(client, self.config)

        self.assertIsNone(error)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version_id"], "238.0")
        self.assertEqual(payload["cutoff"], self.cutoff.isoformat())
        self.assertEqual(
            client.calls,
            [("drive", "item", self.cutoff)],
        )

    def test_reports_missing_initial_baseline_as_validation_error(self) -> None:
        client = _BaselineClient(
            BaselineVersionNotFound("No retained version exists before cutoff")
        )

        payload, error = _initial_baseline_status(client, self.config)

        self.assertEqual(
            payload,
            {"status": "missing", "cutoff": self.cutoff.isoformat()},
        )
        self.assertIn("Initial OneDrive baseline is unavailable", error or "")


if __name__ == "__main__":
    unittest.main()
