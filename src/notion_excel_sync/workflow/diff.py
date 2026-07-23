from __future__ import annotations

from notion_excel_sync.models import ChangeKind, RecordChange, SourceRecord, WorkbookSnapshot


class DuplicateSourceKeyError(ValueError):
    pass


def _index(records: list[SourceRecord]) -> dict[str, SourceRecord]:
    result: dict[str, SourceRecord] = {}
    for record in records:
        compound_key = f"{record.sheet}::{record.key}"
        if compound_key in result:
            raise DuplicateSourceKeyError(f"Duplicate source key: {compound_key}")
        result[compound_key] = record
    return result


def diff_snapshots(
    before: WorkbookSnapshot, after: WorkbookSnapshot
) -> list[RecordChange]:
    """Return record-level changes without treating row reordering as a modification."""

    old = _index(before.records)
    new = _index(after.records)
    changes: list[RecordChange] = []
    for compound_key in sorted(old.keys() | new.keys()):
        previous = old.get(compound_key)
        current = new.get(compound_key)
        source_key = (current or previous).key  # type: ignore[union-attr]
        if previous is None:
            changes.append(RecordChange(ChangeKind.CREATE, source_key, None, current))
        elif current is None:
            changes.append(
                RecordChange(ChangeKind.DELETE_CANDIDATE, source_key, previous, None)
            )
        elif previous.content_hash != current.content_hash:
            changes.append(RecordChange(ChangeKind.UPDATE, source_key, previous, current))
    return changes

