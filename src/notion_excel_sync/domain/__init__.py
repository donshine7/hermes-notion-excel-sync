"""Extensible, deterministic domain-analysis platform.

This package emits local :class:`AnalyzerResult` proposals only.  It deliberately
contains no Notion, Telegram, OneDrive, or other write adapter.
"""

from notion_excel_sync.domain.analyzer import (
    AnalysisContext,
    AnalyzerContractError,
    AnalyzerManifest,
    DomainAnalyzer,
    ValueKey,
)
from notion_excel_sync.domain.analyzers import (
    ACTUAL_COST_CONTEXT_METADATA,
    EVIDENCE_CONTEXT_METADATA,
    ActualCostContextEntry,
    ActualCostAnalyzer,
    AnomalyAnalyzer,
    CaseHistoryAnalyzer,
    CaseIdentityAnalyzer,
    ContactAnalyzer,
    DeadlineAnalyzer,
    DocumentMaterialAnalyzer,
    EvidenceContextEntry,
    GovernmentSupportEvidenceAnalyzer,
    GroupAnalyzer,
    PartyAnalyzer,
    RegistrationFollowupAnalyzer,
    RelationshipAnalyzer,
    WorkflowStatusAnalyzer,
    build_actual_cost_context,
)
from notion_excel_sync.domain.generator import (
    DraftAnalyzerScaffold,
    DraftRule,
    SafeDraftAnalyzerGenerator,
)
from notion_excel_sync.domain.profiler import (
    ColumnProfile,
    InferredType,
    SheetProfile,
    SourceCatalog,
    SourceProfiler,
)
from notion_excel_sync.domain.registry import AnalyzerRegistry, AnalyzerRegistryError
from notion_excel_sync.domain.router import (
    AnalysisPipeline,
    AnalysisRun,
    AnalyzerRouter,
    RoutingDecision,
)


def build_default_registry() -> AnalyzerRegistry:
    """Create the initial practical analyzer set with validated dependencies."""

    registry = AnalyzerRegistry(
        (
            CaseIdentityAnalyzer(),
            PartyAnalyzer(),
            GroupAnalyzer(),
            WorkflowStatusAnalyzer(),
            DeadlineAnalyzer(),
            CaseHistoryAnalyzer(),
            ActualCostAnalyzer(),
            GovernmentSupportEvidenceAnalyzer(),
            RegistrationFollowupAnalyzer(),
            ContactAnalyzer(),
            DocumentMaterialAnalyzer(),
            RelationshipAnalyzer(),
            AnomalyAnalyzer(),
        )
    )
    registry.validate()
    return registry


__all__ = [
    "ACTUAL_COST_CONTEXT_METADATA",
    "EVIDENCE_CONTEXT_METADATA",
    "ActualCostContextEntry",
    "ActualCostAnalyzer",
    "AnalysisContext",
    "AnalysisPipeline",
    "AnalysisRun",
    "AnalyzerContractError",
    "AnalyzerManifest",
    "AnalyzerRegistry",
    "AnalyzerRegistryError",
    "AnalyzerRouter",
    "AnomalyAnalyzer",
    "CaseIdentityAnalyzer",
    "CaseHistoryAnalyzer",
    "ColumnProfile",
    "ContactAnalyzer",
    "DeadlineAnalyzer",
    "DocumentMaterialAnalyzer",
    "EvidenceContextEntry",
    "DomainAnalyzer",
    "DraftAnalyzerScaffold",
    "DraftRule",
    "GovernmentSupportEvidenceAnalyzer",
    "GroupAnalyzer",
    "InferredType",
    "PartyAnalyzer",
    "RegistrationFollowupAnalyzer",
    "RelationshipAnalyzer",
    "RoutingDecision",
    "SafeDraftAnalyzerGenerator",
    "SheetProfile",
    "SourceCatalog",
    "SourceProfiler",
    "ValueKey",
    "WorkflowStatusAnalyzer",
    "build_default_registry",
    "build_actual_cost_context",
]
