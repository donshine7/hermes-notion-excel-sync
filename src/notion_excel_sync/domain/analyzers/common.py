from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from notion_excel_sync.models import JsonValue, SourceRecord


def normalize_header(value: object) -> str:
    return re.sub(r"[\s_\-·./()\[\]]+", "", str(value).strip().lower())


def searchable_text(record: SourceRecord) -> str:
    parts = [record.sheet, *record.values.keys()]
    parts.extend(str(value) for value in record.values.values() if value not in (None, ""))
    return normalize_header(" ".join(parts))


def find_entry(record: SourceRecord, aliases: Iterable[str]) -> tuple[str | None, JsonValue]:
    normalized_aliases = [normalize_header(alias) for alias in aliases]
    exact = {normalize_header(header): header for header in record.values}
    for alias in normalized_aliases:
        if alias in exact:
            header = exact[alias]
            return header, record.values.get(header)
    for header in record.values:
        normalized_header = normalize_header(header)
        if any(
            alias in normalized_header or normalized_header in alias
            for alias in normalized_aliases
        ):
            return header, record.values.get(header)
    return None, None


def find_value(record: SourceRecord, aliases: Iterable[str]) -> JsonValue:
    return find_entry(record, aliases)[1]


def find_before(record: SourceRecord | None, aliases: Iterable[str]) -> JsonValue:
    return find_value(record, aliases) if record else None


def non_empty(value: JsonValue) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def split_values(value: JsonValue) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_values = [str(item) for item in value]
    else:
        raw_values = re.split(r"\s*(?:,|;|\n|\r|\|)\s*", str(value))
    return list(dict.fromkeys(item.strip() for item in raw_values if item.strip()))


def split_case_numbers(value: JsonValue) -> list[str]:
    values: list[str] = []
    for item in split_values(value):
        values.extend(part.strip() for part in re.split(r"\s+/\s+", item) if part.strip())
    return list(dict.fromkeys(values))


def parse_date(value: JsonValue) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for pattern in (
        r"^(\d{4})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})(?:일)?$",
        r"^(\d{2})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})$",
    ):
        match = re.match(pattern, text)
        if match:
            year, month, day = (int(part) for part in match.groups())
            if year < 100:
                year += 2000
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return None
    return None


def parse_money(value: JsonValue) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    text = str(value).strip().replace(",", "").replace(" ", "")
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    multiplier = Decimal(1)
    if "억원" in text or text.endswith("억"):
        multiplier = Decimal(100_000_000)
    elif "만원" in text or text.endswith("만"):
        multiplier = Decimal(10_000)
    elif "천원" in text or text.endswith("천"):
        multiplier = Decimal(1_000)
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return int(Decimal(match.group()) * multiplier) * sign
    except InvalidOperation:
        return None


def parse_count(value: JsonValue) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        return None
    match = re.search(r"(\d+)\s*건", str(value)) or re.search(r"^\s*(\d+)\s*$", str(value))
    return int(match.group(1)) if match else None


def record_case_numbers(record: SourceRecord) -> list[str]:
    value = find_value(record, ("당소 사건번호", "사건번호", "관리번호", "케이스번호"))
    values = split_case_numbers(value)
    return values or ([record.key] if record.key else [])


def primary_case_key(record: SourceRecord) -> str:
    cases = record_case_numbers(record)
    return cases[0] if cases else record.key


def normalized_text(value: JsonValue) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
