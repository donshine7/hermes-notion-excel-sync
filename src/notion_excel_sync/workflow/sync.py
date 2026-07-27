from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from notion_excel_sync.adapters.excel import ExcelSnapshotReader, diff_snapshots
from notion_excel_sync.adapters.onedrive import DriveVersion, OneDriveReadOnlyClient
from notion_excel_sync.domain import (
    ACTUAL_COST_CONTEXT_METADATA,
    EVIDENCE_CONTEXT_METADATA,
    AnalysisPipeline,
    EvidenceContextEntry,
    SafeDraftAnalyzerGenerator,
    SourceProfiler,
    build_actual_cost_context,
)
from notion_excel_sync.models import (
    ChangeKind,
    KnowledgeRef,
    ProposalAction,
    ProposalOperation,
    ProposalRevision,
    ProposedChange,
    RecordChange,
    ReviewItem,
    SourceRecord,
    SourceRef,
    WorkbookSnapshot,
    sha256_json,
)
from notion_excel_sync.knowledge.provider import (
    AnalyzerKnowledgeProvider,
    AnalyzerKnowledgeSnapshot,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.workflow.notion_writer import (
    NOTION_DEFINITIONS,
    NotionCurrentValueReader,
    build_notion_binding,
)
from notion_excel_sync.workflow.proposals import ProposalService


@dataclass(slots=True)
class PreparationResult:
    baseline_version_id: str
    current_version_id: str
    changes_detected: int
    proposal: ProposalRevision | None
    pending_reintroduced: int = 0
    draft_analyzers: tuple[str, ...] = ()
    checkpoint_advanced: bool = False
    wiki_generation_digest: str | None = None
    wiki_shadow_hits: int = 0
    pending_staged: int = 0


_NOTION_TITLE_TEXT_LIMIT = 2_000
_REVIEW_IDENTITY_VERSION = 2
_REVIEW_TITLE_DIGEST_LENGTH = 16
_REVIEW_LOCATOR_LIMIT = 96
_PROPOSAL_BATCH_MAX_ENTITIES = 25
_PROPOSAL_BATCH_MAX_OPERATIONS = 500
_LEGACY_ROW_PROVENANCE_ANALYZER = "provenance_tracker"
_LEGACY_ROW_PROVENANCE_DATABASE = "근거자료"
_REVIEW_MATERIALIZER_ANALYZER = "review_materializer"
_REVIEW_DATABASE = "검토함"
_REVIEW_CONFIDENCE_PROPERTY = "신뢰도"
_BOOTSTRAP_REVIEW_MIN_CONFIDENCE = 0.8
_OBSOLETE_PARTY_ANALYZER_VERSION = "1.0.0"


def _retire_obsolete_party_pending(
    database: StateDatabase,
    pending_operations: list[ProposalOperation],
    *,
    actor: str,
) -> tuple[list[ProposalOperation], int]:
    """Retire only unreviewed bootstrap party output from the unsafe v1 parser."""

    obsolete = [
        operation
        for operation in pending_operations
        if (
            operation.change.analyzer == "party"
            and operation.change.analyzer_version
            == _OBSOLETE_PARTY_ANALYZER_VERSION
            and operation.change.target_database == "당사자"
            and operation.action is ProposalAction.DEFER
            and not operation.user_override
        )
    ]
    if not obsolete:
        return pending_operations, 0
    operation_ids = {operation.change.operation_id for operation in obsolete}
    retired = database.retire_pending_operations(
        operation_ids,
        expected_analyzer="party",
        expected_database="당사자",
        actor=actor,
        reason=(
            "Party analyzer v1.1 rematerializes the current Excel snapshot "
            "without splitting legal-name commas or auto-registering composite names"
        ),
    )
    if retired != len(operation_ids):
        raise RuntimeError("Obsolete party pending retirement count mismatch")
    return (
        [
            operation
            for operation in pending_operations
            if operation.change.operation_id not in operation_ids
        ],
        retired,
    )


def _review_group_confidence(
    operations: list[ProposalOperation],
) -> float | None:
    for operation in operations:
        if operation.change.property_name != _REVIEW_CONFIDENCE_PROPERTY:
            continue
        value = operation.approved_value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
    return None


def _retire_obsolete_bootstrap_reviews(
    database: StateDatabase,
    pending_operations: list[ProposalOperation],
    *,
    current_version_id: str,
    actor: str,
) -> tuple[list[ProposalOperation], dict[str, int]]:
    """Retire only stale or low-value review pages before the first checkpoint.

    Rejected first-run proposals can leave durable pending review operations.
    A later Excel save has a new source version, so retaining the old review
    identity would duplicate the same uncertainty. Low-confidence bootstrap
    reviews are also summarized locally instead of creating thousands of
    Notion pages. Non-review work and high-confidence current reviews are
    preserved exactly.
    """

    review_groups: dict[str, list[ProposalOperation]] = {}
    for operation in pending_operations:
        change = operation.change
        if (
            change.analyzer == _REVIEW_MATERIALIZER_ANALYZER
            and change.target_database == _REVIEW_DATABASE
        ):
            review_groups.setdefault(change.entity_key, []).append(operation)

    stale_ids: set[str] = set()
    low_confidence_ids: set[str] = set()
    for group in review_groups.values():
        source_versions = {
            ref.version_id
            for operation in group
            for ref in operation.change.source_refs
            if ref.version_id
        }
        if source_versions and current_version_id not in source_versions:
            stale_ids.update(
                operation.change.operation_id for operation in group
            )
            continue
        confidence = _review_group_confidence(group)
        if (
            confidence is not None
            and confidence < _BOOTSTRAP_REVIEW_MIN_CONFIDENCE
        ):
            low_confidence_ids.update(
                operation.change.operation_id for operation in group
            )

    retired_ids = stale_ids | low_confidence_ids
    if retired_ids:
        database.retire_pending_operations(
            retired_ids,
            expected_analyzer=_REVIEW_MATERIALIZER_ANALYZER,
            expected_database=_REVIEW_DATABASE,
            actor=actor,
            reason=(
                "Superseded first-run reviews and low-confidence bootstrap "
                "reviews are summarized locally instead of materialized as "
                "individual Notion pages"
            ),
        )
    return (
        [
            operation
            for operation in pending_operations
            if operation.change.operation_id not in retired_ids
        ],
        {
            "stale_operations": len(stale_ids),
            "low_confidence_operations": len(low_confidence_ids),
            "retired_operations": len(retired_ids),
        },
    )


def _source_ref_identity(ref: SourceRef) -> dict[str, object]:
    return {
        "drive_id": ref.drive_id,
        "item_id": ref.item_id,
        "version_id": ref.version_id,
        "file_hash": ref.file_hash,
        "sheet": ref.sheet,
        "row": ref.row,
        "cells": ref.cells,
        "raw_value": ref.raw_value,
    }


def _knowledge_ref_identity(ref: KnowledgeRef) -> dict[str, object]:
    return {
        "drive_id": ref.drive_id,
        "item_id": ref.item_id,
        "version_id": ref.version_id,
        "content_hash": ref.content_hash,
        "chunk_locator": ref.chunk_locator,
        "chunk_hash": ref.chunk_hash,
        "security_classification": ref.security_classification,
        "display_name": ref.display_name,
        "etag": ref.etag,
        "extractor_version": ref.extractor_version,
        "policy_version": ref.policy_version,
        "effective_from": ref.effective_from,
        "effective_to": ref.effective_to,
        "authority": ref.authority,
        "status": ref.status,
    }


def _identity_item_key(item: dict[str, object]) -> str:
    return json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _normalized_identity_items(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Sort citations as before while removing exact semantic duplicates."""

    unique = {_identity_item_key(item): item for item in items}
    return sorted(
        unique.values(),
        key=lambda item: (sha256_json(item), _identity_item_key(item)),
    )


def _normalized_source_refs(refs: list[SourceRef]) -> list[SourceRef]:
    unique = {
        _identity_item_key(_source_ref_identity(ref)): ref
        for ref in refs
    }
    return sorted(
        unique.values(),
        key=lambda ref: (
            sha256_json(_source_ref_identity(ref)),
            _identity_item_key(_source_ref_identity(ref)),
        ),
    )


def _normalized_knowledge_refs(refs: list[KnowledgeRef]) -> list[KnowledgeRef]:
    unique = {
        _identity_item_key(_knowledge_ref_identity(ref)): ref
        for ref in refs
    }
    return sorted(
        unique.values(),
        key=lambda ref: (
            sha256_json(_knowledge_ref_identity(ref)),
            _identity_item_key(_knowledge_ref_identity(ref)),
        ),
    )


def _review_identity_payload(review: ReviewItem, version_id: str) -> dict[str, object]:
    """Bind one review page to every fact that distinguishes its meaning."""

    return {
        "identity_version": _REVIEW_IDENTITY_VERSION,
        "version": version_id,
        "review_type": review.review_type,
        "title": review.title,
        "reason": review.reason,
        "current_value": review.current_value,
        "proposed_value": review.proposed_value,
        "confidence": review.confidence,
        "source_refs": _normalized_identity_items(
            [_source_ref_identity(ref) for ref in review.source_refs]
        ),
        "knowledge_refs": _normalized_identity_items(
            [_knowledge_ref_identity(ref) for ref in review.knowledge_refs]
        ),
    }


def _legacy_review_entity_key_for_refs(
    review: ReviewItem,
    version_id: str,
    refs: list[SourceRef],
) -> str:
    identity = sha256_json(
        {
            "version": version_id,
            "title": review.title,
            "refs": [
                (item.sheet, item.row, item.cells) for item in refs
            ],
        }
    )[:20]
    return f"review:{identity}"


def _legacy_review_entity_key(review: ReviewItem, version_id: str) -> str:
    """Reproduce the pre-v2 key for the citation order received today."""

    return _legacy_review_entity_key_for_refs(
        review,
        version_id,
        review.source_refs,
    )


def _legacy_review_entity_key_candidates(
    review: ReviewItem,
    version_id: str,
) -> tuple[str, ...]:
    """Return a bounded set of old order-sensitive keys for safe migration."""

    current_order = _legacy_review_entity_key(review, version_id)
    canonical_order = _legacy_review_entity_key_for_refs(
        review,
        version_id,
        _normalized_source_refs(review.source_refs),
    )
    return tuple(dict.fromkeys((current_order, canonical_order)))


def _compact_title_part(value: object, limit: int) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


def _review_source_locator(review: ReviewItem) -> str:
    if review.source_refs:
        ref = min(review.source_refs, key=lambda item: sha256_json(_source_ref_identity(item)))
        locator = f"{ref.sheet}!{ref.cells}" if ref.cells else f"{ref.sheet} {ref.row}행"
    elif review.knowledge_refs:
        ref = min(
            review.knowledge_refs,
            key=lambda item: sha256_json(_knowledge_ref_identity(item)),
        )
        locator = f"Wiki {ref.display_name or ref.chunk_locator}"
    else:
        locator = review.review_type
    return _compact_title_part(locator or "근거 미지정", _REVIEW_LOCATOR_LIMIT)


def _bounded_review_title(review: ReviewItem, identity: str) -> str:
    """Keep the visible title readable while making title matching deterministic."""

    base = " ".join(str(review.title).split()) or "검토 항목"
    suffix = (
        f" · {_review_source_locator(review)}"
        f" · {identity[:_REVIEW_TITLE_DIGEST_LENGTH]}"
    )
    available = _NOTION_TITLE_TEXT_LIMIT - len(suffix)
    if len(base) > available:
        base = f"{base[: max(available - 1, 0)].rstrip()}…"
    return f"{base}{suffix}"


def _bounded_history_title(change: ProposedChange) -> str:
    """Give each append-only history row a readable, operation-unique title."""

    operation_id = " ".join(str(change.operation_id).split())
    if len(operation_id) > _REVIEW_LOCATOR_LIMIT:
        operation_id = (
            f"{operation_id[: _REVIEW_LOCATOR_LIMIT - 18].rstrip()}…"
            f"{sha256_json({'operation_id': change.operation_id})[:16]}"
        )
    suffix = f" · op {operation_id}"
    base = f"{change.entity_key} · {change.property_name}"
    available = _NOTION_TITLE_TEXT_LIMIT - len(suffix)
    if len(base) > available:
        base = f"{base[: max(available - 1, 0)].rstrip()}…"
    return f"{base}{suffix}"


class SyncPreparationService:
    """Read the configured source, analyze changed rows, and create a proposal."""

    def __init__(
        self,
        database: StateDatabase,
        onedrive: OneDriveReadOnlyClient,
        excel: ExcelSnapshotReader,
        pipeline: AnalysisPipeline,
        proposals: ProposalService,
        notion_reader: NotionCurrentValueReader | None = None,
        available_data_sources: set[str] | None = None,
        data_source_ids: Mapping[str, str] | None = None,
        notion_schema_provider: Callable[[str], Mapping[str, Any]] | None = None,
        knowledge_provider: AnalyzerKnowledgeProvider | None = None,
        first_run_full_reconcile: bool = False,
        snapshot_observer: Callable[[WorkbookSnapshot, bool], None] | None = None,
    ) -> None:
        self.database = database
        self.onedrive = onedrive
        self.excel = excel
        self.pipeline = pipeline
        self.proposals = proposals
        self.notion_reader = notion_reader
        self.available_data_sources = available_data_sources
        self.data_source_ids = data_source_ids
        self.notion_schema_provider = notion_schema_provider
        self.knowledge_provider = knowledge_provider
        self.first_run_full_reconcile = first_run_full_reconcile
        self.snapshot_observer = snapshot_observer

    def _versions(
        self, drive_id: str, item_id: str, initial_cutoff: datetime
    ) -> tuple[DriveVersion, DriveVersion]:
        versions = self.onedrive.list_versions(drive_id, item_id)
        if not versions:
            raise RuntimeError("Configured source returned no retained workbook versions")
        latest_time = max(item.last_modified_at for item in versions)
        latest = [item for item in versions if item.last_modified_at == latest_time]
        if len(latest) != 1:
            raise RuntimeError(
                "Configured source returned multiple current versions with the same timestamp"
            )
        current = latest[0]
        checkpoint = self.database.get_checkpoint(drive_id, item_id)
        if checkpoint:
            matching = [item for item in versions if item.id == checkpoint["version_id"]]
            if not matching:
                raise RuntimeError(
                    "The last committed source version is no longer retained; manual baseline "
                    "recovery is required"
                )
            baseline = matching[0]
        elif self.first_run_full_reconcile:
            # A local first run starts from the exact command-time snapshot.
            # It must not consult a historical cutoff inherited from the
            # legacy remote-source workflow.
            baseline = current
        else:
            baseline = self.onedrive.select_initial_baseline(
                drive_id, item_id, initial_cutoff
            )
        return baseline, current

    def _snapshot(
        self, content: bytes, drive_id: str, item_id: str, version_id: str
    ) -> WorkbookSnapshot:
        return self.excel.read_bytes(
            content,
            drive_id=drive_id,
            item_id=item_id,
            version_id=version_id,
        )

    def prepare(
        self,
        *,
        drive_id: str,
        item_id: str,
        initial_cutoff: datetime,
        requested_by: str,
        chat_id: str,
        source_web_url: str | None = None,
    ) -> PreparationResult:
        clear_reader_cache = getattr(self.notion_reader, "clear_cache", None)
        if callable(clear_reader_cache):
            clear_reader_cache()
        first_run = self.database.get_checkpoint(drive_id, item_id) is None
        baseline_version, current_version = self._versions(
            drive_id, item_id, initial_cutoff
        )
        bootstrap_review_retirement = {
            "stale_operations": 0,
            "low_confidence_operations": 0,
            "retired_operations": 0,
        }
        obsolete_party_operations_retired = 0
        pending_operations = self.database.load_pending_operations()
        legacy_provenance = [
            operation
            for operation in pending_operations
            if (
                operation.change.analyzer == _LEGACY_ROW_PROVENANCE_ANALYZER
                and operation.change.target_database
                == _LEGACY_ROW_PROVENANCE_DATABASE
            )
        ]
        if legacy_provenance:
            retired_ids = {
                operation.change.operation_id
                for operation in legacy_provenance
            }
            self.database.retire_pending_operations(
                retired_ids,
                expected_analyzer=_LEGACY_ROW_PROVENANCE_ANALYZER,
                expected_database=_LEGACY_ROW_PROVENANCE_DATABASE,
                actor=requested_by,
                reason=(
                    "Excel row provenance remains in immutable SourceRef, "
                    "local audit state, and Wiki; it is no longer materialized "
                    "as one Notion page per row"
                ),
            )
            pending_operations = [
                operation
                for operation in pending_operations
                if operation.change.operation_id not in retired_ids
            ]
        if first_run:
            (
                pending_operations,
                bootstrap_review_retirement,
            ) = _retire_obsolete_bootstrap_reviews(
                self.database,
                pending_operations,
                current_version_id=current_version.id,
                actor=requested_by,
            )
            (
                pending_operations,
                obsolete_party_operations_retired,
            ) = _retire_obsolete_party_pending(
                self.database,
                pending_operations,
                actor=requested_by,
            )
        wiki_pending_count = sum(
            bool(operation.change.knowledge_refs) for operation in pending_operations
        )
        if wiki_pending_count:
            # A deferred operation currently stores its citations but not the
            # immutable Wiki generation binding from the proposal that created
            # it. Rebinding those old citations to whatever generation happens
            # to be current would defeat apply-time provenance validation.
            self.database.audit(
                "wiki_pending_rematerialization_blocked",
                {"operations": wiki_pending_count},
                None,
                requested_by,
            )
            raise RuntimeError(
                "Deferred Wiki-backed operations cannot be rematerialized until "
                "their original Wiki binding can be validated"
            )
        pending_changes: list[ProposedChange] = []
        for operation in pending_operations:
            change = deepcopy(operation.change)
            change.proposed_value = deepcopy(operation.approved_value)
            pending_changes.append(change)
        full_reconcile = self.first_run_full_reconcile and first_run
        if (
            baseline_version.id == current_version.id
            and not pending_changes
            and not full_reconcile
        ):
            return PreparationResult(
                baseline_version.id, current_version.id, 0, None
            )
        current_bytes = self.onedrive.download_current(drive_id, item_id)
        current = self._snapshot(
            current_bytes, drive_id, item_id, current_version.id
        )
        if current.identity_warnings:
            self.database.audit(
                "source_identity_warning",
                {
                    "version_id": current.version_id,
                    "warnings": current.identity_warnings,
                },
                None,
                requested_by,
            )
        catalog = SourceProfiler().profile(current.records)
        drafts = SafeDraftAnalyzerGenerator().create_drafts(catalog)
        for draft in drafts:
            self.database.save_analyzer_definition(
                draft.manifest.name,
                draft.manifest.version,
                draft.manifest.lifecycle.value,
                json.loads(draft.to_json()),
            )
        if baseline_version.id == current_version.id:
            baseline = current
            row_changes = (
                [
                    RecordChange(
                        kind=ChangeKind.CREATE,
                        key=record.key,
                        before=None,
                        after=record,
                    )
                    for record in current.records
                ]
                if full_reconcile
                else []
            )
        else:
            baseline_bytes = self.onedrive.download_version(
                drive_id, item_id, baseline_version.id
            )
            baseline = self._snapshot(
                baseline_bytes, drive_id, item_id, baseline_version.id
            )
            row_changes = diff_snapshots(baseline, current)
        _, current_after_download = self._versions(drive_id, item_id, initial_cutoff)
        if current_after_download.id != current_version.id:
            raise RuntimeError("Configured source changed while the workbook was read")
        if self.snapshot_observer is not None:
            self.snapshot_observer(current, full_reconcile)
        if not row_changes and not pending_changes:
            self.database.commit_noop_checkpoint(
                drive_id=drive_id,
                item_id=item_id,
                version_id=current.version_id,
                file_hash=current.file_hash,
                actor=requested_by,
            )
            return PreparationResult(
                baseline_version.id,
                current_version.id,
                0,
                None,
                0,
                tuple(draft.manifest.name for draft in drafts),
                True,
            )

        knowledge = AnalyzerKnowledgeSnapshot(available=False)
        if row_changes and self.knowledge_provider is not None:
            knowledge = self.knowledge_provider.prepare(row_changes, self.pipeline)
        metadata = {
            "source_web_url": source_web_url,
            "baseline_version_id": baseline_version.id,
            "current_version_id": current_version.id,
            "captured_at": current.captured_at.isoformat(),
            ACTUAL_COST_CONTEXT_METADATA: build_actual_cost_context(current.records),
        }
        if knowledge.available:
            metadata["wiki_generation_digest"] = knowledge.binding.get(
                "generation_digest", ""
            )
            metadata["wiki_shadow_counts"] = knowledge.shadow_counts
        runs = []
        if row_changes:
            metadata[EVIDENCE_CONTEXT_METADATA] = self._build_evidence_context(
                current.records,
                metadata,
                knowledge.verified_refs_by_analyzer,
            )
            preliminary = self.pipeline.analyze_changes(
                row_changes,
                metadata=metadata,
                knowledge_refs_by_analyzer=knowledge.verified_refs_by_analyzer,
            )
            preliminary_changes = [
                change for run in preliminary for change in run.proposed_changes
            ]
            current_values = self._load_current_values(preliminary_changes)
            runs = self.pipeline.analyze_changes(
                row_changes,
                current_values=current_values,
                metadata=metadata,
                knowledge_refs_by_analyzer=knowledge.verified_refs_by_analyzer,
            )
        domain_changes = [change for run in runs for change in run.proposed_changes]
        reviews = [review for run in runs for review in run.review_items]
        reviews.extend(self._unrouted_reviews(runs))
        bootstrap_reviews_suppressed = 0
        if full_reconcile:
            bootstrap_reviews_suppressed = sum(
                review.confidence < _BOOTSTRAP_REVIEW_MIN_CONFIDENCE
                for review in reviews
            )
            reviews = [
                review
                for review in reviews
                if review.confidence >= _BOOTSTRAP_REVIEW_MIN_CONFIDENCE
            ]
            self.database.audit(
                "bootstrap_review_filter",
                {
                    "source_version_id": current.version_id,
                    "minimum_confidence": _BOOTSTRAP_REVIEW_MIN_CONFIDENCE,
                    "materialized_review_pages": len(reviews),
                    "suppressed_review_pages": bootstrap_reviews_suppressed,
                    **bootstrap_review_retirement,
                },
                None,
                requested_by,
            )
        all_changes = self._deduplicate(domain_changes)
        all_changes.extend(self._review_changes(reviews, current.version_id))
        all_changes = self._deduplicate(all_changes)
        pending_regular = [
            change
            for change in pending_changes
            if change.target_database != "변경이력"
        ]
        pending_history = [
            change
            for change in pending_changes
            if change.target_database == "변경이력"
        ]
        all_changes = self._merge_pending(all_changes, pending_regular)
        self._refresh_current_values(all_changes)
        # The command-time T0 snapshot establishes the initial Notion state; it
        # is not a sequence of post-baseline changes. Detailed history begins
        # with later incremental syncs. Previously deferred history is retained.
        history_changes = [] if full_reconcile else self._history_changes(all_changes)
        history_changes = self._merge_pending(history_changes, pending_history)
        # Fresh history rows are append-only and carry an operation-bound unique
        # title, so they cannot have a current value before approval. Deferred
        # history may already be mapped to a page and still needs an exact read.
        self._refresh_pending_history_values(history_changes, pending_history)
        all_changes.extend(history_changes)
        all_changes = self._sort_changes(all_changes)
        if not all_changes:
            if full_reconcile:
                self.database.commit_noop_checkpoint(
                    drive_id=drive_id,
                    item_id=item_id,
                    version_id=current.version_id,
                    file_hash=current.file_hash,
                    actor=requested_by,
                )
            return PreparationResult(
                baseline_version.id,
                current_version.id,
                len(row_changes),
                None,
                checkpoint_advanced=full_reconcile,
            )

        proposal_changes, staged_pending = self._proposal_batch(all_changes)
        proposal = self.proposals.create(
            source_version_id=current.version_id,
            source_file_hash=current.file_hash,
            requested_by=requested_by,
            chat_id=chat_id,
            changes=proposal_changes,
            defer_databases=(
                {
                    change.target_database
                    for change in proposal_changes
                    if change.target_database not in self.available_data_sources
                }
                if self.available_data_sources is not None
                else None
            ),
            notion_binding=(
                build_notion_binding(
                    proposal_changes,
                    self.data_source_ids,
                    schema_provider=self.notion_schema_provider,
                )
                if self.data_source_ids is not None
                else None
            ),
            knowledge_binding=(
                knowledge.binding
                if any(change.knowledge_refs for change in proposal_changes)
                else None
            ),
            staged_pending_changes=staged_pending,
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        self.database.audit(
            "source_analyzed",
            {
                "baseline": baseline.version_id,
                "current": current.version_id,
                "row_changes": len(row_changes),
                "operations": len(proposal.operations),
                "staged_pending_operations": len(staged_pending),
                "bootstrap_reviews_suppressed": bootstrap_reviews_suppressed,
                "bootstrap_review_retirement": bootstrap_review_retirement,
                "obsolete_party_operations_retired": (
                    obsolete_party_operations_retired
                ),
                "wiki_generation_digest": knowledge.binding.get(
                    "generation_digest", ""
                ),
                "wiki_shadow_hits": sum(knowledge.shadow_counts.values()),
            },
            proposal.proposal_id,
            requested_by,
        )
        return PreparationResult(
            baseline.version_id,
            current.version_id,
            len(row_changes),
            proposal,
            len(pending_changes),
            tuple(draft.manifest.name for draft in drafts),
            wiki_generation_digest=(
                str(knowledge.binding.get("generation_digest"))
                if knowledge.binding.get("generation_digest")
                else None
            ),
            wiki_shadow_hits=sum(knowledge.shadow_counts.values()),
            pending_staged=len(staged_pending),
        )

    def _build_evidence_context(
        self,
        records: list[SourceRecord],
        metadata: Mapping[str, object],
        knowledge_refs_by_analyzer: Mapping[str, tuple[KnowledgeRef, ...]] | None = None,
    ) -> tuple[EvidenceContextEntry, ...]:
        """Extract full-snapshot evidence facts without proposing unchanged rows."""

        entries: list[EvidenceContextEntry] = []
        for record in records:
            run = self.pipeline.analyze_change(
                RecordChange(
                    kind=ChangeKind.CREATE,
                    key=record.key,
                    before=None,
                    after=record,
                ),
                metadata=metadata,
                knowledge_refs_by_analyzer=knowledge_refs_by_analyzer,
            )
            result = next(
                (
                    item
                    for item in run.results
                    if item.analyzer == "government_support_evidence"
                ),
                None,
            )
            evidence = result.derived.get("evidence") if result else None
            if isinstance(evidence, dict) and evidence.get("key"):
                entries.append(
                    EvidenceContextEntry(
                        record_key=record.key,
                        evidence=deepcopy(evidence),
                        source_refs=tuple(record.source_refs),
                    )
                )
        return tuple(entries)

    @staticmethod
    def _unrouted_reviews(runs: list) -> list[ReviewItem]:
        reviews: list[ReviewItem] = []
        for run in runs:
            if run.routing.analyzer_names:
                continue
            record = run.change.after or run.change.before
            if record is None:
                continue
            reviews.append(
                ReviewItem(
                    review_type="미분류 분석",
                    title=f"미분류 변경: {record.sheet} {record.row}행",
                    reason=(
                        "활성 분석기의 신호와 일치하지 않아 안전한 DRAFT 분석기 "
                        "후보를 생성하고 사람 검토로 보냅니다."
                    ),
                    current_value=(
                        run.change.before.values if run.change.before else None
                    ),
                    proposed_value=(
                        run.change.after.values if run.change.after else None
                    ),
                    confidence=0.0,
                    source_refs=list(record.source_refs),
                )
            )
        return reviews

    def _load_current_values(
        self, changes: list[ProposedChange]
    ) -> dict[tuple[str, str, str], object]:
        if not self.notion_reader or not changes:
            return {}
        keys = {
            (change.target_database, change.entity_key, change.property_name)
            for change in changes
        }
        return self.notion_reader.load(keys, self._title_values(changes))

    def _refresh_current_values(self, changes: list[ProposedChange]) -> None:
        current_values = self._load_current_values(changes)
        for change in changes:
            key = (
                change.target_database,
                change.entity_key,
                change.property_name,
            )
            if key in current_values:
                change.current_value = current_values[key]

    def _refresh_pending_history_values(
        self,
        history_changes: list[ProposedChange],
        pending_history: list[ProposedChange],
    ) -> None:
        pending_keys = {
            (
                change.target_database,
                change.entity_key,
                change.property_name,
            )
            for change in pending_history
        }
        self._refresh_current_values(
            [
                change
                for change in history_changes
                if (
                    change.target_database,
                    change.entity_key,
                    change.property_name,
                )
                in pending_keys
            ]
        )

    @staticmethod
    def _merge_pending(
        fresh: list[ProposedChange], pending: list[ProposedChange]
    ) -> list[ProposedChange]:
        result = list(fresh)
        by_key = {
            (change.target_database, change.entity_key, change.property_name): change
            for change in result
        }
        pending_keys: set[tuple[str, str, str]] = set()
        for deferred in pending:
            key = (
                deferred.target_database,
                deferred.entity_key,
                deferred.property_name,
            )
            if key in pending_keys:
                raise RuntimeError(
                    "Multiple unresolved deferred operations target the same Notion property"
                )
            pending_keys.add(key)
            current = by_key.get(key)
            if current is not None:
                current.operation_id = deferred.operation_id
                continue
            result.append(deferred)
            by_key[key] = deferred
        return result

    @staticmethod
    def _title_values(changes: list[ProposedChange]) -> dict[tuple[str, str], str]:
        result: dict[tuple[str, str], str] = {}
        for change in changes:
            definition = NOTION_DEFINITIONS.get(change.target_database)
            if definition and change.property_name == definition.title_property:
                result[(change.target_database, change.entity_key)] = str(
                    change.proposed_value
                )
        return result

    @staticmethod
    def _deduplicate(changes: list[ProposedChange]) -> list[ProposedChange]:
        chosen: dict[tuple[str, str, str], ProposedChange] = {}
        for change in changes:
            key = (change.target_database, change.entity_key, change.property_name)
            previous = chosen.get(key)
            if previous is None or change.confidence > previous.confidence:
                chosen[key] = change
        return list(chosen.values())

    @staticmethod
    def _review_type(value: str) -> str:
        exact = {
            "복수 사건": "복수 사건번호",
            "사건 식별": "사건번호 형식",
            "값 형식": "기타",
            "당사자 구분": "당사자 미확정",
            "비용": "비용 파싱",
            "날짜": "날짜 이상",
            "중복": "중복 사건",
            "정책": "정책 충돌",
        }
        if value in exact:
            return exact[value]
        for token, target in exact.items():
            if token in value:
                return target
        return "기타"

    def _review_changes(
        self, reviews: list[ReviewItem], version_id: str
    ) -> list[ProposedChange]:
        result: list[ProposedChange] = []
        unique: dict[str, ReviewItem] = {}
        legacy_noncanonical_key_by_identity: dict[str, str] = {}
        legacy_canonical_key_by_identity: dict[str, str] = {}
        legacy_keys_by_identity: dict[str, set[str]] = {}
        legacy_groups: dict[str, set[str]] = {}
        mapping_cache: dict[str, dict[str, str] | None] = {}

        def mapping_for(entity_key: str) -> dict[str, str] | None:
            if entity_key not in mapping_cache:
                mapping_cache[entity_key] = self.database.get_entity_mapping(
                    "검토함",
                    entity_key,
                )
            return mapping_cache[entity_key]

        for review in reviews:
            identity = sha256_json(_review_identity_payload(review, version_id))
            unique.setdefault(identity, review)
            legacy_keys = _legacy_review_entity_key_candidates(
                review,
                version_id,
            )
            current_key = legacy_keys[0]
            canonical_key = legacy_keys[-1]
            legacy_canonical_key_by_identity[identity] = canonical_key
            if current_key != canonical_key:
                legacy_noncanonical_key_by_identity[identity] = min(
                    current_key,
                    legacy_noncanonical_key_by_identity.get(
                        identity,
                        current_key,
                    ),
                )

        for identity in unique:
            legacy_keys = {legacy_canonical_key_by_identity[identity]}
            noncanonical_key = legacy_noncanonical_key_by_identity.get(
                identity
            )
            if noncanonical_key is not None:
                legacy_keys.add(noncanonical_key)
            legacy_keys_by_identity[identity] = legacy_keys
            for legacy_key in legacy_keys:
                legacy_groups.setdefault(legacy_key, set()).add(identity)

        for identity in sorted(unique):
            review = unique[identity]
            entity_key = f"review:{identity}"
            title = _bounded_review_title(review, identity)
            source_refs = _normalized_source_refs(review.source_refs)
            knowledge_refs = _normalized_knowledge_refs(
                review.knowledge_refs
            )

            # New-style mappings are exact. A legacy mapping is reused only
            # when every order-sensitive legacy key belongs only to this
            # semantic review and every stored mapping resolves to one
            # identical page/title target.
            mapping = mapping_for(entity_key)
            legacy_keys = sorted(legacy_keys_by_identity[identity])
            if mapping is None and all(
                legacy_groups[legacy_key] == {identity}
                for legacy_key in legacy_keys
            ):
                legacy_mappings = [
                    (legacy_key, legacy_mapping)
                    for legacy_key in legacy_keys
                    if (
                        legacy_mapping := mapping_for(legacy_key)
                    )
                    is not None
                ]
                mapping_targets = {
                    (
                        legacy_mapping.get("notion_page_id"),
                        legacy_mapping.get("match_value"),
                    )
                    for _, legacy_mapping in legacy_mappings
                }
                valid_mapping_target = bool(mapping_targets) and all(
                    isinstance(page_id, str)
                    and bool(page_id)
                    and isinstance(match_value, str)
                    and bool(match_value)
                    for page_id, match_value in mapping_targets
                )
                if valid_mapping_target and len(mapping_targets) == 1:
                    entity_key, mapping = legacy_mappings[0]
            if mapping is not None and mapping.get("match_value"):
                title = str(mapping["match_value"])

            evidence = "; ".join(
                f"{item.sheet}!{item.cells} (v{item.version_id})"
                for item in source_refs
            )
            fields: list[tuple[str, object, float]] = [
                ("검토항목", title, 1.0),
                ("검토유형", self._review_type(review.review_type), 1.0),
                ("상태", "대기", 1.0),
                ("현재값", _display_value(review.current_value), review.confidence),
                ("제안값", _display_value(review.proposed_value), review.confidence),
                ("근거", f"{review.reason} | {evidence}", 1.0),
                ("신뢰도", review.confidence, 1.0),
            ]
            for property_name, value, confidence in fields:
                if value in (None, "") and property_name in {"현재값", "제안값"}:
                    continue
                result.append(
                    ProposedChange(
                        target_database="검토함",
                        entity_key=entity_key,
                        property_name=property_name,
                        kind=ChangeKind.CREATE,
                        current_value=None,
                        proposed_value=value,
                        analyzer="review_materializer",
                        analyzer_version="1.0.0",
                        confidence=confidence,
                        reason="분석 결과의 불확실성을 사용자 승인 후 검토함에 기록합니다.",
                        source_refs=source_refs,
                        knowledge_refs=knowledge_refs,
                    )
                )
        return result

    @staticmethod
    def _history_changes(changes: list[ProposedChange]) -> list[ProposedChange]:
        result: list[ProposedChange] = []
        for change in changes:
            if change.target_database == "변경이력":
                continue
            entity_key = f"history:{change.operation_id}"
            change_type = "생성" if change.kind is ChangeKind.CREATE else "값변경"
            area = _history_area(change.target_database)
            fields: list[tuple[str, object]] = [
                ("변경명", _bounded_history_title(change)),
                ("변경유형", change_type),
                ("변경영역", area),
                ("이전값", _display_value(change.current_value)),
                ("새값", _display_value(change.proposed_value)),
                ("변경사유", change.reason),
                (
                    "기록방식",
                    "LLM분석" if "llm" in change.analyzer.casefold() else "자동수집",
                ),
                ("신뢰도", change.confidence),
                ("변경일시", datetime.now(UTC).isoformat()),
                ("사람확인", True),
            ]
            for property_name, value in fields:
                if value in (None, "") and property_name == "이전값":
                    continue
                result.append(
                    ProposedChange(
                        target_database="변경이력",
                        entity_key=entity_key,
                        property_name=property_name,
                        kind=ChangeKind.CREATE,
                        current_value=None,
                        proposed_value=value,
                        analyzer="change_history_materializer",
                        analyzer_version="1.0.0",
                        confidence=1.0,
                        reason="승인된 변경과 함께 감사 이력을 생성합니다.",
                        source_refs=change.source_refs,
                        knowledge_refs=change.knowledge_refs,
                    )
                )
        return result

    @staticmethod
    def _proposal_batch(
        changes: list[ProposedChange],
    ) -> tuple[list[ProposedChange], list[ProposedChange]]:
        """Return one reviewable page-atomic prefix and its durable backlog."""

        grouped: dict[tuple[str, str], list[ProposedChange]] = {}
        for change in changes:
            grouped.setdefault(
                (change.target_database, change.entity_key),
                [],
            ).append(change)

        selected: list[ProposedChange] = []
        staged: list[ProposedChange] = []
        selected_entities = 0
        boundary_reached = False
        for group in grouped.values():
            if not boundary_reached and selected and (
                selected_entities >= _PROPOSAL_BATCH_MAX_ENTITIES
                or len(selected) + len(group) > _PROPOSAL_BATCH_MAX_OPERATIONS
            ):
                boundary_reached = True
            if boundary_reached:
                staged.extend(group)
                continue
            selected.extend(group)
            selected_entities += 1
        return selected, staged

    @staticmethod
    def _sort_changes(changes: list[ProposedChange]) -> list[ProposedChange]:
        order = {
            "근거자료": 0,
            "그룹": 1,
            "당사자": 1,
            "한국 특허 사건": 2,
            "업무·절차": 3,
            "비용·청구": 4,
            "정부지원사업 증빙": 5,
            "기일": 6,
            "사건 히스토리": 6,
            "등록결정 후속관리": 6,
            "연락이력": 6,
            "검토함": 7,
            "변경이력": 8,
        }
        return sorted(
            changes,
            key=lambda item: (
                order.get(item.target_database, 99),
                0
                if NOTION_DEFINITIONS.get(item.target_database)
                and item.property_name
                == NOTION_DEFINITIONS[item.target_database].title_property
                else 1,
                item.target_database,
                _natural_sort_key(item.entity_key),
                item.property_name,
            ),
        )


def _display_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _natural_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"([0-9]+)", str(value))
        if part
    )


def _history_area(database: str) -> str:
    mapping = {
        "한국 특허 사건": "사건기본",
        "그룹": "그룹",
        "당사자": "당사자",
        "업무·절차": "업무·절차",
        "기일": "기일",
        "비용·청구": "비용·청구",
        "정부지원사업 증빙": "근거자료",
        "등록결정 후속관리": "등록결정",
        "연락이력": "연락",
        "근거자료": "근거자료",
        "검토함": "근거자료",
    }
    return mapping.get(database, "근거자료")
