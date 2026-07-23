from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from notion_excel_sync.adapters.onedrive import DriveEntry, DriveFolderDelta
from notion_excel_sync.config import WikiConfig
from notion_excel_sync.knowledge.index import WikiIndex
from notion_excel_sync.knowledge.refresh import (
    OneDriveWikiRefreshService,
    WikiRefreshError,
)


def _entry(
    item_id: str,
    relative_path: str,
    *,
    folder: bool = False,
    version: str = "v1",
) -> DriveEntry:
    return DriveEntry(
        drive_id="drive",
        item_id=item_id,
        name=relative_path.rsplit("/", 1)[-1] or "wiki-root",
        relative_path=relative_path,
        parent_item_id="root" if item_id != "root" else None,
        parent_path="/drive/root:/Wiki",
        is_folder=folder,
        version_tag=version,
        last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
        size=0 if folder else 10,
        mime_type=None if folder else "text/plain",
        etag=version,
        ctag=f"c-{version}",
    )


def _service(onedrive: object, folder: str) -> OneDriveWikiRefreshService:
    index = WikiIndex(Path(folder) / "wiki.db")
    config = WikiConfig(
        enabled=True,
        drive_id="drive",
        root_item_id="root",
        root_path="",
        index_db=index.path,
        include_roots=("Rules",),
    )
    return OneDriveWikiRefreshService(
        config=config,
        onedrive=onedrive,  # type: ignore[arg-type]
        index=index,
        default_drive_id="drive",
    )


class WikiFastFullScanTests(unittest.TestCase):
    def test_full_walk_merges_catch_up_moves_both_directions_and_deletions(self) -> None:
        root_v1 = _entry("root", "", folder=True, version="root-v1")
        root_v2 = _entry("root", "", folder=True, version="root-v2")
        folder = _entry("rules", "Rules", folder=True)
        stable = _entry("stable", "Rules/stable.txt")
        changed_v1 = _entry("changed", "Rules/changed.txt", version="v1")
        changed_v2 = _entry("changed", "Excluded/changed.txt", version="v2")
        removed = _entry("removed", "Rules/removed.txt")
        added = _entry("added", "Rules/added.txt", version="v1")
        calls: list[tuple[str, str | None]] = []

        class Drive:
            def get_folder_delta_checkpoint(self, drive_id, root_item_id):
                self._check_scope(drive_id, root_item_id)
                calls.append(("checkpoint", None))
                return DriveFolderDelta(root_v1, (), (), "checkpoint-1")

            def walk_folder(self, drive_id, root_item_id, **kwargs):
                self._check_scope(drive_id, root_item_id)
                self.assert_limit(kwargs)
                if kwargs.get("include_root_names") != ("Rules",):
                    raise AssertionError(kwargs)
                calls.append(("walk", None))
                return [folder, stable, changed_v1, removed]

            def read_folder_delta(self, drive_id, root_item_id, **kwargs):
                self._check_scope(drive_id, root_item_id)
                calls.append(("catch-up", kwargs.get("delta_link")))
                return DriveFolderDelta(
                    root_v2,
                    (changed_v2, added),
                    ("removed",),
                    "checkpoint-2",
                )

            def get_entry(self, drive_id, root_item_id):
                self._check_scope(drive_id, root_item_id)
                calls.append(("root", None))
                return root_v2

            @staticmethod
            def _check_scope(drive_id, root_item_id):
                if (drive_id, root_item_id) != ("drive", "root"):
                    raise AssertionError((drive_id, root_item_id))

            @staticmethod
            def assert_limit(kwargs):
                if (
                    kwargs.get("max_items") != 50_000
                    or kwargs.get("max_workers") != 4
                ):
                    raise AssertionError(kwargs)

        with tempfile.TemporaryDirectory() as temp:
            result = _service(Drive(), temp)._read_consistent_full_catalog(
                "drive",
                "root",
                progress=None,
            )

        self.assertEqual(
            {entry.item_id: entry.version_tag for entry in result.entries},
            {"rules": "v1", "stable": "v1", "added": "v1"},
        )
        self.assertEqual(result.deleted_item_ids, ("changed", "removed"))
        self.assertEqual(result.delta_link, "checkpoint-2")
        self.assertEqual(
            calls,
            [
                ("checkpoint", None),
                ("walk", None),
                ("catch-up", "checkpoint-1"),
                ("root", None),
            ],
        )

    def test_scanned_folder_deletion_retries_with_a_fresh_checkpoint(self) -> None:
        root = _entry("root", "", folder=True, version="root-v1")
        folder = _entry("rules", "Rules", folder=True)
        stable = _entry("stable", "Rules/stable.txt")
        progress: list[dict[str, object]] = []

        class Drive:
            attempts = 0

            def get_folder_delta_checkpoint(self, *_args):
                self.attempts += 1
                return DriveFolderDelta(root, (), (), f"checkpoint-{self.attempts}")

            def walk_folder(self, *_args, **_kwargs):
                return [folder, stable] if self.attempts == 1 else [stable]

            def read_folder_delta(self, *_args, **kwargs):
                if kwargs.get("delta_link") != f"checkpoint-{self.attempts}":
                    raise AssertionError(kwargs)
                deleted = ("rules",) if self.attempts == 1 else ()
                return DriveFolderDelta(
                    root,
                    (),
                    deleted,
                    f"caught-up-{self.attempts}",
                )

            def get_entry(self, *_args):
                return root

        drive = Drive()
        with tempfile.TemporaryDirectory() as temp:
            result = _service(drive, temp)._read_consistent_full_catalog(
                "drive",
                "root",
                progress=progress.append,
            )

        self.assertEqual(drive.attempts, 2)
        self.assertEqual([entry.item_id for entry in result.entries], ["stable"])
        self.assertEqual(progress[0]["stage"], "catalog_retry")
        self.assertEqual(progress[0]["reason"], "folder_changed")

    def test_excluded_folder_event_and_unknown_deletion_are_ignored(self) -> None:
        root = _entry("root", "", folder=True, version="root-v1")
        included_folder = _entry("rules", "Rules", folder=True)
        stable = _entry("stable", "Rules/stable.txt")
        excluded_folder = _entry("archive", "Archive", folder=True, version="v2")

        class Drive:
            attempts = 0

            def get_folder_delta_checkpoint(self, *_args):
                self.attempts += 1
                return DriveFolderDelta(root, (), (), "checkpoint")

            def walk_folder(self, *_args, **_kwargs):
                return [included_folder, stable]

            def read_folder_delta(self, *_args, **_kwargs):
                return DriveFolderDelta(
                    root,
                    (excluded_folder,),
                    ("unknown-excluded-item",),
                    "caught-up",
                )

            def get_entry(self, *_args):
                return root

        drive = Drive()
        with tempfile.TemporaryDirectory() as temp:
            result = _service(drive, temp)._read_consistent_full_catalog(
                "drive",
                "root",
                progress=None,
            )

        self.assertEqual(drive.attempts, 1)
        self.assertEqual(
            [entry.item_id for entry in result.entries],
            ["rules", "stable"],
        )
        self.assertEqual(result.deleted_item_ids, ())

    def test_walk_result_outside_include_roots_fails_closed(self) -> None:
        root = _entry("root", "", folder=True, version="root-v1")
        outside = _entry("outside", "Archive/outside.txt")

        class Drive:
            catch_up_called = False

            def get_folder_delta_checkpoint(self, *_args):
                return DriveFolderDelta(root, (), (), "checkpoint")

            def walk_folder(self, *_args, **_kwargs):
                return [outside]

            def read_folder_delta(self, *_args, **_kwargs):
                self.catch_up_called = True
                raise AssertionError("unsafe walk result must fail before catch-up")

        drive = Drive()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(WikiRefreshError, "outside include roots"):
                _service(drive, temp)._read_consistent_full_catalog(
                    "drive",
                    "root",
                    progress=None,
                )
        self.assertFalse(drive.catch_up_called)

    def test_repeated_folder_changes_fail_closed_after_bounded_retries(self) -> None:
        root = _entry("root", "", folder=True, version="root-v1")
        changed_folder = _entry("rules", "Rules", folder=True, version="v2")

        class Drive:
            attempts = 0

            def get_folder_delta_checkpoint(self, *_args):
                self.attempts += 1
                return DriveFolderDelta(root, (), (), f"checkpoint-{self.attempts}")

            def walk_folder(self, *_args, **_kwargs):
                return []

            def read_folder_delta(self, *_args, **_kwargs):
                return DriveFolderDelta(
                    root,
                    (changed_folder,),
                    (),
                    f"caught-up-{self.attempts}",
                )

        drive = Drive()
        with tempfile.TemporaryDirectory() as temp:
            service = _service(drive, temp)
            with self.assertRaisesRegex(WikiRefreshError, "changed repeatedly"):
                service._read_consistent_full_catalog(
                    "drive",
                    "root",
                    progress=None,
                )
        self.assertEqual(drive.attempts, 3)

    def test_final_root_change_restarts_catalog_before_returning(self) -> None:
        root_v1 = _entry("root", "", folder=True, version="root-v1")
        root_v2 = _entry("root", "", folder=True, version="root-v2")
        stable = _entry("stable", "Rules/stable.txt")
        progress: list[dict[str, object]] = []

        class Drive:
            attempts = 0

            def get_folder_delta_checkpoint(self, *_args):
                self.attempts += 1
                return DriveFolderDelta(
                    root_v1,
                    (),
                    (),
                    f"checkpoint-{self.attempts}",
                )

            def walk_folder(self, *_args, **_kwargs):
                return [stable]

            def read_folder_delta(self, *_args, **_kwargs):
                catch_up_root = root_v1 if self.attempts == 1 else root_v2
                return DriveFolderDelta(
                    catch_up_root,
                    (),
                    (),
                    f"caught-up-{self.attempts}",
                )

            def get_entry(self, *_args):
                return root_v2

        drive = Drive()
        with tempfile.TemporaryDirectory() as temp:
            result = _service(drive, temp)._read_consistent_full_catalog(
                "drive",
                "root",
                progress=progress.append,
            )

        self.assertEqual(drive.attempts, 2)
        self.assertEqual(result.root.version_tag, "root-v2")
        self.assertEqual(progress[0]["reason"], "root_changed")


if __name__ == "__main__":
    unittest.main()
