from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping

from notion_excel_sync.models import (
    AnalyzerLifecycle,
    AnalyzerResult,
    ChangeKind,
    JsonValue,
    KnowledgeRef,
    ProposedChange,
    RecordChange,
    ReviewItem,
    SourceRecord,
    SourceRef,
)


ValueKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class AnalyzerManifest:
    """Declarative metadata used to route and order a domain analyzer."""

    name: str
    version: str
    description: str
    header_signals: tuple[str, ...] = ()
    sheet_signals: tuple[str, ...] = ()
    keyword_signals: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    target_databases: tuple[str, ...] = ()
    lifecycle: AnalyzerLifecycle = AnalyzerLifecycle.ACTIVE
    priority: int = 50
    always_run: bool = False
    knowledge_topics: tuple[str, ...] = ()
    uses_verified_knowledge: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("analyzer name must contain only letters, digits, and underscores")
        if not self.version:
            raise ValueError("analyzer version is required")
        if self.name in self.dependencies:
            raise ValueError(f"{self.name} cannot depend on itself")


@dataclass(slots=True)
class AnalysisContext:
    """Read-only inputs available to an analyzer for a single changed source row."""

    change: RecordChange
    prior_results: Mapping[str, AnalyzerResult] = field(default_factory=dict)
    current_values: Mapping[ValueKey, JsonValue] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    knowledge_refs: tuple[KnowledgeRef, ...] = ()

    @property
    def record(self) -> SourceRecord:
        record = self.change.after or self.change.before
        if record is None:  # Defensive: RecordChange should always have one side.
            raise ValueError(f"change {self.change.key!r} has neither before nor after")
        return record

    @property
    def source_refs(self) -> list[SourceRef]:
        return list(self.record.source_refs)

    def current_value(
        self,
        database: str,
        entity_key: str,
        property_name: str,
        fallback: JsonValue = None,
    ) -> JsonValue:
        return self.current_values.get((database, entity_key, property_name), fallback)

    def derived(self, analyzer_name: str) -> Mapping[str, JsonValue]:
        result = self.prior_results.get(analyzer_name)
        return result.derived if result else {}


class AnalyzerContractError(ValueError):
    """Raised when an analyzer emits unsafe or untraceable output."""


class DomainAnalyzer(ABC):
    """Pure domain analyzer. Implementations cannot access or write to Notion."""

    manifest: AnalyzerManifest

    @abstractmethod
    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        """Analyze one source-row change and return proposals only."""

    def empty_result(self) -> AnalyzerResult:
        return AnalyzerResult(analyzer=self.manifest.name, version=self.manifest.version)

    def proposed_change(
        self,
        context: AnalysisContext,
        *,
        database: str,
        entity_key: str,
        property_name: str,
        proposed_value: JsonValue,
        reason: str,
        confidence: float,
        current_value: JsonValue = None,
        source_refs: list[SourceRef] | None = None,
        knowledge_refs: list[KnowledgeRef] | None = None,
    ) -> ProposedChange | None:
        """Create a traceable proposal, omitting values that have not changed."""

        refs = list(source_refs if source_refs is not None else context.source_refs)
        wiki_refs = list(
            knowledge_refs if knowledge_refs is not None else context.knowledge_refs
        )
        if not refs:
            raise AnalyzerContractError(
                f"{self.manifest.name} cannot propose {database}.{property_name} without provenance"
            )
        if context.change.kind is ChangeKind.DELETE_CANDIDATE:
            return None
        current = context.current_value(database, entity_key, property_name, current_value)
        if current == proposed_value:
            return None
        kind = ChangeKind.CREATE if current is None else ChangeKind.UPDATE
        return ProposedChange(
            target_database=database,
            entity_key=entity_key,
            property_name=property_name,
            kind=kind,
            current_value=current,
            proposed_value=proposed_value,
            analyzer=self.manifest.name,
            analyzer_version=self.manifest.version,
            confidence=max(0.0, min(1.0, confidence)),
            reason=reason,
            source_refs=refs,
            knowledge_refs=wiki_refs,
        )

    def review_item(
        self,
        context: AnalysisContext,
        *,
        review_type: str,
        title: str,
        reason: str,
        current_value: JsonValue = None,
        proposed_value: JsonValue = None,
        confidence: float = 0.0,
        source_refs: list[SourceRef] | None = None,
        knowledge_refs: list[KnowledgeRef] | None = None,
    ) -> ReviewItem:
        refs = list(source_refs if source_refs is not None else context.source_refs)
        wiki_refs = list(
            knowledge_refs if knowledge_refs is not None else context.knowledge_refs
        )
        if not refs:
            raise AnalyzerContractError(
                f"{self.manifest.name} cannot create review item {title!r} without provenance"
            )
        return ReviewItem(
            review_type=review_type,
            title=title,
            reason=reason,
            current_value=current_value,
            proposed_value=proposed_value,
            confidence=max(0.0, min(1.0, confidence)),
            source_refs=refs,
            knowledge_refs=wiki_refs,
        )

    def validate_result(self, result: AnalyzerResult) -> AnalyzerResult:
        if result.analyzer != self.manifest.name or result.version != self.manifest.version:
            raise AnalyzerContractError(
                f"{self.manifest.name} returned mismatched analyzer identity/version"
            )
        for change in result.proposed_changes:
            if change.analyzer != self.manifest.name:
                raise AnalyzerContractError("proposal analyzer does not match result analyzer")
            if change.analyzer_version != self.manifest.version:
                raise AnalyzerContractError("proposal version does not match result version")
            if not change.source_refs:
                raise AnalyzerContractError("every proposed change must include provenance")
            if any(not isinstance(ref, KnowledgeRef) for ref in change.knowledge_refs):
                raise AnalyzerContractError("knowledge provenance must use KnowledgeRef")
            if change.target_database not in self.manifest.target_databases:
                raise AnalyzerContractError(
                    f"{self.manifest.name} cannot target {change.target_database!r}"
                )
        for item in result.review_items:
            if not item.source_refs:
                raise AnalyzerContractError("every review item must include provenance")
            if any(not isinstance(ref, KnowledgeRef) for ref in item.knowledge_refs):
                raise AnalyzerContractError("knowledge provenance must use KnowledgeRef")
        return result
