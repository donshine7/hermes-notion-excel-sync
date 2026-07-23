from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from notion_excel_sync.adapters.local_workbook import (
    LocalWorkbookError,
    LocalWorkbookReadOnlyClient,
)


class LocalWorkbookReadOnlyClientTest(unittest.TestCase):
    def make_client(
        self, root: Path, content: bytes = b"first"
    ) -> tuple[Path, LocalWorkbookReadOnlyClient]:
        source_dir = root / "source"
        source_dir.mkdir()
        source = source_dir / "workbook.xlsx"
        source.write_bytes(content)
        client = LocalWorkbookReadOnlyClient(
            source,
            root / "runtime" / "snapshots",
            drive_id="local",
            item_id="local:item",
        )
        return source, client

    def test_capture_never_changes_source_and_uses_content_digest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source, client = self.make_client(Path(folder))
            before = source.stat()

            capture = client.capture()

            after = source.stat()
            digest = hashlib.sha256(b"first").hexdigest()
            self.assertEqual(capture.version_id, f"sha256:{digest}")
            self.assertEqual(capture.content, b"first")
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(before.st_size, after.st_size)
            self.assertEqual(source.read_bytes(), b"first")

    def test_history_is_local_and_initial_baseline_is_command_time_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source, client = self.make_client(Path(folder))
            first = client.capture()
            source.write_bytes(b"second")
            second = client.capture()

            versions = client.list_versions("local", "local:item")
            self.assertEqual({item.id for item in versions}, {first.version_id, second.version_id})
            self.assertEqual(
                client.download_version("local", "local:item", first.version_id),
                b"first",
            )
            self.assertEqual(
                client.select_initial_baseline(
                    "local",
                    "local:item",
                    datetime.now(UTC),
                ).id,
                second.version_id,
            )

    def test_scope_and_cached_snapshot_integrity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, client = self.make_client(root)
            capture = client.capture()
            with self.assertRaises(LocalWorkbookError):
                client.download_current("other", "local:item")

            snapshot = (
                root
                / "runtime"
                / "snapshots"
                / f"{capture.file_hash}.snapshot"
            )
            snapshot.write_bytes(b"tampered")
            with self.assertRaises(LocalWorkbookError):
                client.download_version("local", "local:item", capture.version_id)


if __name__ == "__main__":
    unittest.main()
