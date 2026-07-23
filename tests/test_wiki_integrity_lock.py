from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from notion_excel_sync.adapters.onedrive import DriveEntry, DriveFolderDelta
from notion_excel_sync.config import WikiConfig
from notion_excel_sync.knowledge.index import (
    WikiGenerationLockTimeout,
    WikiIndex,
    WikiIndexError,
    chunk_text,
)
from notion_excel_sync.knowledge.models import KnowledgeDocument, KnowledgeQuery
from notion_excel_sync.knowledge.policy import policy_fingerprint
from notion_excel_sync.knowledge.refresh import (
    OneDriveWikiRefreshService,
    WikiRefreshError,
)
from notion_excel_sync.models import KnowledgeRef
from notion_excel_sync.security.hermes_gateway import _wiki_live_binding_guard


def _hold_generation_lock(
    index_path: str,
    ready_path: str,
    release_path: str,
) -> None:
    """Child-process target used to prove the lock is not process-local."""

    with WikiIndex(index_path).generation_lock(timeout_seconds=5.0):
        Path(ready_path).touch()
        deadline = time.monotonic() + 10.0
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.02)


def _document(item_id: str, text: str):
    return (
        KnowledgeDocument(
            drive_id="drive",
            item_id=item_id,
            version_id="v1",
            etag="etag-v1",
            ctag="ctag-v1",
            display_name=f"{item_id}.txt",
            relative_path=f"9. 법령/{item_id}.txt",
            content_hash=f"content-{item_id}",
            last_modified_at="2026-07-21T00:00:00+00:00",
            size=len(text.encode("utf-8")),
            mime_type="text/plain",
            category="법령·제도",
            authority="official_candidate",
            status="unverified",
            security_classification="internal_reference",
            policy_version="policy-v1",
        ),
        chunk_text(item_id, text, chunk_chars=80, overlap_chars=10),
    )


def _build_index(path: Path) -> WikiIndex:
    index = WikiIndex(path)
    index.apply_generation(
        drive_id="drive",
        root_item_id="root",
        root_version_tag="root-v1",
        policy_version="policy-v1",
        delta_link="https://graph.microsoft.com/v1.0/drives/drive/items/root/delta?t=1",
        upserts=[_document("rule", "정부지원사업 증빙은 적용 연도 지침을 확인한다.")],
        full_rebuild=True,
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    return index


class WikiIntegrityAndLockTests(unittest.TestCase):
    def test_live_binding_validates_index_refs_and_remote_content(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wiki.db"
            config = WikiConfig(
                enabled=True,
                drive_id="drive",
                root_item_id="root",
                root_path="Desktop/[상상] 업무분류",
                index_db=path,
            )
            data = "정부지원사업 증빙 지침".encode("utf-8")
            document, chunks = _document("rule", data.decode("utf-8"))
            document = replace(
                document,
                content_hash=hashlib.sha256(data).hexdigest(),
                size=len(data),
                policy_version=policy_fingerprint(config),
            )
            index = WikiIndex(path)
            index.apply_generation(
                drive_id="drive",
                root_item_id="root",
                root_version_tag="root-v1",
                policy_version=policy_fingerprint(config),
                delta_link="delta",
                upserts=[(document, chunks)],
                full_rebuild=True,
            )
            ref = index.query(KnowledgeQuery("증빙 지침", top_k=1))[0].ref

            root_entry = DriveEntry(
                drive_id="drive",
                item_id="root",
                name="[상상] 업무분류",
                relative_path="Desktop/[상상] 업무분류",
                parent_item_id="desktop",
                parent_path="/drives/drive/root:/Desktop",
                is_folder=True,
                version_tag="root-v1",
                last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
            )
            file_entry = DriveEntry(
                drive_id="drive",
                item_id="rule",
                name="rule.txt",
                relative_path="9. 법령/rule.txt",
                parent_item_id="laws",
                parent_path="/drives/drive/root:/Desktop/[상상] 업무분류/9. 법령",
                is_folder=False,
                version_tag="v1",
                last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
                size=len(data),
                etag="etag-v1",
            )

            class BindingDrive:
                def get_item_by_path(self, drive_id: str, root_path: str) -> DriveEntry:
                    self.assert_scope = (drive_id, root_path)
                    return root_entry

                def get_entry(self, drive_id: str, item_id: str) -> DriveEntry:
                    if drive_id != "drive":
                        raise AssertionError(drive_id)
                    return root_entry if item_id == "root" else file_entry

                def download_current(self, drive_id: str, item_id: str) -> bytes:
                    if (drive_id, item_id) != ("drive", "rule"):
                        raise AssertionError((drive_id, item_id))
                    return data

            drive = BindingDrive()
            service = OneDriveWikiRefreshService(
                config=config,
                onedrive=drive,  # type: ignore[arg-type]
                index=index,
                default_drive_id="drive",
            )
            binding = service.live_binding([ref], verify_content=True)
            self.assertEqual(binding, index.status().binding())

            metadata_changes = {
                "version": replace(file_entry, version_tag="v2"),
                "etag": replace(file_entry, etag="etag-v2"),
            }
            for label, changed_entry in metadata_changes.items():
                with self.subTest(label=label):
                    changed_drive = BindingDrive()
                    changed_drive.get_entry = (  # type: ignore[method-assign]
                        lambda drive_id, item_id, entry=changed_entry: (
                            root_entry if item_id == "root" else entry
                        )
                    )
                    changed = OneDriveWikiRefreshService(
                        config=config,
                        onedrive=changed_drive,  # type: ignore[arg-type]
                        index=index,
                        default_drive_id="drive",
                    )
                    with self.assertRaisesRegex(
                        WikiRefreshError, "cited OneDrive document"
                    ):
                        changed.live_binding([ref], verify_content=True)

            changed_content_drive = BindingDrive()
            changed_content_drive.download_current = (  # type: ignore[method-assign]
                lambda drive_id, item_id: b"x" * len(data)
            )
            changed_content = OneDriveWikiRefreshService(
                config=config,
                onedrive=changed_content_drive,  # type: ignore[arg-type]
                index=index,
                default_drive_id="drive",
            )
            with self.assertRaisesRegex(WikiRefreshError, "document content changed"):
                changed_content.live_binding([ref], verify_content=True)

    def test_gateway_guard_preserves_all_conflicting_refs(self) -> None:
        valid = KnowledgeRef(
            drive_id="drive",
            item_id="rule",
            version_id="v1",
            content_hash="content-v1",
            chunk_locator="chars:0-10",
            chunk_hash="chunk-v1",
            security_classification="internal_reference",
            etag="etag-v1",
        )
        conflicting = replace(valid, content_hash="content-conflict")
        proposal = SimpleNamespace(
            operations=(
                SimpleNamespace(
                    change=SimpleNamespace(knowledge_refs=(conflicting,))
                ),
                SimpleNamespace(change=SimpleNamespace(knowledge_refs=(valid,))),
            )
        )
        config = SimpleNamespace(
            wiki=WikiConfig(),
            onedrive=SimpleNamespace(drive_id="drive"),
        )
        service = MagicMock()
        service.live_binding.return_value = {"generation_digest": "digest"}
        with patch(
            "notion_excel_sync.security.hermes_gateway.OneDriveWikiRefreshService",
            return_value=service,
        ):
            guard = _wiki_live_binding_guard(
                config,  # type: ignore[arg-type]
                MagicMock(),
                MagicMock(),
                proposal,  # type: ignore[arg-type]
            )
            self.assertEqual(guard(), {"generation_digest": "digest"})
            self.assertEqual(guard(), {"generation_digest": "digest"})

        first_call, second_call = service.live_binding.call_args_list
        self.assertEqual(first_call.args[0], (conflicting, valid))
        self.assertTrue(first_call.kwargs["verify_content"])
        self.assertFalse(second_call.kwargs["verify_content"])

    def test_incremental_generation_caps_replace_prior_chunk_totals(self) -> None:
        cases = (
            ("replacement_frees_old_total", 10, 20, "67890", True),
            ("overlap_counts_as_persisted_text", 24, 20, "abcdefghijklmno", False),
            ("replacement_frees_old_chunks", 100, 2, "67890", True),
            ("new_chunks_respect_chunk_cap", 100, 2, "abcdefghijklmno", False),
        )
        for label, max_chars, max_chunks, new_text, expected_indexed in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "wiki.db"
                config = WikiConfig(
                    enabled=True,
                    drive_id="drive",
                    root_item_id="root",
                    root_path="",
                    index_db=path,
                    include_roots=("Rules",),
                    exclude_roots=(),
                    selective_roots=(),
                    allowed_extensions=(".txt",),
                    max_generation_chars=max_chars,
                    max_generation_chunks=max_chunks,
                    chunk_chars=10,
                    chunk_overlap_chars=5,
                )
                policy_version = policy_fingerprint(config)

                def indexed_document(
                    item_id: str, text: str
                ) -> tuple[KnowledgeDocument, tuple]:
                    return (
                        KnowledgeDocument(
                            drive_id="drive",
                            item_id=item_id,
                            version_id="v1",
                            etag="etag-v1",
                            ctag="ctag-v1",
                            display_name=f"{item_id}.txt",
                            relative_path=f"Rules/{item_id}.txt",
                            content_hash=hashlib.sha256(text.encode()).hexdigest(),
                            last_modified_at="2026-07-21T00:00:00+00:00",
                            size=len(text.encode()),
                            mime_type="text/plain",
                            category="reference",
                            authority="reference",
                            status="unverified",
                            security_classification="internal_reference",
                            policy_version=policy_version,
                        ),
                        chunk_text(
                            item_id,
                            text,
                            chunk_chars=config.chunk_chars,
                            overlap_chars=config.chunk_overlap_chars,
                        ),
                    )

                index = WikiIndex(path)
                index.apply_generation(
                    drive_id="drive",
                    root_item_id="root",
                    root_version_tag="root-v1",
                    policy_version=policy_version,
                    delta_link="delta-v1",
                    upserts=[
                        indexed_document("stable", "12345"),
                        indexed_document("changed", "abcdefghijklmno"),
                    ],
                    full_rebuild=True,
                )
                before = index.document_inventory()
                self.assertEqual(before["stable"]["char_count"], 5)
                self.assertEqual(before["changed"]["char_count"], 20)

                payload = b"x" * len(new_text)
                root = DriveEntry(
                    drive_id="drive",
                    item_id="root",
                    name="wiki-root",
                    relative_path="",
                    parent_item_id=None,
                    parent_path="/drives/drive/root:",
                    is_folder=True,
                    version_tag="root-v2",
                    last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
                    etag="root-v2",
                )
                changed_entry = DriveEntry(
                    drive_id="drive",
                    item_id="changed",
                    name="changed.txt",
                    relative_path="Rules/changed.txt",
                    parent_item_id="root",
                    parent_path="/drives/drive/root:/wiki-root/Rules",
                    is_folder=False,
                    version_tag="v2",
                    last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
                    size=len(payload),
                    mime_type="text/plain",
                    etag="v2",
                    ctag="ctag-v2",
                )

                class IncrementalDrive:
                    def read_folder_delta(self, *args, **kwargs):
                        if kwargs.get("delta_link") != "delta-v1":
                            raise AssertionError(kwargs)
                        return DriveFolderDelta(
                            root=root,
                            entries=(changed_entry,),
                            deleted_item_ids=(),
                            delta_link="delta-v2",
                        )

                    def download_current(self, drive_id, item_id):
                        if (drive_id, item_id) != ("drive", "changed"):
                            raise AssertionError((drive_id, item_id))
                        return payload

                    def get_entry(self, drive_id, item_id):
                        if drive_id != "drive":
                            raise AssertionError(drive_id)
                        return root if item_id == "root" else changed_entry

                service = OneDriveWikiRefreshService(
                    config=config,
                    onedrive=IncrementalDrive(),  # type: ignore[arg-type]
                    index=index,
                    default_drive_id="drive",
                )
                with patch(
                    "notion_excel_sync.knowledge.refresh._isolated_extract",
                    return_value=(new_text, ()),
                ):
                    report = service.refresh()

                inventory = index.document_inventory()
                self.assertEqual(report.indexed_documents, int(expected_indexed))
                if expected_indexed:
                    self.assertEqual(set(inventory), {"stable", "changed"})
                    self.assertEqual(inventory["changed"]["version_id"], "v2")
                    self.assertLessEqual(
                        sum(int(item["char_count"]) for item in inventory.values()),
                        max_chars,
                    )
                    self.assertLessEqual(
                        sum(int(item["chunk_count"]) for item in inventory.values()),
                        max_chunks,
                    )
                else:
                    self.assertEqual(set(inventory), {"stable"})
                    self.assertEqual(
                        report.skipped_reasons,
                        {"generation_text_capacity_limit": 1},
                    )

    def test_semantic_integrity_detects_chunk_digest_search_and_fts_tampering(self) -> None:
        mutations = {
            "chunk_text": (
                "UPDATE chunk SET text = text || '변조'",
                "chunk hash",
            ),
            "search_terms": (
                "UPDATE chunk SET search_terms = 'tampered'",
                "search terms",
            ),
            "fts": (
                "UPDATE chunk_fts SET search_terms = 'tampered'",
                "full-text index",
            ),
            "generation_digest": (
                "UPDATE metadata SET value = ? WHERE key = 'generation_digest'",
                "generation digest",
            ),
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, (statement, expected_error) in mutations.items():
                with self.subTest(name=name):
                    index = _build_index(root / f"{name}.db")
                    connection = sqlite3.connect(index.path)
                    try:
                        if name == "generation_digest":
                            connection.execute(statement, (json.dumps("0" * 64),))
                        else:
                            connection.execute(statement)
                        connection.commit()
                    finally:
                        connection.close()
                    with self.assertRaisesRegex(WikiIndexError, expected_error):
                        index.verify_integrity()

    def test_semantic_integrity_detects_foreign_key_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            index = _build_index(Path(folder) / "wiki.db")
            connection = sqlite3.connect(index.path)
            try:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("DELETE FROM document")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(WikiIndexError, "foreign-key"):
                index.verify_integrity()

    def test_integrity_can_attest_an_older_extractor_generation_for_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            index = _build_index(Path(folder) / "wiki.db")
            connection = index._connect(index.path)
            try:
                with connection:
                    legacy_version = "safe-ooxml-text-v3"
                    connection.execute(
                        "UPDATE document SET extractor_version = ?",
                        (legacy_version,),
                    )
                    digest = index._generation_digest(
                        connection,
                        drive_id="drive",
                        root_item_id="root",
                        root_version_tag="root-v1",
                        policy_version="policy-v1",
                        extractor_version=legacy_version,
                    )
                    connection.execute(
                        "UPDATE metadata SET value = ? WHERE key = 'extractor_version'",
                        (json.dumps(legacy_version),),
                    )
                    connection.execute(
                        "UPDATE metadata SET value = ? WHERE key = 'generation_digest'",
                        (json.dumps(digest),),
                    )
            finally:
                connection.close()
            self.assertEqual(index.verify_integrity().extractor_version, legacy_version)

    def test_generation_lease_avoids_recursive_lock_and_cannot_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            index = WikiIndex(Path(folder) / "wiki.db")
            with index.generation_lock(timeout_seconds=1.0) as lease:
                snapshot = index.apply_generation(
                    drive_id="drive",
                    root_item_id="root",
                    root_version_tag="root-v1",
                    policy_version="policy-v1",
                    delta_link="delta",
                    upserts=[_document("rule", "특허 등록료 납부 기한을 확인한다.")],
                    full_rebuild=True,
                    generation_lock=lease,
                )
                self.assertEqual(snapshot.document_count, 1)
                with self.assertRaises(WikiGenerationLockTimeout):
                    with index.generation_lock(timeout_seconds=0.05):
                        self.fail("recursive lock unexpectedly succeeded")
            with self.assertRaisesRegex(WikiIndexError, "lease is not valid"):
                index.apply_generation(
                    drive_id="drive",
                    root_item_id="root",
                    root_version_tag="root-v2",
                    policy_version="policy-v1",
                    delta_link="delta-2",
                    upserts=[],
                    generation_lock=lease,
                )

    def test_generation_lock_excludes_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            index_path = root / "wiki.db"
            ready = root / "ready"
            release = root / "release"
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_hold_generation_lock,
                args=(str(index_path), str(ready), str(release)),
            )
            process.start()
            deadline = time.monotonic() + 5.0
            while not ready.exists() and process.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            try:
                self.assertTrue(ready.exists(), "child process did not acquire the lock")
                with self.assertRaises(WikiGenerationLockTimeout):
                    with WikiIndex(index_path).generation_lock(timeout_seconds=0.15):
                        self.fail("cross-process lock unexpectedly succeeded")
            finally:
                release.touch()
                process.join(timeout=5.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2.0)
            self.assertEqual(process.exitcode, 0)

    def test_refresh_holds_generation_lock_for_download_through_publication(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root_path = Path(folder)
            index = WikiIndex(root_path / "wiki.db")
            root = DriveEntry(
                drive_id="drive",
                item_id="root",
                name="[상상] 업무분류",
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
            payload = "정부지원사업 증빙 지침".encode()
            document = DriveEntry(
                drive_id="drive",
                item_id="rule",
                name="증빙 지침.txt",
                relative_path="9. 법령/증빙 지침.txt",
                parent_item_id="root",
                parent_path="/drive/root:/[상상] 업무분류/9. 법령",
                is_folder=False,
                version_tag="rule-v1",
                last_modified_at=datetime(2026, 7, 21, tzinfo=UTC),
                size=len(payload),
                mime_type="text/plain",
                etag="rule-v1",
                ctag="rule-c1",
            )
            download_started = threading.Event()
            release_download = threading.Event()

            class FakeOneDrive:
                def get_folder_delta_checkpoint(self, *_args, **_kwargs):
                    return DriveFolderDelta(
                        root=root,
                        entries=(),
                        deleted_item_ids=(),
                        delta_link="checkpoint-v1",
                    )

                def walk_folder(self, *_args, **_kwargs):
                    return [document]

                def read_folder_delta(self, *_args, **kwargs):
                    if kwargs.get("delta_link") != "checkpoint-v1":
                        raise AssertionError(kwargs)
                    return DriveFolderDelta(
                        root=root,
                        entries=(),
                        deleted_item_ids=(),
                        delta_link="delta-v1",
                    )

                def download_current(self, *_args):
                    download_started.set()
                    if not release_download.wait(timeout=5.0):
                        raise AssertionError("test did not release the download")
                    return payload

                def get_entry(self, _drive_id, item_id):
                    return root if item_id == "root" else document

            config = replace(
                WikiConfig(),
                enabled=True,
                drive_id="drive",
                root_item_id="root",
                root_path="",
                index_db=index.path,
            )
            service = OneDriveWikiRefreshService(
                config=config,
                onedrive=FakeOneDrive(),  # type: ignore[arg-type]
                index=index,
                default_drive_id="drive",
                work_dir=root_path / "work",
            )
            errors: list[BaseException] = []

            def run_refresh() -> None:
                try:
                    service.refresh(force_full=True)
                except BaseException as exc:  # surfaced in the parent test thread
                    errors.append(exc)

            with patch(
                "notion_excel_sync.knowledge.refresh._isolated_extract",
                return_value=("정부지원사업 증빙 지침", ()),
            ):
                worker = threading.Thread(target=run_refresh)
                worker.start()
                self.assertTrue(download_started.wait(timeout=2.0))
                with self.assertRaises(WikiGenerationLockTimeout):
                    with WikiIndex(index.path).generation_lock(timeout_seconds=0.1):
                        self.fail("refresh did not retain the generation lock")
                release_download.set()
                worker.join(timeout=5.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(index.verify_integrity().document_count, 1)


if __name__ == "__main__":
    unittest.main()
