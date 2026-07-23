from __future__ import annotations

from pathlib import Path

import pytest

from notion_excel_sync.adapters import local_directory, local_files
from notion_excel_sync.adapters.local_directory import (
    LocalDirectoryDeltaError,
    LocalDirectoryPathError,
    LocalDirectoryReadOnlyAdapter,
)
from notion_excel_sync.adapters.local_files import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    HydrationPolicy,
    LocalFileHydrationRequired,
)
from notion_excel_sync.config import WikiConfig
from notion_excel_sync.knowledge.index import WikiIndex
from notion_excel_sync.knowledge.refresh import OneDriveWikiRefreshService


def _source_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    rules = source / "Wiki" / "Rules"
    rules.mkdir(parents=True)
    document = rules / "guide.txt"
    document.write_text("approved local guidance", encoding="utf-8")
    return source, rules, document


def _adapter(
    source: Path,
    runtime: Path,
    *,
    hydration_policy: HydrationPolicy | str = HydrationPolicy.DENY,
) -> LocalDirectoryReadOnlyAdapter:
    return LocalDirectoryReadOnlyAdapter(
        source,
        runtime,
        drive_id="local-drive",
        hydration_policy=hydration_policy,
    )


def test_graph_like_surface_is_compatible_with_wiki_catalog_and_download(
    tmp_path: Path,
) -> None:
    source, _, document = _source_tree(tmp_path)
    runtime = tmp_path / "runtime"
    adapter = _adapter(source, runtime)
    root = adapter.get_item_by_path("local-drive", "Wiki")
    by_path = adapter.get_item_by_path("local-drive", "Wiki/Rules/guide.txt")

    assert root.is_folder
    assert root.parent_path == "/drives/local-drive/root:/source"
    assert by_path.parent_path == "/drives/local-drive/root:/source/Wiki/Rules"
    assert adapter.get_entry("local-drive", by_path.item_id).item_id == by_path.item_id
    assert adapter.download_current("local-drive", by_path.item_id) == document.read_bytes()

    config = WikiConfig(
        enabled=True,
        drive_id="local-drive",
        root_item_id=root.item_id,
        root_path="",
        index_db=runtime / "wiki.db",
        include_roots=("Rules",),
    )
    service = OneDriveWikiRefreshService(
        config=config,
        onedrive=adapter,  # type: ignore[arg-type]
        index=WikiIndex(config.index_db),
        default_drive_id="local-drive",
    )

    catalog = service._read_consistent_full_catalog(
        "local-drive",
        root.item_id,
        progress=None,
    )

    assert [entry.relative_path for entry in catalog.entries] == [
        "Rules",
        "Rules/guide.txt",
    ]
    expected = next(entry for entry in catalog.entries if not entry.is_folder)
    current = adapter.get_entry("local-drive", expected.item_id)
    payload = adapter.download_current("local-drive", expected.item_id)
    service._validate_download_binding(
        drive_id="local-drive",
        root_item_id=root.item_id,
        root=catalog.root,
        expected=expected,
        current=current,
        data_size=len(payload),
    )


def test_delta_detects_move_modify_and_delete_with_stable_inode_id(
    tmp_path: Path,
) -> None:
    source, _, document = _source_tree(tmp_path)
    adapter = _adapter(source, tmp_path / "runtime")
    root = adapter.get_item_by_path("local-drive", "Wiki")
    original = adapter.get_item_by_path(
        "local-drive",
        "Wiki/Rules/guide.txt",
    )
    checkpoint = adapter.get_folder_delta_checkpoint(
        "local-drive",
        root.item_id,
    )

    moved_path = document.with_name("renamed.txt")
    document.rename(moved_path)
    moved_delta = adapter.read_folder_delta(
        "local-drive",
        root.item_id,
        delta_link=checkpoint.delta_link,
    )
    moved = next(
        entry for entry in moved_delta.entries if entry.item_id == original.item_id
    )
    assert moved.relative_path == "Rules/renamed.txt"
    assert original.item_id not in moved_delta.deleted_item_ids

    moved_path.write_text("materially modified content", encoding="utf-8")
    modified_delta = adapter.read_folder_delta(
        "local-drive",
        root.item_id,
        delta_link=moved_delta.delta_link,
    )
    modified = next(
        entry for entry in modified_delta.entries if entry.item_id == original.item_id
    )
    assert modified.version_tag != moved.version_tag
    assert modified.size != moved.size

    moved_path.unlink()
    deleted_delta = adapter.read_folder_delta(
        "local-drive",
        root.item_id,
        delta_link=modified_delta.delta_link,
    )
    assert original.item_id in deleted_delta.deleted_item_ids
    assert all(entry.item_id != original.item_id for entry in deleted_delta.entries)


def test_relative_path_fallback_reports_move_as_delete_and_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, document = _source_tree(tmp_path)
    monkeypatch.setattr(
        local_directory,
        "_usable_inode",
        lambda _source_stat, *, is_folder: False,
    )
    adapter = _adapter(source, tmp_path / "runtime")
    root = adapter.get_item_by_path("local-drive", "Wiki")
    original = adapter.get_item_by_path(
        "local-drive",
        "Wiki/Rules/guide.txt",
    )
    checkpoint = adapter.get_folder_delta_checkpoint(
        "local-drive",
        root.item_id,
    )

    document.rename(document.with_name("renamed.txt"))
    delta = adapter.read_folder_delta(
        "local-drive",
        root.item_id,
        delta_link=checkpoint.delta_link,
    )

    replacement = next(entry for entry in delta.entries if not entry.is_folder)
    assert original.item_id.startswith("local-path-")
    assert replacement.item_id != original.item_id
    assert delta.deleted_item_ids == (original.item_id,)


def test_delta_manifest_is_root_scoped_atomic_and_outside_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    first = source / "A"
    second = source / "B"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "a.txt").write_text("a", encoding="utf-8")
    (second / "b.txt").write_text("b", encoding="utf-8")
    runtime = tmp_path / "runtime"
    adapter = _adapter(source, runtime)
    first_entry = adapter.get_item_by_path("local-drive", "A")
    second_entry = adapter.get_item_by_path("local-drive", "B")
    source_inventory = sorted(path.relative_to(source) for path in source.rglob("*"))

    checkpoint = adapter.get_folder_delta_checkpoint(
        "local-drive",
        first_entry.item_id,
    )

    assert sorted(path.relative_to(source) for path in source.rglob("*")) == source_inventory
    manifests = list(runtime.rglob("*.json"))
    assert len(manifests) == 1
    assert not list(runtime.rglob("*.tmp"))
    with pytest.raises(LocalDirectoryDeltaError, match="cross root scopes"):
        adapter.read_folder_delta(
            "local-drive",
            second_entry.item_id,
            delta_link=checkpoint.delta_link,
        )

    manifests[0].write_text("{}", encoding="utf-8")
    with pytest.raises(LocalDirectoryDeltaError, match="content binding"):
        adapter.read_folder_delta(
            "local-drive",
            first_entry.item_id,
            delta_link=checkpoint.delta_link,
        )


def test_rejects_runtime_overlap_traversal_and_symbolic_links(
    tmp_path: Path,
) -> None:
    source, _, _ = _source_tree(tmp_path)
    nested_runtime = source / "runtime"

    with pytest.raises(LocalDirectoryPathError, match="separate"):
        _adapter(source, nested_runtime)
    assert not nested_runtime.exists()

    adapter = _adapter(source, tmp_path / "runtime")
    with pytest.raises(LocalDirectoryPathError, match="canonical relative"):
        adapter.get_item_by_path("local-drive", "../outside")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = source / "Wiki" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted on this host")
    root = adapter.get_item_by_path("local-drive", "Wiki")
    with pytest.raises(LocalDirectoryPathError, match="Symbolic links"):
        adapter.walk_folder("local-drive", root.item_id)


def test_online_only_files_follow_explicit_hydration_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, document = _source_tree(tmp_path)
    expected_bytes = document.read_bytes()
    denied = _adapter(source, tmp_path / "runtime-denied")
    entry = denied.get_item_by_path("local-drive", "Wiki/Rules/guide.txt")
    monkeypatch.setattr(
        local_files,
        "_windows_file_attributes",
        lambda _stat: FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    )
    source_opens: list[str] = []
    real_open = Path.open

    def observed_open(
        path: Path,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ) -> object:
        if path == document:
            source_opens.append(mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)
    with pytest.raises(LocalFileHydrationRequired, match="explicit read-only"):
        denied.download_current("local-drive", entry.item_id)
    assert source_opens == []

    allowed = _adapter(
        source,
        tmp_path / "runtime-allowed",
        hydration_policy=HydrationPolicy.READ_ONLY,
    )
    allowed_entry = allowed.get_item_by_path(
        "local-drive",
        "Wiki/Rules/guide.txt",
    )
    monkeypatch.setattr(
        local_files,
        "_windows_file_attributes",
        lambda _stat: FILE_ATTRIBUTE_RECALL_ON_OPEN,
    )

    assert allowed.download_current("local-drive", allowed_entry.item_id) == expected_bytes
    assert source_opens == ["rb"]


def test_drive_scope_and_traversal_limits_fail_closed(tmp_path: Path) -> None:
    source, _, _ = _source_tree(tmp_path)
    adapter = _adapter(source, tmp_path / "runtime")
    root = adapter.get_item_by_path("local-drive", "Wiki")

    with pytest.raises(Exception, match="drive binding mismatch"):
        adapter.get_entry("other-drive", root.item_id)
    with pytest.raises(Exception, match="max_items"):
        adapter.walk_folder("local-drive", root.item_id, max_items=1)
    with pytest.raises(ValueError, match="max_workers"):
        adapter.walk_folder("local-drive", root.item_id, max_workers=5)
