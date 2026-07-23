from __future__ import annotations

import json
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
    ProposalRevision,
    ProposedChange,
    RecordChange,
    ReviewItem,
    SourceRecord,
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
        first_run = self.database.get_checkpoint(drive_id, item_id) is None
        baseline_version, current_version = self._versions(
            drive_id, item_id, initial_cutoff
        )
        pending_operations = self.database.load_pending_operations()
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
        all_changes = self._deduplicate(domain_changes)
        all_changes.extend(self._provenance_changes(current, row_changes, source_web_url))
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
        history_changes = self._history_changes(all_changes)
        history_changes = self._merge_pending(history_changes, pending_history)
        self._refresh_current_values(history_changes)
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

        proposal = self.proposals.create(
            source_version_id=current.version_id,
            source_file_hash=current.file_hash,
            requested_by=requested_by,
            chat_id=chat_id,
            changes=all_changes,
            defer_databases=(
                {
                    change.target_database
                    for change in all_changes
                    if change.target_database not in self.available_data_sources
                }
                if self.available_data_sources is not None
                else None
            ),
            notion_binding=(
                build_notion_binding(
                    all_changes,
                    self.data_source_ids,
                    schema_provider=self.notion_schema_provider,
                )
                if self.data_source_ids is not None
                else None
            ),
            knowledge_binding=(
                knowledge.binding
                if any(change.knowledge_refs for change in all_changes)
                else None
            ),
        )
        proposal = self.proposals.request_approval(proposal.proposal_id)
        self.database.audit(
            "source_analyzed",
            {
                "baseline": baseline.version_id,
                "current": current.version_id,
                "row_changes": len(row_changes),
                "operations": len(proposal.operations),
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
        for review in reviews:
            identity = sha256_json(
                {
                    "version": version_id,
                    "title": review.title,
                    "refs": [
                        (item.sheet, item.row, item.cells) for item in review.source_refs
                    ],
                }
            )[:20]
            entity_key = f"review:{identity}"
            evidence = "; ".join(
                f"{item.sheet}!{item.cells} (v{item.version_id})"
                for item in review.source_refs
            )
            fields: list[tuple[str, object, float]] = [
                ("검토항목", review.title, 1.0),
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
                        source_refs=review.source_refs,
                        knowledge_refs=review.knowledge_refs,
                    )
                )
        return result

    @staticmethod
    def _provenance_changes(
        snapshot: WorkbookSnapshot,
        row_changes: list,
        source_web_url: str | None,
    ) -> list[ProposedChange]:
        result: list[ProposedChange] = []
        for change in row_changes:
            record = change.after or change.before
            if record is None or not record.source_refs:
                continue
            refs = record.source_refs
            source = refs[0]
            entity_key = f"source:{source.version_id}:{record.sheet}:{record.row}"
            title = f"Excel {record.sheet} {record.row}행 (v{source.version_id})"
            fields: list[tuple[str, object]] = [
                ("근거명", title),
                ("driveItem ID", source.item_id),
                ("버전ID", source.version_id),
                ("SHA256", source.file_hash),
                ("원본유형", "엑셀"),
                ("수집일시", snapshot.captured_at.isoformat()),
                ("파싱상태", "완료"),
                ("보안등급", "내부"),
                ("요약", f"{record.sheet} 시트 {record.row}행 변경분"),
            ]
            if source_web_url:
                fields.append(("OneDrive URL", source_web_url))
            for property_name, value in fields:
                result.append(
                    ProposedChange(
                        target_database="근거자료",
                        entity_key=entity_key,
                        property_name=property_name,
                        kind=ChangeKind.CREATE,
                        current_value=None,
                        proposed_value=value,
                        analyzer="provenance_tracker",
                        analyzer_version="1.0.0",
                        confidence=1.0,
                        reason="변경 제안의 로컬·Excel 원본 위치를 보존합니다.",
                        source_refs=refs,
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
                ("변경명", f"{change.entity_key} · {change.property_name}"),
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
                item.entity_key,
                item.property_name,
            ),
        )


def _display_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


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
