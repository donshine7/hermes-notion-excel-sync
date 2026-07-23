from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from notion_excel_sync.domain.analyzer import AnalysisContext, ValueKey
from notion_excel_sync.domain.analyzers.common import normalize_header, searchable_text
from notion_excel_sync.domain.registry import AnalyzerRegistry
from notion_excel_sync.models import (
    AnalyzerResult,
    JsonValue,
    KnowledgeRef,
    RecordChange,
    SourceRecord,
)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    record_key: str
    analyzer_names: tuple[str, ...]
    scores: Mapping[str, int]
    matched_signals: Mapping[str, tuple[str, ...]]


@dataclass(slots=True)
class AnalysisRun:
    change: RecordChange
    routing: RoutingDecision
    results: list[AnalyzerResult] = field(default_factory=list)
    knowledge_refs_by_analyzer: Mapping[str, tuple[KnowledgeRef, ...]] = field(
        default_factory=dict,
        repr=False,
    )

    @property
    def proposed_changes(self):
        return [change for result in self.results for change in result.proposed_changes]

    @property
    def review_items(self):
        return [item for result in self.results for item in result.review_items]


class AnalyzerRouter:
    """Selects analyzers from declarative source signals."""

    def __init__(self, registry: AnalyzerRegistry) -> None:
        self.registry = registry

    def route(self, record: SourceRecord, *, include_shadow: bool = False) -> RoutingDecision:
        headers = {normalize_header(header) for header in record.values}
        sheet = normalize_header(record.sheet)
        text = searchable_text(record)
        selected: set[str] = set()
        scores: dict[str, int] = {}
        matched: dict[str, tuple[str, ...]] = {}

        active = self.registry.active(include_shadow=include_shadow)
        for analyzer in active:
            manifest = analyzer.manifest
            hits: list[str] = []
            score = 0
            for signal in manifest.header_signals:
                normalized = normalize_header(signal)
                if any(normalized in header or header in normalized for header in headers):
                    score += 3
                    hits.append(f"header:{signal}")
            for signal in manifest.sheet_signals:
                normalized = normalize_header(signal)
                if normalized and normalized in sheet:
                    score += 2
                    hits.append(f"sheet:{signal}")
            for signal in manifest.keyword_signals:
                normalized = normalize_header(signal)
                if normalized and normalized in text:
                    score += 1
                    hits.append(f"keyword:{signal}")
            if score > 0:
                selected.add(manifest.name)
                scores[manifest.name] = score
                matched[manifest.name] = tuple(hits)

        # Rows with no recognizable domain are preserved by returning an empty decision.
        if selected:
            selected.update(
                analyzer.manifest.name for analyzer in active if analyzer.manifest.always_run
            )
            ordered = self.registry.ordered(selected)
        else:
            ordered = []
        return RoutingDecision(
            record_key=record.key,
            analyzer_names=tuple(analyzer.manifest.name for analyzer in ordered),
            scores=scores,
            matched_signals=matched,
        )


class AnalysisPipeline:
    """Executes pure analyzers for changes; it has no adapter or write capability."""

    def __init__(self, registry: AnalyzerRegistry, router: AnalyzerRouter | None = None) -> None:
        registry.validate()
        self.registry = registry
        self.router = router or AnalyzerRouter(registry)

    def analyze_change(
        self,
        change: RecordChange,
        *,
        current_values: Mapping[ValueKey, JsonValue] | None = None,
        metadata: Mapping[str, object] | None = None,
        knowledge_refs_by_analyzer: Mapping[str, tuple[KnowledgeRef, ...]] | None = None,
        include_shadow: bool = False,
    ) -> AnalysisRun:
        record = change.after or change.before
        if record is None:
            raise ValueError(f"change {change.key!r} has no source record")
        routing = self.router.route(record, include_shadow=include_shadow)
        prior_results: dict[str, AnalyzerResult] = {}
        knowledge_refs = knowledge_refs_by_analyzer or {}
        run = AnalysisRun(
            change=change,
            routing=routing,
            knowledge_refs_by_analyzer=knowledge_refs,
        )
        for name in routing.analyzer_names:
            analyzer = self.registry.get(name)
            context = AnalysisContext(
                change=change,
                prior_results=prior_results,
                current_values=current_values or {},
                metadata=metadata or {},
                knowledge_refs=knowledge_refs.get(name, ()),
            )
            result = analyzer.validate_result(analyzer.analyze(context))
            prior_results[name] = result
            run.results.append(result)
        return run

    def analyze_changes(
        self,
        changes: Iterable[RecordChange],
        *,
        current_values: Mapping[ValueKey, JsonValue] | None = None,
        metadata: Mapping[str, object] | None = None,
        knowledge_refs_by_analyzer: Mapping[str, tuple[KnowledgeRef, ...]] | None = None,
        include_shadow: bool = False,
    ) -> list[AnalysisRun]:
        runs = [
            self.analyze_change(
                change,
                current_values=current_values,
                metadata=metadata,
                knowledge_refs_by_analyzer=knowledge_refs_by_analyzer,
                include_shadow=include_shadow,
            )
            for change in changes
        ]
        for analyzer in self.registry.active(include_shadow=include_shadow):
            aggregate = getattr(analyzer, "aggregate_runs", None)
            if callable(aggregate):
                aggregate(
                    runs,
                    current_values=current_values or {},
                    metadata=metadata or {},
                )
                for run in runs:
                    for result in run.results:
                        if result.analyzer == analyzer.manifest.name:
                            analyzer.validate_result(result)
        return runs
