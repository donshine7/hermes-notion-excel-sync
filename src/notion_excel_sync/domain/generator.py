from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from notion_excel_sync.domain.analyzer import AnalyzerManifest
from notion_excel_sync.domain.profiler import ColumnProfile, SourceCatalog
from notion_excel_sync.models import AnalyzerLifecycle


@dataclass(frozen=True, slots=True)
class DraftRule:
    input_header: str
    operation: str
    output_field: str
    notes: str


@dataclass(slots=True)
class DraftAnalyzerScaffold:
    """Inert analyzer proposal. It contains data, never executable source code."""

    manifest: AnalyzerManifest
    source_sheet: str
    input_columns: list[str]
    rules: list[DraftRule]
    sample_values: dict[str, list[object]] = field(default_factory=dict)
    required_tests: list[str] = field(default_factory=list)
    activation_allowed: bool = False

    def to_json(self) -> str:
        payload = asdict(self)
        payload["manifest"]["lifecycle"] = self.manifest.lifecycle.value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)


class SafeDraftAnalyzerGenerator:
    """Creates reviewable DRAFT manifests without eval/import/dynamic execution."""

    def create_drafts(self, catalog: SourceCatalog) -> list[DraftAnalyzerScaffold]:
        drafts: list[DraftAnalyzerScaffold] = []
        unconsumed = set(catalog.unconsumed_columns)
        for sheet in catalog.sheets:
            unmapped = [
                column
                for column in sheet.columns
                if (sheet.sheet, column.header) in unconsumed
            ]
            if not unmapped:
                continue
            by_domain: dict[str, list[ColumnProfile]] = defaultdict(list)
            for column in unmapped:
                domain = (
                    column.semantic_candidates[0]
                    if column.semantic_candidates
                    else "unclassified"
                )
                by_domain[domain].append(column)
            for domain, columns in sorted(by_domain.items()):
                name = self._safe_name(
                    f"{sheet.sheet}_{domain}_extension_analyzer"
                )
                drafts.append(
                    DraftAnalyzerScaffold(
                        manifest=AnalyzerManifest(
                            name=name,
                            version="0.1.0-draft",
                            description=(
                                f"Draft {domain} analyzer for unmapped columns in {sheet.sheet}"
                            ),
                            header_signals=tuple(column.header for column in columns),
                            outputs=tuple(self._safe_name(column.header) for column in columns),
                            lifecycle=AnalyzerLifecycle.DRAFT,
                            priority=50,
                        ),
                        source_sheet=sheet.sheet,
                        input_columns=[column.header for column in columns],
                        rules=[self._draft_rule(column) for column in columns],
                        sample_values={
                            column.header: list(column.examples) for column in columns
                        },
                        required_tests=[
                            "unknown and blank values remain review items",
                            "every proposal includes SourceRef provenance",
                            "analyzer cannot write to external services",
                            "shadow output matches the declared schema",
                        ],
                        activation_allowed=False,
                    )
                )
        return drafts

    @staticmethod
    def _draft_rule(column: ColumnProfile) -> DraftRule:
        return DraftRule(
            input_header=column.header,
            operation="preserve_then_classify",
            output_field=SafeDraftAnalyzerGenerator._safe_name(column.header),
            notes=(
                f"Observed type={column.inferred_type.value}; keep original value and require "
                "human-reviewed deterministic mapping before activation; semantic candidates="
                f"{','.join(column.semantic_candidates) or 'none'}"
            ),
        )

    @staticmethod
    def _safe_name(value: str) -> str:
        pieces: list[str] = []
        for char in value.strip().lower():
            if char.isascii() and (char.isalnum() or char == "_"):
                pieces.append(char)
            elif char.isspace() or re.match(r"[^\w]", char):
                pieces.append("_")
            else:
                # Preserve non-ASCII business names so separate sheets cannot collide.
                pieces.append(f"_u{ord(char):x}_")
        ascii_name = re.sub(r"_+", "_", "".join(pieces)).strip("_")
        if not ascii_name:
            ascii_name = "unclassified"
        if ascii_name[0].isdigit():
            ascii_name = f"field_{ascii_name}"
        return ascii_name
