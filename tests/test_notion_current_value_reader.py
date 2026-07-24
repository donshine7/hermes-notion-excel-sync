from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from notion_excel_sync.adapters.notion import NotionMatch
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.workflow.notion_writer import (
    DatabaseDefinition,
    NotionCurrentValueReader,
    NotionSchemaError,
    decode_property,
)


DEFINITIONS = {
    "Cases": DatabaseDefinition(
        "Name",
        {
            "Name": "title",
            "Status": "select",
            "Note": "rich_text",
        },
    ),
    "Tasks": DatabaseDefinition(
        "Name",
        {
            "Name": "title",
            "Status": "select",
            "Note": "rich_text",
        },
    ),
}
DATA_SOURCES = {
    "Cases": "cases-source",
    "Tasks": "tasks-source",
}


def _page(
    page_id: str,
    data_source_id: str,
    title: str,
    *,
    status: str = "Open",
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": page_id,
        "parent": {
            "type": "data_source_id",
            "data_source_id": data_source_id,
        },
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": title}],
            },
            "Status": {
                "type": "select",
                "select": {"name": status},
            },
            "Note": {
                "type": "rich_text",
                "rich_text": [{"plain_text": note}],
            },
        },
    }


class CountingReadGateway:
    def __init__(
        self,
        pages: Mapping[str, list[Mapping[str, Any]]],
        *,
        uses_data_sources: bool = True,
    ) -> None:
        self.pages = {
            data_source_id: list(items)
            for data_source_id, items in pages.items()
        }
        self.uses_data_sources = uses_data_sources
        self.data_source_queries: list[str] = []
        self.database_queries: list[str] = []
        self.find_calls: list[tuple[str, str]] = []
        self.get_calls: list[str] = []

    def get_page(self, page_id: str) -> Mapping[str, Any]:
        self.get_calls.append(page_id)
        for pages in self.pages.values():
            for page in pages:
                if page.get("id") == page_id:
                    return page
        raise KeyError(page_id)

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        assert filter_body is None
        assert page_size == 100
        self.data_source_queries.append(data_source_id)
        return list(self.pages.get(data_source_id, []))

    def query_database(
        self,
        database_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        assert filter_body is None
        assert page_size == 100
        self.database_queries.append(database_id)
        return list(self.pages.get(database_id, []))

    def find_page(
        self,
        data_source_id: str,
        match: NotionMatch,
        entity_key: object,
    ) -> Mapping[str, Any] | None:
        assert match == NotionMatch("Name", "title")
        expected = str(entity_key)
        self.find_calls.append((data_source_id, expected))
        matches = [
            page
            for page in self.pages.get(data_source_id, [])
            if decode_property(
                page.get("properties", {}).get("Name"),
                "title",
            )
            == expected
        ]
        if len(matches) > 1:
            raise RuntimeError("ambiguous synthetic title")
        return matches[0] if matches else None


def test_large_unmapped_groups_query_once_per_database_and_reuse_cache(
    tmp_path,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    database.save_entity_mapping(
        "Cases",
        "mapped-case",
        "mapped-page",
        "Mapped Case",
    )
    case_pages = [
        _page(
            f"case-page-{index}",
            "cases-source",
            f"CASE-{index:04d}",
            status=f"Case status {index}",
            note=f"Case note {index}",
        )
        for index in range(180)
    ]
    case_pages.append(
        _page(
            "mapped-page",
            "cases-source",
            "Mapped Case",
            status="Mapped status",
        )
    )
    task_pages = [
        _page(
            f"task-page-{index}",
            "tasks-source",
            f"TASK-{index:04d}",
            status=f"Task status {index}",
        )
        for index in range(140)
    ]
    gateway = CountingReadGateway(
        {
            "cases-source": case_pages,
            "tasks-source": task_pages,
        }
    )
    reader = NotionCurrentValueReader(
        gateway,
        DATA_SOURCES,
        DEFINITIONS,
        database,
        bulk_read_threshold=10,
    )
    keys = [
        ("Cases", f"case-{index}", "Status")
        for index in range(180)
    ]
    keys.extend(
        ("Tasks", f"task-{index}", "Status")
        for index in range(140)
    )
    keys.extend(
        [
            ("Cases", "missing-case", "Status"),
            ("Cases", "mapped-case", "Status"),
        ]
    )
    titles = {
        ("Cases", f"case-{index}"): f"CASE-{index:04d}"
        for index in range(180)
    }
    titles.update(
        {
            ("Tasks", f"task-{index}"): f"TASK-{index:04d}"
            for index in range(140)
        }
    )
    titles[("Cases", "missing-case")] = "CASE-MISSING"

    result = reader.load(keys, titles)

    assert result[("Cases", "case-179", "Status")] == "Case status 179"
    assert result[("Tasks", "task-139", "Status")] == "Task status 139"
    assert result[("Cases", "missing-case", "Status")] is None
    assert result[("Cases", "mapped-case", "Status")] == "Mapped status"
    assert gateway.data_source_queries == ["cases-source", "tasks-source"]
    assert gateway.database_queries == []
    assert gateway.find_calls == []
    assert gateway.get_calls == ["mapped-page"]

    cached = reader.load(
        [
            ("Cases", "case-179", "Note"),
            ("Cases", "mapped-case", "Status"),
            ("Tasks", "task-139", "Status"),
        ],
        titles,
    )

    assert cached[("Cases", "case-179", "Note")] == "Case note 179"
    assert gateway.data_source_queries == ["cases-source", "tasks-source"]
    assert gateway.get_calls == ["mapped-page"]


def test_large_legacy_group_uses_database_query() -> None:
    pages = [
        _page(f"page-{index}", "legacy-db", f"ITEM-{index}")
        for index in range(25)
    ]
    gateway = CountingReadGateway(
        {"legacy-db": pages},
        uses_data_sources=False,
    )
    reader = NotionCurrentValueReader(
        gateway,
        {"Cases": "legacy-db"},
        {"Cases": DEFINITIONS["Cases"]},
        bulk_read_threshold=5,
    )

    result = reader.load(
        [
            ("Cases", f"entity-{index}", "Status")
            for index in range(25)
        ],
        {
            ("Cases", f"entity-{index}"): f"ITEM-{index}"
            for index in range(25)
        },
    )

    assert len(result) == 25
    assert gateway.database_queries == ["legacy-db"]
    assert gateway.data_source_queries == []


def test_small_unmapped_group_keeps_filtered_lookup() -> None:
    gateway = CountingReadGateway(
        {
            "cases-source": [
                _page(f"page-{index}", "cases-source", f"CASE-{index}")
                for index in range(3)
            ]
        }
    )
    reader = NotionCurrentValueReader(
        gateway,
        {"Cases": "cases-source"},
        {"Cases": DEFINITIONS["Cases"]},
        bulk_read_threshold=5,
    )

    reader.load(
        [
            ("Cases", f"entity-{index}", "Status")
            for index in range(3)
        ],
        {
            ("Cases", f"entity-{index}"): f"CASE-{index}"
            for index in range(3)
        },
    )

    assert len(gateway.find_calls) == 3
    assert gateway.data_source_queries == []


def test_group_threshold_uses_whole_load_after_small_cached_phase() -> None:
    gateway = CountingReadGateway(
        {
            "cases-source": [
                _page(f"page-{index}", "cases-source", f"CASE-{index}")
                for index in range(6)
            ]
        }
    )
    reader = NotionCurrentValueReader(
        gateway,
        {"Cases": "cases-source"},
        {"Cases": DEFINITIONS["Cases"]},
        bulk_read_threshold=5,
    )
    titles = {
        ("Cases", f"entity-{index}"): f"CASE-{index}"
        for index in range(6)
    }
    reader.load(
        [
            ("Cases", f"entity-{index}", "Status")
            for index in range(4)
        ],
        titles,
    )
    assert len(gateway.find_calls) == 4

    reader.load(
        [
            ("Cases", f"entity-{index}", "Status")
            for index in range(6)
        ],
        titles,
    )

    assert len(gateway.find_calls) == 4
    assert gateway.data_source_queries == ["cases-source"]


def test_bulk_read_fails_closed_on_duplicate_remote_title() -> None:
    gateway = CountingReadGateway(
        {
            "cases-source": [
                _page("page-a", "cases-source", "CASE-DUPLICATE"),
                _page("page-b", "cases-source", "CASE-DUPLICATE"),
            ]
        }
    )
    reader = NotionCurrentValueReader(
        gateway,
        {"Cases": "cases-source"},
        {"Cases": DEFINITIONS["Cases"]},
        bulk_read_threshold=1,
    )

    with pytest.raises(
        NotionSchemaError,
        match="Multiple Notion pages",
    ):
        reader.load(
            [("Cases", "entity", "Status")],
            {("Cases", "entity"): "CASE-DUPLICATE"},
        )


def test_bulk_read_fails_closed_on_duplicate_requested_title() -> None:
    gateway = CountingReadGateway({"cases-source": []})
    reader = NotionCurrentValueReader(
        gateway,
        {"Cases": "cases-source"},
        {"Cases": DEFINITIONS["Cases"]},
        bulk_read_threshold=1,
    )

    with pytest.raises(
        NotionSchemaError,
        match="requested entities",
    ):
        reader.load(
            [
                ("Cases", "entity-a", "Status"),
                ("Cases", "entity-b", "Status"),
            ],
            {
                ("Cases", "entity-a"): "SAME-TITLE",
                ("Cases", "entity-b"): "SAME-TITLE",
            },
        )
    assert gateway.data_source_queries == []


def test_duplicate_unmapped_title_across_load_phases_fails_closed() -> None:
    gateway = CountingReadGateway(
        {
            "cases-source": [
                _page("page-a", "cases-source", "SHARED-TITLE")
            ]
        }
    )
    reader = NotionCurrentValueReader(
        gateway,
        {"Cases": "cases-source"},
        {"Cases": DEFINITIONS["Cases"]},
        bulk_read_threshold=1,
    )
    reader.load(
        [("Cases", "entity-a", "Status")],
        {("Cases", "entity-a"): "SHARED-TITLE"},
    )

    with pytest.raises(
        NotionSchemaError,
        match="requested entities",
    ):
        reader.load(
            [("Cases", "entity-b", "Status")],
            {("Cases", "entity-b"): "SHARED-TITLE"},
        )

    reader.clear_cache()
    result = reader.load(
        [("Cases", "entity-b", "Status")],
        {("Cases", "entity-b"): "SHARED-TITLE"},
    )
    assert result[("Cases", "entity-b", "Status")] == "Open"
    assert gateway.data_source_queries == [
        "cases-source",
        "cases-source",
    ]


def test_mapped_page_still_requires_exact_binding(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    database.save_entity_mapping(
        "Cases",
        "mapped-case",
        "mapped-page",
        "Expected title",
    )
    gateway = CountingReadGateway(
        {
            "cases-source": [
                _page(
                    "mapped-page",
                    "cases-source",
                    "Changed title",
                )
            ]
        }
    )
    reader = NotionCurrentValueReader(
        gateway,
        {"Cases": "cases-source"},
        {"Cases": DEFINITIONS["Cases"]},
        database,
        bulk_read_threshold=1,
    )

    with pytest.raises(
        NotionSchemaError,
        match="title no longer matches",
    ):
        reader.load(
            [("Cases", "mapped-case", "Status")],
            {},
        )
    assert gateway.get_calls == ["mapped-page"]
    assert gateway.data_source_queries == []
