from __future__ import annotations

import re

from notion_excel_sync.config import AppConfig
from notion_excel_sync.persistence.database import StateDatabase


_NOTION_ID_RE = re.compile(
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)


class DataSourceBindingError(RuntimeError):
    """A hardened config and the verified local installation disagree."""


def _is_placeholder(value: str) -> bool:
    return not value.strip() or value.strip().upper().startswith("REPLACE_")


def _canonical_notion_id(value: str) -> str:
    normalized = value.strip()
    if _NOTION_ID_RE.fullmatch(normalized) is None:
        raise DataSourceBindingError("A configured Notion data-source ID is invalid")
    return normalized.replace("-", "").lower()


def configured_data_sources(
    config: AppConfig,
    database: StateDatabase | None = None,
) -> dict[str, str]:
    """Merge static config with approval-verified schema installations.

    The schema workflow never edits the ACL-hardened config file.  Its verified
    logical-name to data-source binding is stored in the state database and is
    overlaid at runtime.  Any disagreement with a non-placeholder config value
    fails closed instead of silently choosing one source.
    """

    configured = {
        str(name): str(value).strip()
        for name, value in config.notion.databases.items()
        if not _is_placeholder(str(value))
    }
    if database is None:
        return configured

    for logical_name, installed_id in database.schema_installations().items():
        installed = _canonical_notion_id(installed_id)
        current = configured.get(logical_name)
        if current is not None and _canonical_notion_id(current) != installed:
            raise DataSourceBindingError(
                "Configured and verified Notion data-source bindings conflict"
            )
        configured[logical_name] = installed
    return configured


__all__ = ["DataSourceBindingError", "configured_data_sources"]
