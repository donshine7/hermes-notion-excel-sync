from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from notion_excel_sync.adapters import local_files
from notion_excel_sync.adapters.local_files import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    HydrationPolicy,
    LocalFileChangedError,
    LocalFileHydrationRequired,
    LocalFilePathError,
    LocalFileReadOnlyAdapter,
    is_windows_online_only,
)


def test_requires_absolute_root_and_file_paths(tmp_path: Path) -> None:
    with pytest.raises(LocalFilePathError, match="root must be an absolute"):
        LocalFileReadOnlyAdapter("relative")

    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    adapter = LocalFileReadOnlyAdapter(tmp_path)

    with pytest.raises(LocalFilePathError, match="file must be an absolute"):
        adapter.read("source.txt")


def test_rejects_files_outside_the_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    adapter = LocalFileReadOnlyAdapter(root)

    with pytest.raises(LocalFilePathError, match="below the configured root"):
        adapter.read(outside)


def test_read_returns_content_bound_manifest_without_modifying_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    nested = root / "folder"
    nested.mkdir(parents=True)
    source = nested / "문서.txt"
    payload = "읽기 전용 원본".encode()
    source.write_bytes(payload)
    before = source.stat()

    result = LocalFileReadOnlyAdapter(root).read(source)
    after = source.stat()

    assert result.content == payload
    assert result.manifest.relative_path == "folder/문서.txt"
    assert result.manifest.size == len(payload)
    assert result.manifest.revision == hashlib.sha256(payload).hexdigest()
    assert result.manifest.revision_algorithm == "sha256"
    assert result.manifest.online_only_before_read is False
    assert result.manifest.as_dict()["revision"] == {
        "algorithm": "sha256",
        "value": hashlib.sha256(payload).hexdigest(),
    }
    assert source.read_bytes() == payload
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns


def test_manifest_hashes_without_retaining_content(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    payload = b"\x00\x01\x02"
    source.write_bytes(payload)

    manifest = LocalFileReadOnlyAdapter(tmp_path).manifest(source)

    assert manifest.relative_path == "source.bin"
    assert manifest.revision == hashlib.sha256(payload).hexdigest()
    assert manifest.size == len(payload)


def test_rejects_a_file_that_changes_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("stable", encoding="utf-8")
    adapter = LocalFileReadOnlyAdapter(tmp_path)
    real_fstat = os.fstat
    calls = 0

    def unstable_fstat(file_descriptor: int) -> object:
        nonlocal calls
        calls += 1
        current = real_fstat(file_descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=current.st_dev,
                st_ino=current.st_ino,
                st_mode=current.st_mode,
                st_size=current.st_size + 1,
                st_mtime_ns=current.st_mtime_ns,
                st_ctime_ns=current.st_ctime_ns,
            )
        return current

    monkeypatch.setattr(local_files.os, "fstat", unstable_fstat)

    with pytest.raises(LocalFileChangedError, match="changed while"):
        adapter.read(source)


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        (None, False),
        (0, False),
        (FILE_ATTRIBUTE_OFFLINE, True),
        (FILE_ATTRIBUTE_RECALL_ON_OPEN, True),
        (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, True),
        (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, True),
    ],
)
def test_windows_online_only_attribute_detection(
    attributes: int | None, expected: bool
) -> None:
    assert is_windows_online_only(attributes) is expected


def test_online_only_file_is_not_opened_without_explicit_hydration_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "online-only.txt"
    source.write_text("cloud", encoding="utf-8")
    adapter = LocalFileReadOnlyAdapter(tmp_path)
    monkeypatch.setattr(
        local_files,
        "_windows_file_attributes",
        lambda _stat: FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    )
    opened = False
    real_open = Path.open

    def observed_open(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal opened
        opened = True
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)

    with pytest.raises(LocalFileHydrationRequired, match="explicit read-only"):
        adapter.read(source)
    assert opened is False


def test_explicit_read_only_policy_allows_placeholder_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "online-only.txt"
    payload = b"cloud"
    source.write_bytes(payload)
    monkeypatch.setattr(
        local_files,
        "_windows_file_attributes",
        lambda _stat: FILE_ATTRIBUTE_RECALL_ON_OPEN,
    )
    observed_modes: list[str] = []
    real_open = Path.open

    def observed_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        observed_modes.append(mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)
    adapter = LocalFileReadOnlyAdapter(
        tmp_path,
        hydration_policy=HydrationPolicy.READ_ONLY,
    )

    result = adapter.read(source)

    assert result.content == payload
    assert result.manifest.online_only_before_read is True
    assert result.manifest.windows_file_attributes == FILE_ATTRIBUTE_RECALL_ON_OPEN
    assert observed_modes == ["rb"]
