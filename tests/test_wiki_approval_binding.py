from __future__ import annotations

from copy import deepcopy

import pytest

from notion_excel_sync.domain.analyzer import (
    AnalysisContext,
    AnalyzerManifest,
    DomainAnalyzer,
)
from notion_excel_sync.models import (
    AnalyzerResult,
    ChangeKind,
    KnowledgeRef,
    ProposalAction,
    ProposalOperation,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    RecordChange,
    SourceRecord,
    SourceRef,
    sha256_json,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.security.approval import ApprovalError, ApprovalService
from notion_excel_sync.workflow.apply import ApprovalGatedApplyService
from notion_excel_sync.workflow.proposals import ProposalError, ProposalService


def source_ref() -> SourceRef:
    return SourceRef(
        drive_id="excel-drive",
        item_id="excel-item",
        version_id="excel-etag",
        file_hash="excel-hash",
        sheet="2026",
        row=2,
        cells="A2:H2",
    )


def knowledge_ref() -> KnowledgeRef:
    return KnowledgeRef(
        drive_id="wiki-drive",
        item_id="wiki-item",
        version_id="wiki-etag",
        content_hash="document-hash",
        chunk_locator="page:3#chunk:2",
        chunk_hash="chunk-hash",
        security_classification="internal",
        display_name="지원사업 증빙 지침",
        extractor_version="extractor-v1",
        policy_version="policy-v1",
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        authority="issuing-agency",
        status="active",
    )


def proposed_change(*, with_wiki: bool = True) -> ProposedChange:
    return ProposedChange(
        target_database="한국 특허 사건",
        entity_key="SS-2026-001",
        property_name="현재상태",
        kind=ChangeKind.UPDATE,
        current_value="착수",
        proposed_value="출원",
        analyzer="status_analyzer",
        analyzer_version="1.0.0",
        confidence=1.0,
        reason="Excel 상태와 승인된 업무 지침을 함께 검토함",
        source_refs=[source_ref()],
        operation_id="operation-1",
        knowledge_refs=[knowledge_ref()] if with_wiki else [],
    )


class MemoryWriter:
    def __init__(self) -> None:
        self.calls = 0
        self.preflight_calls = 0

    def preflight(self, operation: ProposalOperation) -> bool:
        del operation
        self.preflight_calls += 1
        return True

    def apply(self, operation: ProposalOperation, receipt) -> str:
        del operation, receipt
        self.calls += 1
        return "page-1"


class KnowledgeAwareAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="knowledge_aware",
        version="1",
        description="test",
        target_databases=("한국 특허 사건",),
    )

    def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        del context
        return self.empty_result()


@pytest.fixture
def workflow(tmp_path):
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    approval = ApprovalService(
        secret="a-secure-test-secret-at-least-24-characters",
        allowed_users={"123"},
        nonce_used=database.nonce_used,
        mark_nonce_used=database.mark_nonce_used,
    )
    proposals = ProposalService(database, approval)
    return database, approval, proposals


def approve_wiki_proposal(workflow, binding):
    database, approval, proposals = workflow
    proposal = proposals.create(
        "excel-etag",
        "excel-hash",
        "123",
        "chat",
        [proposed_change()],
        knowledge_binding=binding,
    )
    proposal = proposals.request_approval(proposal.proposal_id)
    receipt = proposals.approve(
        proposal.proposal_id,
        proposal.revision,
        "123",
        "chat",
        proposal.digest,
    )
    return database, approval, proposals, proposal, receipt


def test_empty_wiki_fields_preserve_legacy_digest_shape() -> None:
    change = proposed_change(with_wiki=False)
    operation = ProposalOperation(change=change)
    proposal = ProposalRevision(
        proposal_id="P-legacy",
        revision=1,
        source_version_id="excel-etag",
        source_file_hash="excel-hash",
        requested_by="123",
        chat_id="chat",
        operations=[operation],
    )

    assert "knowledge_refs" not in change.digest_payload()
    assert "user_override" not in operation.digest_payload()
    legacy_payload = {
        "proposal_id": "P-legacy",
        "revision": 1,
        "source_version_id": "excel-etag",
        "source_file_hash": "excel-hash",
        "requested_by": "123",
        "chat_id": "chat",
        "operations": [operation.digest_payload()],
        "notion_binding": {},
    }
    assert proposal.digest == sha256_json(legacy_payload)


def test_wiki_citations_and_binding_round_trip_and_change_digest(workflow) -> None:
    database, _, proposals = workflow
    binding = {"generation_digest": "generation-1", "policy_version": "policy-v1"}
    proposal = proposals.create(
        "excel-etag",
        "excel-hash",
        "123",
        "chat",
        [proposed_change()],
        knowledge_binding=binding,
    )
    loaded = database.load_proposal(proposal.proposal_id)
    assert loaded.knowledge_binding == binding
    assert loaded.operations[0].change.knowledge_refs == [knowledge_ref()]

    without_wiki = deepcopy(proposal)
    without_wiki.knowledge_binding = {}
    without_wiki.operations[0].change.knowledge_refs = []
    assert loaded.digest != without_wiki.digest


def test_wiki_citations_cannot_create_a_proposal_without_a_binding(workflow) -> None:
    _, _, proposals = workflow
    with pytest.raises(ProposalError, match="immutable knowledge binding"):
        proposals.create(
            "excel-etag",
            "excel-hash",
            "123",
            "chat",
            [proposed_change()],
        )


def test_analyzer_keeps_excel_provenance_and_adds_wiki_citations() -> None:
    record = SourceRecord(
        key="SS-2026-001",
        sheet="2026",
        row=2,
        values={"현재상태": "출원"},
        source_refs=[source_ref()],
    )
    context = AnalysisContext(
        RecordChange(ChangeKind.UPDATE, record.key, record, record),
        knowledge_refs=(knowledge_ref(),),
    )
    analyzer = KnowledgeAwareAnalyzer()

    change = analyzer.proposed_change(
        context,
        database="한국 특허 사건",
        entity_key=record.key,
        property_name="현재상태",
        proposed_value="출원",
        current_value="착수",
        reason="지침과 원본을 함께 검토",
        confidence=0.9,
    )

    assert change is not None
    assert change.source_refs == [source_ref()]
    assert change.knowledge_refs == [knowledge_ref()]


def test_edit_is_bound_as_user_override_without_erasing_original_citation(workflow) -> None:
    database, _, proposals = workflow
    proposal = proposals.create(
        "excel-etag",
        "excel-hash",
        "123",
        "chat",
        [proposed_change()],
        knowledge_binding={"generation_digest": "generation-1"},
    )
    revised = proposals.edit(
        proposal.proposal_id,
        proposal.operations[0].change.operation_id,
        ProposalAction.EDIT,
        approved_value="보류",
    )
    loaded = database.load_proposal(revised.proposal_id)
    operation = loaded.operations[0]
    assert operation.user_override is True
    assert operation.approved_value == "보류"
    assert operation.change.knowledge_refs == [knowledge_ref()]
    assert loaded.digest != proposal.digest

    restored = proposals.edit(
        revised.proposal_id,
        operation.change.operation_id,
        ProposalAction.APPLY,
    )
    assert restored.operations[0].user_override is False
    assert restored.operations[0].approved_value == "출원"


@pytest.mark.parametrize("guard", [None, lambda: {"generation_digest": "changed"}])
def test_missing_or_mismatched_wiki_guard_marks_stale_without_write(
    workflow,
    guard,
) -> None:
    binding = {"generation_digest": "generation-1"}
    database, approval, _, proposal, receipt = approve_wiki_proposal(workflow, binding)
    writer = MemoryWriter()

    with pytest.raises(ApprovalError, match="Wiki sources changed"):
        ApprovalGatedApplyService(database, approval, writer).apply(
            receipt,
            "excel-drive",
            "excel-item",
            "excel-etag",
            "excel-hash",
            knowledge_binding_guard=guard,
        )

    assert writer.calls == 0
    assert writer.preflight_calls == 0
    assert database.load_proposal(proposal.proposal_id).status is ProposalStatus.STALE
    assert database.nonce_used(receipt.nonce) is False


def test_wiki_binding_is_checked_again_immediately_before_first_write(workflow) -> None:
    binding = {"generation_digest": "generation-1"}
    database, approval, _, proposal, receipt = approve_wiki_proposal(workflow, binding)
    bindings = iter([binding, {"generation_digest": "generation-2"}])
    writer = MemoryWriter()

    with pytest.raises(ApprovalError, match="Wiki sources changed"):
        ApprovalGatedApplyService(database, approval, writer).apply(
            receipt,
            "excel-drive",
            "excel-item",
            "excel-etag",
            "excel-hash",
            knowledge_binding_guard=lambda: next(bindings),
        )

    assert writer.calls == 0
    assert database.load_proposal(proposal.proposal_id).status is ProposalStatus.STALE
    assert database.nonce_used(receipt.nonce) is False


def test_exact_wiki_binding_allows_write_and_is_checked_before_each_write(workflow) -> None:
    binding = {"generation_digest": "generation-1"}
    database, approval, _, _, receipt = approve_wiki_proposal(workflow, binding)
    writer = MemoryWriter()
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        return binding

    report = ApprovalGatedApplyService(database, approval, writer).apply(
        receipt,
        "excel-drive",
        "excel-item",
        "excel-etag",
        "excel-hash",
        knowledge_binding_guard=guard,
    )

    assert report.committed is True
    assert writer.calls == 1
    assert checks == 3


def test_wiki_drift_after_a_partial_apply_is_failed_not_stale(workflow) -> None:
    binding = {"generation_digest": "generation-1"}
    database, approval, proposals = workflow
    second = deepcopy(proposed_change())
    second.operation_id = "operation-2"
    proposal = proposals.create(
        "excel-etag",
        "excel-hash",
        "123",
        "chat",
        [proposed_change(), second],
        knowledge_binding=binding,
    )
    proposal = proposals.request_approval(proposal.proposal_id)
    receipt = proposals.approve(
        proposal.proposal_id,
        proposal.revision,
        "123",
        "chat",
        proposal.digest,
    )
    bindings = iter([binding, binding, binding, {"generation_digest": "generation-2"}])
    writer = MemoryWriter()

    report = ApprovalGatedApplyService(database, approval, writer).apply(
        receipt,
        "excel-drive",
        "excel-item",
        "excel-etag",
        "excel-hash",
        knowledge_binding_guard=lambda: next(bindings),
    )

    assert report.committed is False
    assert writer.calls == 1
    assert report.applied == ["operation-1"]
    assert "operation-2" in report.failed
    assert database.load_proposal(proposal.proposal_id).status is ProposalStatus.FAILED
