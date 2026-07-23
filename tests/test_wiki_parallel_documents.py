from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from notion_excel_sync.adapters.onedrive import DriveEntry, DriveFolderDelta
from notion_excel_sync.config import WikiConfig
from notion_excel_sync.knowledge.index import WikiIndex
from notion_excel_sync.knowledge.refresh import (
    OneDriveWikiRefreshService,
    WikiRefreshError,
    _isolated_extract,
)


def _root() -> DriveEntry:
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
        etag="root-etag",
        ctag="root-v1",
    )


def _document(item_id: str, payload: bytes) -> DriveEntry:
    return DriveEntry(
        drive_id="drive",
        item_id=item_id,
        name=f"{item_id}.txt",
        relative_path=f"Rules/{item_id}.txt",
        parent_item_id="rules-folder",
        parent_path="/drive/root:/Wiki/Rules",
        is_folder=False,
        version_tag=f"{item_id}-v1",
        last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
        size=len(payload),
        mime_type="text/plain",
        etag=f"{item_id}-v1",
        ctag=f"{item_id}-c1",
    )


class _DocumentDrive:
    def __init__(
        self,
        payloads: dict[str, bytes],
        *,
        download_delay: float = 0.0,
        drift_item: str | None = None,
    ) -> None:
        self.root = _root()
        self.payloads = payloads
        self.entries = {
            item_id: _document(item_id, payload)
            for item_id, payload in payloads.items()
        }
        self.download_delay = download_delay
        self.drift_item = drift_item
        self._activity_lock = threading.Lock()
        self.active_downloads = 0
        self.max_active_downloads = 0

    def get_folder_delta_checkpoint(self, *_args, **_kwargs):
        return DriveFolderDelta(self.root, (), (), "checkpoint-v1")

    def walk_folder(self, *_args, **kwargs):
        if kwargs.get("max_workers") != 4:
            raise AssertionError(kwargs)
        return list(reversed(self.entries.values()))

    def read_folder_delta(self, *_args, **kwargs):
        if kwargs.get("delta_link") != "checkpoint-v1":
            raise AssertionError(kwargs)
        return DriveFolderDelta(self.root, (), (), "delta-v1")

    def download_current(self, drive_id: str, item_id: str) -> bytes:
        if drive_id != "drive" or item_id not in self.payloads:
            raise AssertionError((drive_id, item_id))
        with self._activity_lock:
            self.active_downloads += 1
            self.max_active_downloads = max(
                self.max_active_downloads,
                self.active_downloads,
            )
        try:
            if self.download_delay:
                time.sleep(self.download_delay)
            return self.payloads[item_id]
        finally:
            with self._activity_lock:
                self.active_downloads -= 1

    def get_entry(self, drive_id: str, item_id: str) -> DriveEntry:
        if drive_id != "drive":
            raise AssertionError(drive_id)
        if item_id == "root":
            return self.root
        entry = self.entries[item_id]
        if item_id == self.drift_item:
            return replace(
                entry,
                version_tag=f"{item_id}-v2",
                etag=f"{item_id}-v2",
            )
        return entry


def _config(path: Path, **overrides: object) -> WikiConfig:
    values: dict[str, object] = {
        "enabled": True,
        "drive_id": "drive",
        "root_item_id": "root",
        "root_path": "",
        "index_db": path,
        "include_roots": ("Rules",),
        "exclude_roots": (),
        "selective_roots": (),
        "allowed_extensions": (".txt",),
        "max_documents": 100,
        "max_total_bytes": 10_000,
        "max_generation_chars": 10_000,
        "max_generation_chunks": 1_000,
        "chunk_chars": 100,
        "chunk_overlap_chars": 0,
    }
    values.update(overrides)
    return WikiConfig(**values)  # type: ignore[arg-type]


class WikiParallelDocumentTests(unittest.TestCase):
    def _run(
        self,
        folder: str,
        drive: _DocumentDrive,
        *,
        workers: int,
        extractor,
        config_overrides: dict[str, object] | None = None,
    ):
        path = Path(folder) / "wiki.db"
        index = WikiIndex(path)
        config = _config(path, **(config_overrides or {}))
        service = OneDriveWikiRefreshService(
            config=config,
            onedrive=drive,  # type: ignore[arg-type]
            index=index,
            default_drive_id="drive",
            work_dir=Path(folder) / "work",
            document_max_workers=workers,
        )
        with patch(
            "notion_excel_sync.knowledge.refresh._isolated_extract",
            side_effect=extractor,
        ):
            report = service.refresh(force_full=True)
        return report, index.document_inventory()

    def test_workers_one_and_four_are_identical_while_four_overlap(self) -> None:
        payloads = {f"{number:02d}": f"source-{number}".encode() for number in range(6)}

        def extractor(data, _extension, **_kwargs):
            value = data.decode()
            return f"knowledge {value}", (f"warning:{value[-1]}",)

        serial_drive = _DocumentDrive(payloads, download_delay=0.03)
        parallel_drive = _DocumentDrive(payloads, download_delay=0.03)
        with tempfile.TemporaryDirectory() as serial_folder, tempfile.TemporaryDirectory() as parallel_folder:
            serial_report, serial_inventory = self._run(
                serial_folder,
                serial_drive,
                workers=1,
                extractor=extractor,
            )
            parallel_report, parallel_inventory = self._run(
                parallel_folder,
                parallel_drive,
                workers=4,
                extractor=extractor,
            )

        self.assertEqual(serial_inventory, parallel_inventory)
        self.assertEqual(
            serial_report.snapshot.generation_digest,
            parallel_report.snapshot.generation_digest,
        )
        self.assertEqual(serial_report.skipped_reasons, parallel_report.skipped_reasons)
        self.assertEqual(
            serial_report.extraction_warnings,
            parallel_report.extraction_warnings,
        )
        self.assertEqual(serial_drive.max_active_downloads, 1)
        self.assertGreaterEqual(parallel_drive.max_active_downloads, 2)

    def test_document_extraction_failures_quarantine_only_that_document(self) -> None:
        document_codes = (
            "extractor_timeout",
            "extractor_rejected_document",
            "extractor_output_limit",
        )
        for code in document_codes:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                payloads = {
                    item_id: item_id.encode() for item_id in ("01", "02", "03")
                }
                drive = _DocumentDrive(payloads, download_delay=0.01)

                def extractor(data, _extension, **_kwargs):
                    value = data.decode()
                    if value == "02":
                        raise WikiRefreshError(code)
                    return f"accepted {value}", ()

                report, inventory = self._run(
                    folder,
                    drive,
                    workers=4,
                    extractor=extractor,
                )

                self.assertEqual(set(inventory), {"01", "03"})
                self.assertEqual(report.indexed_documents, 2)
                self.assertEqual(report.quarantined, 1)
                self.assertEqual(report.skipped_reasons, {code: 1})

    def test_runtime_failure_keeps_existing_generation_and_never_applies(self) -> None:
        payloads = {item_id: item_id.encode() for item_id in ("01", "02", "03")}
        drive = _DocumentDrive(payloads, download_delay=0.01)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wiki.db"
            index = WikiIndex(path)
            service = OneDriveWikiRefreshService(
                config=_config(path),
                onedrive=drive,  # type: ignore[arg-type]
                index=index,
                default_drive_id="drive",
                document_max_workers=4,
            )
            with patch(
                "notion_excel_sync.knowledge.refresh._isolated_extract",
                return_value=("accepted", ()),
            ):
                service.refresh(force_full=True)
            before_snapshot = index.verify_integrity()
            before_inventory = index.document_inventory()

            def runtime_failure(*_args, **_kwargs):
                raise WikiRefreshError("extractor_runtime_unavailable")

            with (
                patch.object(
                    index,
                    "apply_generation",
                    wraps=index.apply_generation,
                ) as apply_generation,
                patch(
                    "notion_excel_sync.knowledge.refresh._isolated_extract",
                    side_effect=runtime_failure,
                ),
            ):
                with self.assertRaisesRegex(
                    WikiRefreshError,
                    "extractor_runtime_unavailable",
                ):
                    service.refresh(force_full=True)
                apply_generation.assert_not_called()

            after_snapshot = index.verify_integrity()
            self.assertEqual(
                after_snapshot.generation_digest,
                before_snapshot.generation_digest,
            )
            self.assertEqual(index.document_inventory(), before_inventory)

    def test_worker_exit_one_is_fatal_and_keeps_existing_generation(self) -> None:
        payloads = {item_id: item_id.encode() for item_id in ("01", "02")}
        drive = _DocumentDrive(payloads)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wiki.db"
            index = WikiIndex(path)
            config = _config(path)
            service = OneDriveWikiRefreshService(
                config=config,
                onedrive=drive,  # type: ignore[arg-type]
                index=index,
                default_drive_id="drive",
            )
            with patch(
                "notion_excel_sync.knowledge.refresh._isolated_extract",
                return_value=("accepted", ()),
            ):
                service.refresh(force_full=True)
            before = index.verify_integrity()
            before_inventory = index.document_inventory()

            failed_process = subprocess.CompletedProcess(
                args=["extractor-worker"],
                returncode=1,
                stdout=b"",
                stderr=b"worker crashed",
            )
            with (
                patch.object(
                    index,
                    "apply_generation",
                    wraps=index.apply_generation,
                ) as apply_generation,
                patch(
                    "notion_excel_sync.knowledge.refresh._run_extractor_process",
                    return_value=failed_process,
                ),
            ):
                with self.assertRaisesRegex(
                    WikiRefreshError,
                    "extractor_worker_failed",
                ):
                    service.refresh(force_full=True)
                apply_generation.assert_not_called()

            self.assertEqual(
                index.verify_integrity().generation_digest,
                before.generation_digest,
            )
            self.assertEqual(index.document_inventory(), before_inventory)

    def test_actual_parser_rejection_is_document_scoped(self) -> None:
        drive = _DocumentDrive(
            {
                "01": "safe first document".encode(),
                "02": b"unsafe\x00text",
                "03": "safe third document".encode(),
            }
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wiki.db"
            index = WikiIndex(path)
            service = OneDriveWikiRefreshService(
                config=_config(path),
                onedrive=drive,  # type: ignore[arg-type]
                index=index,
                default_drive_id="drive",
                work_dir=Path(folder) / "work",
            )
            report = service.refresh(force_full=True)

            self.assertEqual(set(index.document_inventory()), {"01", "03"})
            self.assertEqual(
                report.skipped_reasons,
                {"extractor_rejected_document": 1},
            )

    def test_document_rejection_exit_requires_valid_structured_output(self) -> None:
        malformed_outputs = (b"", b"not-json", b'{"schema_version":1}')
        for stdout in malformed_outputs:
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as folder:
                config = _config(Path(folder) / "wiki.db")
                completed = subprocess.CompletedProcess(
                    args=["extractor-worker"],
                    returncode=20,
                    stdout=stdout,
                    stderr=b"",
                )
                with patch(
                    "notion_excel_sync.knowledge.refresh._run_extractor_process",
                    return_value=completed,
                ):
                    with self.assertRaisesRegex(
                        WikiRefreshError,
                        "extractor_worker_failed",
                    ):
                        _isolated_extract(
                            b"document",
                            ".txt",
                            work_dir=Path(folder) / "work",
                            config=config,
                        )

    def test_unknown_extractor_error_is_fatal(self) -> None:
        drive = _DocumentDrive({"01": b"one"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wiki.db"
            service = OneDriveWikiRefreshService(
                config=_config(path),
                onedrive=drive,  # type: ignore[arg-type]
                index=WikiIndex(path),
                default_drive_id="drive",
            )

            def unknown_failure(*_args, **_kwargs):
                raise WikiRefreshError("extractor_unknown_failure")

            with patch(
                "notion_excel_sync.knowledge.refresh._isolated_extract",
                side_effect=unknown_failure,
            ):
                with self.assertRaisesRegex(
                    WikiRefreshError,
                    "extractor_unknown_failure",
                ):
                    service.refresh(force_full=True)
            self.assertFalse(path.exists())

    def test_version_drift_is_fatal_and_publishes_no_generation(self) -> None:
        payloads = {item_id: item_id.encode() for item_id in ("01", "02", "03")}
        drive = _DocumentDrive(payloads, download_delay=0.01, drift_item="02")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wiki.db"
            config = _config(path)
            service = OneDriveWikiRefreshService(
                config=config,
                onedrive=drive,  # type: ignore[arg-type]
                index=WikiIndex(path),
                default_drive_id="drive",
                document_max_workers=4,
            )
            with patch(
                "notion_excel_sync.knowledge.refresh._isolated_extract",
                return_value=("accepted", ()),
            ):
                with self.assertRaisesRegex(WikiRefreshError, "binding changed"):
                    service.refresh(force_full=True)
            self.assertFalse(path.exists())

    def test_capacity_is_decided_in_selected_order_not_completion_order(self) -> None:
        payloads = {"01": b"first", "02": b"second"}
        drive = _DocumentDrive(payloads)

        def extractor(data, _extension, **_kwargs):
            if data == b"first":
                time.sleep(0.06)
                return "AAAAA", ()
            time.sleep(0.005)
            return "BBBBB", ()

        with tempfile.TemporaryDirectory() as folder:
            report, inventory = self._run(
                folder,
                drive,
                workers=4,
                extractor=extractor,
                config_overrides={"max_generation_chars": 5},
            )

        self.assertEqual(set(inventory), {"01"})
        self.assertEqual(
            report.skipped_reasons,
            {"generation_text_capacity_limit": 1},
        )

    def test_at_most_four_document_tasks_are_in_flight(self) -> None:
        payloads = {f"{number:02d}": b"x" for number in range(12)}
        drive = _DocumentDrive(payloads, download_delay=0.03)
        with tempfile.TemporaryDirectory() as folder:
            report, inventory = self._run(
                folder,
                drive,
                workers=4,
                extractor=lambda data, _extension, **_kwargs: (data.decode(), ()),
            )

        self.assertEqual(report.indexed_documents, 12)
        self.assertEqual(len(inventory), 12)
        self.assertGreaterEqual(drive.max_active_downloads, 2)
        self.assertLessEqual(drive.max_active_downloads, 4)


if __name__ == "__main__":
    unittest.main()
