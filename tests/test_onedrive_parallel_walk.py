from __future__ import annotations

import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from notion_excel_sync.adapters.onedrive import (
    DriveEntry,
    OneDriveError,
    OneDriveReadOnlyClient,
)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers: dict[str, str] = {}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self.payload if amount < 0 else self.payload[:amount]


class _TreeClient(OneDriveReadOnlyClient):
    def __init__(
        self,
        tree: dict[str, list[tuple[str, str, bool]]],
        *,
        error_folder: str | None = None,
    ) -> None:
        super().__init__("token")
        self.tree = tree
        self.error_folder = error_folder
        self.calls: list[str] = []
        self._activity_lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def get_entry(self, drive_id: str, item_id: str) -> DriveEntry:
        if (drive_id, item_id) != ("drive", "root"):
            raise AssertionError((drive_id, item_id))
        return DriveEntry(
            drive_id="drive",
            item_id="root",
            name="Wiki",
            relative_path="",
            parent_item_id=None,
            parent_path="/drive/root:",
            is_folder=True,
            version_tag="root-v1",
            last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
            size=0,
            ctag="root-v1",
        )

    def list_children(
        self,
        drive_id: str,
        folder_item_id: str,
        *,
        parent_relative_path: str = "",
    ) -> list[DriveEntry]:
        if drive_id != "drive":
            raise AssertionError(drive_id)
        with self._activity_lock:
            self.calls.append(folder_item_id)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if folder_item_id != "root":
                time.sleep(0.04)
            if folder_item_id == self.error_folder:
                raise OneDriveError(f"worker boom: {folder_item_id}")
            entries: list[DriveEntry] = []
            for item_id, name, is_folder in self.tree.get(folder_item_id, []):
                path = "/".join(
                    part for part in (parent_relative_path.strip("/"), name) if part
                )
                entries.append(
                    DriveEntry(
                        drive_id="drive",
                        item_id=item_id,
                        name=name,
                        relative_path=path,
                        parent_item_id=folder_item_id,
                        parent_path=f"/drive/root:/Wiki/{parent_relative_path}",
                        is_folder=is_folder,
                        version_tag=f"{item_id}-v1",
                        last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
                        size=0 if is_folder else 10,
                        mime_type=None if is_folder else "text/plain",
                        etag=f"{item_id}-v1",
                    )
                )
            return entries
        finally:
            with self._activity_lock:
                self._active -= 1


class OneDriveParallelWalkTests(unittest.TestCase):
    def test_concurrency_one_and_four_return_the_same_deterministic_tree(self) -> None:
        tree = {
            "root": [("b", "B", True), ("a", "A", True), ("skip", "skip", True)],
            "a": [("a-file", "one.txt", False), ("c", "Nested", True)],
            "b": [("b-file", "two.txt", False)],
            "c": [("c-file", "three.txt", False)],
            "skip": [("skip-file", "never.txt", False)],
        }
        serial = _TreeClient(tree)
        parallel = _TreeClient(tree)

        serial_result = serial.walk_folder(
            "drive",
            "root",
            include_root_names=("A", "B"),
            max_workers=1,
        )
        parallel_result = parallel.walk_folder(
            "drive",
            "root",
            include_root_names=("A", "B"),
            max_workers=4,
        )

        self.assertEqual(serial_result, parallel_result)
        self.assertEqual(
            [entry.relative_path for entry in parallel_result],
            ["A", "A/Nested", "A/Nested/three.txt", "A/one.txt", "B", "B/two.txt"],
        )
        self.assertNotIn("skip", parallel.calls)
        self.assertEqual(serial.max_active, 1)
        self.assertGreaterEqual(parallel.max_active, 2)

    def test_parallel_max_items_overflow_fails_without_a_counter_race(self) -> None:
        client = _TreeClient(
            {
                "root": [("a", "A", True), ("b", "B", True)],
                "a": [("a-file", "one.txt", False)],
                "b": [("b-file", "two.txt", False)],
            }
        )
        with self.assertRaisesRegex(OneDriveError, "max_items"):
            client.walk_folder(
                "drive",
                "root",
                max_items=3,
                max_workers=4,
            )
        self.assertIn("a", client.calls)
        self.assertIn("b", client.calls)

    def test_duplicate_folder_from_parallel_parents_fails_before_traversal(self) -> None:
        client = _TreeClient(
            {
                "root": [("a", "A", True), ("b", "B", True)],
                "a": [("shared", "Shared", True)],
                "b": [("shared", "Shared", True)],
                "shared": [("unsafe", "unsafe.txt", False)],
            }
        )
        with self.assertRaisesRegex(OneDriveError, "loop"):
            client.walk_folder("drive", "root", max_workers=4)
        self.assertNotIn("shared", client.calls)

    def test_worker_error_is_propagated_and_no_partial_result_is_returned(self) -> None:
        client = _TreeClient(
            {
                "root": [("a", "A", True), ("b", "B", True)],
                "a": [("a-file", "one.txt", False)],
                "b": [("b-file", "two.txt", False)],
            },
            error_folder="a",
        )
        with self.assertRaisesRegex(OneDriveError, "worker boom: a"):
            client.walk_folder("drive", "root", max_workers=4)

    def test_callable_token_is_serialized_but_factory_gets_overlap(self) -> None:
        state_lock = threading.Lock()
        provider_active = 0
        provider_max = 0
        network_active = 0
        network_max = 0
        factory_calls = 0
        network_overlap = threading.Event()

        def provider() -> str:
            nonlocal provider_active, provider_max
            with state_lock:
                provider_active += 1
                provider_max = max(provider_max, provider_active)
            try:
                time.sleep(0.03)
                return "token"
            finally:
                with state_lock:
                    provider_active -= 1

        def opener_factory():
            nonlocal factory_calls
            with state_lock:
                factory_calls += 1

            def opener(request: Any, *, timeout: float) -> _Response:
                nonlocal network_active, network_max
                del request, timeout
                with state_lock:
                    network_active += 1
                    network_max = max(network_max, network_active)
                    if network_active >= 2:
                        network_overlap.set()
                try:
                    network_overlap.wait(timeout=1.0)
                    return _Response(json.dumps({"id": "drive"}).encode())
                finally:
                    with state_lock:
                        network_active -= 1

            return opener

        client = OneDriveReadOnlyClient(
            provider,
            base_url="https://graph.test/v1.0",
            opener_factory=opener_factory,
            max_retries=0,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: client.get_my_drive_id(), range(2)))

        self.assertEqual(results, ["drive", "drive"])
        self.assertEqual(provider_max, 1)
        self.assertGreaterEqual(network_max, 2)
        self.assertEqual(factory_calls, 2)


if __name__ == "__main__":
    unittest.main()
