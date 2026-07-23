from __future__ import annotations

import io
import json
import unittest
from datetime import UTC, datetime, timedelta

from openpyxl import Workbook

from notion_excel_sync.adapters.excel import (
    DuplicateRecordKeyError,
    ExcelSnapshotReader,
    SheetSpec,
    diff_snapshots,
)
from notion_excel_sync.adapters.notion import (
    ApprovalRequiredError,
    HttpNotionGateway,
    InMemoryNotionGateway,
    NotionError,
    NotionMatch,
    NotionPrecondition,
)
from notion_excel_sync.adapters.onedrive import (
    BaselineVersionNotFound,
    DriveVersion,
    OneDriveError,
    OneDriveReadOnlyClient,
    select_baseline_version,
)
from notion_excel_sync.adapters.telegram import (
    TelegramProposalFormatter,
)
from notion_excel_sync.models import (
    ApprovalReceipt,
    ChangeKind,
    ProposalOperation,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    SourceRef,
)


def _xlsx(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "2026"
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _check_excel_reader_builds_provenance_and_stable_diff() -> None:
    reader = ExcelSnapshotReader(
        [
            SheetSpec(
                "2026",
                key_columns=("ID", "사건번호"),
                required_columns=("비용",),
            )
        ]
    )
    baseline = reader.read_bytes(
        _xlsx(
            [
                ["사건번호", "상태", "비용"],
                ["S-1", "접수", 100_000],
                ["S-2", "진행", 200_000],
            ]
        ),
        drive_id="drive",
        item_id="item",
        version_id="1.0",
    )
    current = reader.read_bytes(
        _xlsx(
            [
                ["사건번호", "상태", "비용"],
                ["S-3", "접수", 300_000],
                ["S-1", "완료", 100_000],
            ]
        ),
        drive_id="drive",
        item_id="item",
        version_id="2.0",
    )

    changes = diff_snapshots(baseline, current)
    assert {change.kind for change in changes} == {
        ChangeKind.CREATE,
        ChangeKind.UPDATE,
        ChangeKind.DELETE_CANDIDATE,
    }
    updated = next(change for change in changes if change.kind is ChangeKind.UPDATE)
    assert updated.after is not None
    assert updated.after.values["상태"] == "완료"
    assert {ref.cells for ref in updated.after.source_refs} == {"A3", "B3", "C3"}
    assert all(ref.file_hash == current.file_hash for ref in updated.after.source_refs)


def _workbook_reader() -> ExcelSnapshotReader:
    return ExcelSnapshotReader(
        [
            SheetSpec(
                "2026",
                key_groups=(
                    ("사건번호", "종류", "작업설명", "받은날짜"),
                    ("사건번호", "종류", "작업설명"),
                    ("사건번호",),
                ),
            )
        ]
    )


def _check_repeated_case_tasks_use_composite_occurrence_keys() -> None:
    header = ["사건번호", "종류", "작업설명", "받은날짜", "법정기일", "비고"]
    rows = [
        ["P211483", "출원", "국내출원", "2026-01-02", "2026-03-02", "a"],
        ["P211483", "중간", "의견통지 대응", "2026-02-03", "2026-04-03", "b"],
        ["P211483", "번역", "명세서 번역", "2026-02-04", "2026-05-04", "c"],
        ["P212412", "중간", "의견통지 대응", "2022-03-28", "2022-05-28", "첫째"],
        ["P212412", "중간", "의견통지 대응", "2022-03-28", "2022-07-28", "둘째"],
        ["SPARSE-1", "기타", "날짜 미정", None, None, None],
        ["SPARSE-2", None, None, None, None, None],
    ]
    reader = _workbook_reader()
    baseline = reader.read_bytes(
        _xlsx([header, *rows]),
        drive_id="drive",
        item_id="item",
        version_id="1",
    )
    current = reader.read_bytes(
        _xlsx([header, rows[2], rows[0], rows[1], *rows[3:]]),
        drive_id="drive",
        item_id="item",
        version_id="2",
    )

    assert len({record.key for record in baseline.records}) == len(rows)
    assert all("::occurrence::" in record.key for record in baseline.records)
    assert diff_snapshots(baseline, current) == []
    assert len(baseline.identity_warnings) == 1
    assert "P212412" in baseline.identity_warnings[0]


def _check_occurrence_tradeoff_and_identical_rows_fail_closed() -> None:
    header = ["사건번호", "종류", "작업설명", "받은날짜", "법정기일"]
    first = ["P212412", "중간", "의견통지 대응", "2022-03-28", "2022-05-28"]
    second = ["P212412", "중간", "의견통지 대응", "2022-03-28", "2022-07-28"]
    reader = _workbook_reader()
    baseline = reader.read_bytes(
        _xlsx([header, first, second]),
        drive_id="drive",
        item_id="item",
        version_id="1",
    )
    reordered = reader.read_bytes(
        _xlsx([header, second, first]),
        drive_id="drive",
        item_id="item",
        version_id="2",
    )
    assert [change.kind for change in diff_snapshots(baseline, reordered)] == [
        ChangeKind.UPDATE,
        ChangeKind.UPDATE,
    ]

    with unittest.TestCase().assertRaises(DuplicateRecordKeyError):
        reader.read_bytes(
            _xlsx([header, first, first]),
            drive_id="drive",
            item_id="item",
            version_id="3",
        )


def _check_registration_sheet_key_fallbacks() -> None:
    reader = ExcelSnapshotReader(
        [
            SheetSpec(
                "2026",
                key_groups=(
                    ("사건번호", "등록 마감일"),
                    ("사건번호",),
                    ("의뢰인",),
                ),
            )
        ]
    )
    snapshot = reader.read_bytes(
        _xlsx(
            [
                ["사건번호", "등록 마감일", "의뢰인"],
                ["R-1", "2026-09-01", "A"],
                ["R-2", None, "B"],
                [None, None, "C"],
            ]
        ),
        drive_id="drive",
        item_id="item",
        version_id="1",
    )
    assert len({record.key for record in snapshot.records}) == 3
    assert '"등록 마감일"' in snapshot.records[0].key
    assert '"사건번호"' in snapshot.records[1].key
    assert '"의뢰인"' in snapshot.records[2].key


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def _check_onedrive_response_size_is_bounded_before_json_decode() -> None:
    def opener(request: object, *, timeout: float) -> _Response:
        del request, timeout
        return _Response(b"x" * 11, {"Content-Length": "11"})

    client = OneDriveReadOnlyClient(
        "token",
        base_url="https://graph.test/v1.0",
        opener=opener,
        max_json_bytes=10,
        max_retries=0,
    )
    with unittest.TestCase().assertRaisesRegex(OneDriveError, "configured limit"):
        client.get_my_drive_id()


def _check_onedrive_client_pages_versions_selects_cutoff_and_only_uses_get() -> None:
    calls: list[tuple[str, str]] = []
    first = "https://graph.test/v1.0/drives/d/items/i/versions?$top=200"
    second = "https://graph.test/v1.0/next"

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        method = request.get_method()  # type: ignore[attr-defined]
        calls.append((method, url))
        if url == first:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            {"id": "1.0", "lastModifiedDateTime": "2026-07-15T08:00:00Z"}
                        ],
                        "@odata.nextLink": second,
                    }
                ).encode()
            )
        if url == second:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            {"id": "2.0", "lastModifiedDateTime": "2026-07-16T08:00:00Z"}
                        ]
                    }
                ).encode()
            )
        if url.endswith("/versions/1.0/content"):
            return _Response(b"old-xlsx")
        if url.endswith("/content"):
            return _Response(b"current-xlsx")
        raise AssertionError(url)

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    versions = client.list_versions("d", "i")
    baseline = client.select_initial_baseline(
        "d", "i", datetime(2026, 7, 15, 23, 59, tzinfo=UTC)
    )
    assert [version.id for version in versions] == ["1.0", "2.0"]
    assert baseline.id == "1.0"
    assert client.download_version("d", "i", "1.0") == b"old-xlsx"
    assert client.download_current("d", "i") == b"current-xlsx"
    assert calls and {method for method, _ in calls} == {"GET"}


def _check_onedrive_search_returns_exact_read_only_identifiers() -> None:
    calls: list[tuple[str, str]] = []

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        method = request.get_method()  # type: ignore[attr-defined]
        calls.append((method, url))
        return _Response(
            json.dumps(
                {
                    "value": [
                        {
                            "id": "item-1",
                            "name": "나의업무관리.xlsx",
                            "webUrl": "https://onedrive.live.com/example",
                            "size": 1234,
                            "file": {"mimeType": "application/vnd.ms-excel"},
                            "parentReference": {"driveId": "drive-1"},
                        },
                        {
                            "id": "folder-1",
                            "name": "나의업무관리.xlsx",
                            "folder": {},
                            "parentReference": {"driveId": "drive-1"},
                        },
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    results = client.search_items("나의업무관리.xlsx")
    assert [(item.drive_id, item.item_id) for item in results] == [
        ("drive-1", "item-1")
    ]
    assert len(calls) == 1
    assert calls[0][0] == "GET"
    assert "/me/drive/root/search" in calls[0][1]


def _check_onedrive_delta_enumerates_current_files_with_get_only() -> None:
    calls: list[tuple[str, str]] = []
    second = "https://graph.test/v1.0/delta-page-2"

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        method = request.get_method()  # type: ignore[attr-defined]
        calls.append((method, url))
        if url.endswith("/me/drive?$select=id"):
            return _Response(json.dumps({"id": "drive-1"}).encode())
        if "/me/drive/root/delta" in url:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            {
                                "id": "item-1",
                                "name": "나의업무관리.xlsx",
                                "file": {},
                                "parentReference": {"driveId": "drive-1"},
                            }
                        ],
                        "@odata.nextLink": second,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        if url == second:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            {"id": "item-1", "deleted": {}},
                            {
                                "id": "item-2",
                                "name": "나의업무관리.xlsx",
                                "file": {},
                            },
                        ],
                        "@odata.deltaLink": "https://graph.test/v1.0/delta-current",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        raise AssertionError(url)

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    results = client.enumerate_items()
    assert [(item.drive_id, item.item_id) for item in results] == [
        ("drive-1", "item-2")
    ]
    assert {method for method, _ in calls} == {"GET"}


def _check_onedrive_folder_walk_is_root_bound_and_stable() -> None:
    calls: list[tuple[str, str]] = []

    def item(
        item_id: str,
        name: str,
        *,
        parent_id: str,
        folder: bool,
    ) -> dict[str, object]:
        return {
            "id": item_id,
            "name": name,
            "lastModifiedDateTime": "2026-07-20T01:02:03Z",
            "size": 0 if folder else 123,
            "eTag": f'\"{item_id},1\"',
            "cTag": f'c:{item_id}',
            "parentReference": {
                "driveId": "drive-1",
                "id": parent_id,
                "path": "/drives/drive-1/root:/Desktop",
            },
            "folder" if folder else "file": (
                {"childCount": 1} if folder else {"mimeType": "text/plain"}
            ),
        }

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        calls.append((request.get_method(), url))  # type: ignore[attr-defined]
        if "/items/root-folder?" in url:
            return _Response(
                json.dumps(
                    item(
                        "root-folder",
                        "[상상] 업무분류",
                        parent_id="desktop",
                        folder=True,
                    ),
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        if "/items/root-folder/children" in url:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            item("file-b", "B.txt", parent_id="root-folder", folder=False),
                            item("folder-a", "9. 법령", parent_id="root-folder", folder=True),
                            {
                                **item(
                                    "remote-link",
                                    "공유 링크",
                                    parent_id="root-folder",
                                    folder=True,
                                ),
                                "remoteItem": {"id": "outside"},
                            },
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        if "/items/folder-a/children" in url:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            item("file-a", "A.txt", parent_id="folder-a", folder=False)
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        raise AssertionError(url)

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    entries = client.walk_folder("drive-1", "root-folder")
    assert [entry.relative_path for entry in entries] == [
        "9. 법령",
        "9. 법령/A.txt",
        "B.txt",
    ]
    assert all(entry.drive_id == "drive-1" for entry in entries)
    assert {method for method, _ in calls} == {"GET"}
    assert not any("remote-link/children" in url for _, url in calls)


def _check_onedrive_folder_walk_traverses_only_selected_top_roots() -> None:
    calls: list[str] = []
    included_name = "Re\u0301gles"

    def item(item_id: str, name: str, *, folder: bool) -> dict[str, object]:
        return {
            "id": item_id,
            "name": name,
            "lastModifiedDateTime": "2026-07-21T01:02:03Z",
            "size": 0 if folder else 10,
            "eTag": f"{item_id}-v1",
            "parentReference": {
                "driveId": "drive-1",
                "id": "root-folder",
                "path": "/drive/root:/Wiki",
            },
            "folder" if folder else "file": (
                {"childCount": 1} if folder else {"mimeType": "text/plain"}
            ),
        }

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        calls.append(url)
        if "/items/root-folder?" in url:
            return _Response(json.dumps(item("root-folder", "Wiki", folder=True)).encode())
        if "/items/root-folder/children" in url:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            item("included", included_name, folder=True),
                            item("excluded", "Archive", folder=True),
                        ]
                    }
                ).encode()
            )
        if "/items/included/children" in url:
            return _Response(
                json.dumps(
                    {"value": [item("rule", "rule.txt", folder=False)]}
                ).encode()
            )
        if "/items/excluded/children" in url:
            raise AssertionError("excluded root must not be traversed")
        raise AssertionError(url)

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    entries = client.walk_folder(
        "drive-1",
        "root-folder",
        include_root_names=("RÉGLES",),
    )
    assert [entry.item_id for entry in entries] == ["included", "rule"]
    assert not any("/items/excluded/children" in url for url in calls)

    with unittest.TestCase().assertRaisesRegex(OneDriveError, "ambiguous"):
        client.walk_folder(
            "drive-1",
            "root-folder",
            include_root_names=("Rules", "rules"),
        )


def _check_onedrive_folder_walk_rejects_ambiguous_drive_root_names() -> None:
    def item(item_id: str, name: str) -> dict[str, object]:
        return {
            "id": item_id,
            "name": name,
            "lastModifiedDateTime": "2026-07-21T01:02:03Z",
            "parentReference": {
                "driveId": "drive-1",
                "id": "root-folder",
                "path": "/drive/root:/Wiki",
            },
            "folder": {"childCount": 0},
        }

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        if "/items/root-folder?" in url:
            return _Response(json.dumps(item("root-folder", "Wiki")).encode())
        if "/items/root-folder/children" in url:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            item("one", "RÉGLES"),
                            item("two", "Re\u0301gles"),
                        ]
                    }
                ).encode()
            )
        raise AssertionError(url)

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    with unittest.TestCase().assertRaisesRegex(OneDriveError, "ambiguous"):
        client.walk_folder(
            "drive-1",
            "root-folder",
            include_root_names=("RÉGLES",),
        )


def _check_onedrive_exact_path_is_url_encoded() -> None:
    seen: list[str] = []

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        seen.append(request.full_url)  # type: ignore[attr-defined]
        return _Response(
            json.dumps(
                {
                    "id": "folder",
                    "name": "[상상] 업무분류",
                    "lastModifiedDateTime": "2026-07-20T01:02:03Z",
                    "parentReference": {"driveId": "drive-1", "id": "parent"},
                    "folder": {"childCount": 10},
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    entry = client.get_item_by_path(
        "drive-1", "Desktop/ONEDRIVE/Desktop/[상상] 업무분류"
    )
    assert entry.is_folder
    assert "%5B%EC%83%81%EC%83%81%5D" in seen[0]
    assert {request_method for request_method, _ in [("GET", seen[0])]} == {"GET"}


def _check_onedrive_folder_delta_is_scoped_and_incremental() -> None:
    calls: list[str] = []
    next_link = (
        "https://graph.test/v1.0/drives/drive-1/items/root-folder/delta?token=next"
    )
    delta_link = (
        "https://graph.test/v1.0/drives/drive-1/items/root-folder/delta?token=current"
    )

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        calls.append(url)
        if "/items/root-folder?" in url:
            return _Response(
                json.dumps(
                    {
                        "id": "root-folder",
                        "name": "[상상] 업무분류",
                        "lastModifiedDateTime": "2026-07-20T01:02:03Z",
                        "parentReference": {
                            "driveId": "drive-1",
                            "id": "desktop",
                            "path": "/drive/root:/Desktop",
                        },
                        "folder": {"childCount": 2},
                        "cTag": "root-ctag",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        if "token=next" in url:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            {
                                "id": "old-file",
                                "deleted": {},
                            },
                            {
                                "id": "file-2",
                                "name": "지침.txt",
                                "lastModifiedDateTime": "2026-07-20T01:02:03Z",
                                "parentReference": {
                                    "driveId": "drive-1",
                                    "id": "folder-1",
                                    "path": (
                                        "/drive/root:/Desktop/[상상] 업무분류/9. 법령"
                                    ),
                                },
                                "file": {"mimeType": "text/plain"},
                                "eTag": "file-2-v1",
                            },
                        ],
                        "@odata.deltaLink": delta_link,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        if "/delta?" in url:
            return _Response(
                json.dumps(
                    {
                        "value": [
                            {
                                "id": "root-folder",
                                "name": "[상상] 업무분류",
                            },
                            {
                                "id": "folder-1",
                                "name": "9. 법령",
                                "lastModifiedDateTime": "2026-07-20T01:02:03Z",
                                "parentReference": {
                                    "driveId": "drive-1",
                                    "id": "root-folder",
                                    "path": "/drive/root:/Desktop/[상상] 업무분류",
                                },
                                "folder": {"childCount": 1},
                                "cTag": "folder-1-v1",
                            },
                        ],
                        "@odata.nextLink": next_link,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        raise AssertionError(url)

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    result = client.read_folder_delta("drive-1", "root-folder")
    assert [entry.relative_path for entry in result.entries] == [
        "9. 법령",
        "9. 법령/지침.txt",
    ]
    assert result.deleted_item_ids == ("old-file",)
    assert result.delta_link == delta_link
    assert all("/drives/drive-1/items/root-folder" in url for url in calls)

    with unittest.TestCase().assertRaisesRegex(ValueError, "positive"):
        client.read_folder_delta("drive-1", "root-folder", max_items=0)


def _check_onedrive_rejects_delta_link_for_another_root() -> None:
    client = OneDriveReadOnlyClient("token", base_url="https://graph.test/v1.0")
    client._validate_folder_delta_url(
        "https://graph.test/v1.0/drives/drive-1/items/root-folder/"
        "delta(token='opaque-continuation')",
        drive_id="drive-1",
        root_item_id="root-folder",
    )
    client._validate_folder_delta_url(
        "https://graph.test/v1.0/drives/drive%21one/items/root%21folder/"
        "delta(token='opaque-continuation')",
        drive_id="drive!one",
        root_item_id="root!folder",
    )
    with unittest.TestCase().assertRaisesRegex(
        Exception, "pinned root"
    ):
        client._validate_folder_delta_url(
            "https://graph.test/v1.0/drives/drive-1/items/other/delta?token=x",
            drive_id="drive-1",
            root_item_id="root-folder",
        )
    with unittest.TestCase().assertRaisesRegex(Exception, "pinned root"):
        client._validate_folder_delta_url(
            "https://graph.test/v1.0/drives/drive-1/items/root-folder%2Fescape/"
            "delta(token='opaque-continuation')",
            drive_id="drive-1",
            root_item_id="root-folder",
        )
    for changed_case in (
        "https://graph.test/v1.0/drives/DRIVE-1/items/root-folder/delta?token=x",
        "https://graph.test/v1.0/drives/drive-1/items/ROOT-FOLDER/delta?token=x",
    ):
        with unittest.TestCase().assertRaisesRegex(Exception, "pinned root"):
            client._validate_folder_delta_url(
                changed_case,
                drive_id="drive-1",
                root_item_id="root-folder",
            )


def _check_onedrive_latest_folder_checkpoint_uses_exact_get_route() -> None:
    calls: list[tuple[str, str]] = []
    delta_link = (
        "https://graph.test/v1.0/drives/drive%21one/items/root%21folder/"
        "delta?token=checkpoint"
    )

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        calls.append((request.get_method(), url))  # type: ignore[attr-defined]
        if "/items/root%21folder?" in url:
            return _Response(
                json.dumps(
                    {
                        "id": "root!folder",
                        "name": "wiki-root",
                        "lastModifiedDateTime": "2026-07-21T01:02:03Z",
                        "parentReference": {
                            "driveId": "drive!one",
                            "id": "desktop",
                            "path": "/drive/root:/Desktop",
                        },
                        "folder": {"childCount": 2},
                        "cTag": "root-v1",
                    }
                ).encode()
            )
        if url.endswith("/items/root%21folder/delta?token=latest"):
            return _Response(
                json.dumps(
                    {"value": [], "@odata.deltaLink": delta_link}
                ).encode()
            )
        raise AssertionError(url)

    client = OneDriveReadOnlyClient(
        "token", base_url="https://graph.test/v1.0", opener=opener
    )
    checkpoint = client.get_folder_delta_checkpoint("drive!one", "root!folder")

    assert checkpoint.root.item_id == "root!folder"
    assert checkpoint.entries == ()
    assert checkpoint.deleted_item_ids == ()
    assert checkpoint.delta_link == delta_link
    assert any(url.endswith("/delta?token=latest") for _, url in calls)
    assert {method for method, _ in calls} == {"GET"}


def _check_onedrive_children_next_link_is_exactly_folder_bound() -> None:
    client = OneDriveReadOnlyClient("token", base_url="https://graph.test/v1.0")
    for accepted in (
        "https://graph.test/v1.0/drives/drive!one/items/root!folder/children?$skip=1",
        "https://graph.test/v1.0/drives/drive%21one/items/root%21folder/children?$skip=1",
    ):
        client._validate_children_url(
            accepted,
            drive_id="drive!one",
            folder_item_id="root!folder",
        )

    rejected = (
        "https://graph.test/v1.0/drives/drive!one/items/other/children?$skip=1",
        "https://graph.test/v1.0/drives/other/items/root!folder/children?$skip=1",
        "https://graph.test/v1.0/drives/drive!one/items/root!folder/versions?$skip=1",
        "https://graph.test/v1.0/drives/drive!one/items/root!folder/children/extra",
        "https://graph.test/v1.0/drives/drive!one/items/root%2Fescape/children",
        "https://graph.test/v1.0/drives/drive!one/items/root%5Cescape/children",
        "https://graph.test/v1.0/drives/DRIVE!ONE/items/root!folder/children",
        "https://graph.test/v1.0/drives/drive!one/items/ROOT!FOLDER/children",
    )
    for url in rejected:
        with unittest.TestCase().assertRaisesRegex(OneDriveError, "pinned folder"):
            client._validate_children_url(
                url,
                drive_id="drive!one",
                folder_item_id="root!folder",
            )


def _check_ambiguous_initial_baseline_fails_closed() -> None:
    timestamp = datetime(2026, 7, 15, 8, tzinfo=UTC)
    versions = [
        DriveVersion("1.0", timestamp),
        DriveVersion("1.1", timestamp),
    ]
    with unittest.TestCase().assertRaises(BaselineVersionNotFound):
        select_baseline_version(versions, timestamp)


def _check_version_at_cutoff_is_included_in_first_delta() -> None:
    cutoff = datetime(2026, 7, 15, 15, tzinfo=UTC)
    versions = [
        DriveVersion("before", cutoff - timedelta(seconds=1)),
        DriveVersion("at-cutoff", cutoff),
    ]
    assert select_baseline_version(versions, cutoff).id == "before"


def _receipt(now: datetime, *, expires_in: int = 10) -> ApprovalReceipt:
    return ApprovalReceipt(
        proposal_id="proposal-1",
        revision=1,
        proposal_digest="digest",
        source_version_id="2.0",
        source_file_hash="file-hash",
        telegram_user_id="user-1",
        chat_id="chat-1",
        issued_at=now,
        expires_at=now + timedelta(minutes=expires_in),
        nonce="nonce",
        signature="signature",
    )


def _check_notion_gateway_requires_approval_for_every_upsert() -> None:
    now = datetime(2026, 7, 16, 8, tzinfo=UTC)
    gateway = InMemoryNotionGateway(
        lambda receipt, operation_id, *_: receipt.proposal_id == "proposal-1"
        and operation_id == "operation-1",
        now=lambda: now,
    )
    match = NotionMatch("사건번호", "title")

    with unittest.TestCase().assertRaises(ApprovalRequiredError):
        gateway.upsert_page(
            "cases",
            match,
            "S-1",
            {"상태": {"select": {"name": "진행"}}},
            operation_id="not-approved",
            approval=_receipt(now),
        )
    with unittest.TestCase().assertRaises(ApprovalRequiredError):
        gateway.upsert_page(
            "cases",
            match,
            "S-1",
            {},
            operation_id="operation-1",
            approval=_receipt(now - timedelta(minutes=20), expires_in=10),
        )

    created = gateway.upsert_page(
        "cases",
        match,
        "S-1",
        {
            "사건번호": {"title": [{"text": {"content": "S-1"}}]},
            "상태": {"select": {"name": "진행"}},
        },
        operation_id="operation-1",
        approval=_receipt(now),
    )
    updated = gateway.upsert_page(
        "cases",
        match,
        "S-1",
        {"상태": {"select": {"name": "완료"}}},
        operation_id="operation-1",
        approval=_receipt(now),
    )
    assert created.created is True
    assert updated.created is False
    assert created.page_id == updated.page_id


def _check_notion_gateway_rejects_stale_write_intent() -> None:
    now = datetime(2026, 7, 16, 8, tzinfo=UTC)
    gateway = InMemoryNotionGateway(
        lambda *_: True,
        now=lambda: now,
        initial_pages={
            "cases": [
                {
                    "id": "page-1",
                    "properties": {
                        "사건번호": {
                            "title": [{"text": {"content": "S-1"}}]
                        },
                        "상태": {"select": {"name": "접수"}},
                    },
                }
            ]
        },
    )
    match = NotionMatch("사건번호", "title")
    intent = NotionPrecondition(
        page_id="page-1",
        last_edited_time=None,
        property_name="상태",
        property_value={"select": {"name": "접수"}},
    )

    gateway.upsert_page(
        "cases",
        match,
        "S-1",
        {"상태": {"select": {"name": "외부변경"}}},
        operation_id="external-operation",
        approval=_receipt(now),
    )
    with unittest.TestCase().assertRaises(NotionError) as caught:
        gateway.upsert_page(
            "cases",
            match,
            "S-1",
            {"상태": {"select": {"name": "완료"}}},
            operation_id="approved-operation",
            approval=_receipt(now),
            precondition=intent,
        )
    assert caught.exception.status == 409


def _check_http_notion_gateway_uses_latest_data_source_api() -> None:
    now = datetime(2026, 7, 16, 8, tzinfo=UTC)
    calls: list[tuple[str, str, dict[str, object], str]] = []

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        method = request.get_method()  # type: ignore[attr-defined]
        url = request.full_url  # type: ignore[attr-defined]
        data = request.data  # type: ignore[attr-defined]
        body = json.loads(data.decode()) if data else {}
        version = request.headers["Notion-version"]  # type: ignore[attr-defined]
        calls.append((method, url, body, version))
        if url.endswith("/data_sources/cases/query"):
            return _Response(json.dumps({"results": [], "has_more": False}).encode())
        if url.endswith("/pages"):
            return _Response(json.dumps({"id": "page-1"}).encode())
        raise AssertionError(url)

    gateway = HttpNotionGateway(
        "token",
        lambda _receipt, operation_id, *_: operation_id == "operation-1",
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: now,
    )
    result = gateway.upsert_page(
        "cases",
        NotionMatch("사건번호", "title"),
        "S-1",
        {"사건번호": {"title": [{"text": {"content": "S-1"}}]}},
        operation_id="operation-1",
        approval=_receipt(now),
    )
    assert result.created is True
    assert [call[0] for call in calls] == ["POST", "POST"]
    assert calls[0][1].endswith("/data_sources/cases/query")
    assert calls[1][2]["parent"] == {
        "type": "data_source_id",
        "data_source_id": "cases",
    }
    assert {call[3] for call in calls} == {"2026-03-11"}


def _check_telegram_formatter_renders_edit_actions_with_compact_callbacks() -> None:
    source = SourceRef("d", "i", "2.0", "hash", "2026", 3, "C3", "완료")
    change = ProposedChange(
        target_database="한국 특허 사건",
        entity_key="S-1",
        property_name="현재상태",
        kind=ChangeKind.UPDATE,
        current_value="진행",
        proposed_value="완료",
        analyzer="status_analyzer",
        analyzer_version="1.0.0",
        confidence=0.98,
        reason="Excel 상태 열이 변경됨",
        source_refs=[source],
        operation_id="operation-1",
    )
    revision = ProposalRevision(
        proposal_id="proposal-with-a-very-long-identifier-that-stays-server-side",
        revision=3,
        source_version_id="2.0",
        source_file_hash="hash",
        requested_by="user-1",
        chat_id="chat-1",
        operations=[ProposalOperation(change)],
        status=ProposalStatus.PENDING_APPROVAL,
    )
    formatter = TelegramProposalFormatter()

    summary = formatter.render_summary(revision)
    detail = formatter.render_operation(revision, "operation-1")
    assert "최종 승인" in summary.text
    assert "값 수정" in [button.text for row in detail.keyboard for button in row]
    assert all(
        len(button.callback_data.encode("utf-8")) <= 64
        for message in (summary, detail)
        for row in message.keyboard
        for button in row
    )
    assert (
        f"/nx_approve {revision.proposal_id} 3 {revision.digest}"
        in summary.text
    )
    assert all(
        button.text != "최종 승인"
        for row in summary.keyboard
        for button in row
    )


class AdapterTests(unittest.TestCase):
    def test_excel_reader_builds_provenance_and_stable_diff(self) -> None:
        _check_excel_reader_builds_provenance_and_stable_diff()

    def test_repeated_case_tasks_use_composite_occurrence_keys(self) -> None:
        _check_repeated_case_tasks_use_composite_occurrence_keys()

    def test_occurrence_tradeoff_and_identical_rows_fail_closed(self) -> None:
        _check_occurrence_tradeoff_and_identical_rows_fail_closed()

    def test_registration_sheet_key_fallbacks(self) -> None:
        _check_registration_sheet_key_fallbacks()

    def test_onedrive_client_is_read_only_and_selects_cutoff(self) -> None:
        _check_onedrive_client_pages_versions_selects_cutoff_and_only_uses_get()

    def test_onedrive_response_size_is_bounded(self) -> None:
        _check_onedrive_response_size_is_bounded_before_json_decode()

    def test_onedrive_search_returns_read_only_identifiers(self) -> None:
        _check_onedrive_search_returns_exact_read_only_identifiers()

    def test_onedrive_delta_enumerates_current_files_with_get_only(self) -> None:
        _check_onedrive_delta_enumerates_current_files_with_get_only()

    def test_onedrive_folder_walk_is_root_bound_and_stable(self) -> None:
        _check_onedrive_folder_walk_is_root_bound_and_stable()

    def test_onedrive_folder_walk_traverses_only_selected_top_roots(self) -> None:
        _check_onedrive_folder_walk_traverses_only_selected_top_roots()

    def test_onedrive_folder_walk_rejects_ambiguous_drive_root_names(self) -> None:
        _check_onedrive_folder_walk_rejects_ambiguous_drive_root_names()

    def test_onedrive_exact_path_is_url_encoded(self) -> None:
        _check_onedrive_exact_path_is_url_encoded()

    def test_onedrive_folder_delta_is_scoped_and_incremental(self) -> None:
        _check_onedrive_folder_delta_is_scoped_and_incremental()

    def test_onedrive_rejects_delta_link_for_another_root(self) -> None:
        _check_onedrive_rejects_delta_link_for_another_root()

    def test_onedrive_latest_folder_checkpoint_uses_exact_get_route(self) -> None:
        _check_onedrive_latest_folder_checkpoint_uses_exact_get_route()

    def test_onedrive_children_next_link_is_exactly_folder_bound(self) -> None:
        _check_onedrive_children_next_link_is_exactly_folder_bound()

    def test_ambiguous_initial_baseline_fails_closed(self) -> None:
        _check_ambiguous_initial_baseline_fails_closed()

    def test_version_at_cutoff_is_included_in_first_delta(self) -> None:
        _check_version_at_cutoff_is_included_in_first_delta()

    def test_notion_gateway_requires_approval(self) -> None:
        _check_notion_gateway_requires_approval_for_every_upsert()

    def test_notion_gateway_rejects_stale_write_intent(self) -> None:
        _check_notion_gateway_rejects_stale_write_intent()

    def test_http_notion_gateway_uses_latest_data_source_api(self) -> None:
        _check_http_notion_gateway_uses_latest_data_source_api()

    def test_telegram_formatter_renders_edit_actions(self) -> None:
        _check_telegram_formatter_renders_edit_actions_with_compact_callbacks()


if __name__ == "__main__":
    unittest.main()
