from __future__ import annotations

from notion_excel_sync.domain.analyzer import AnalyzerManifest
from notion_excel_sync.knowledge.provider import AnalyzerKnowledgeProvider
from notion_excel_sync.models import ChangeKind, RecordChange, SourceRecord


def test_provider_query_omits_case_identity_and_keeps_safe_rule_dimensions() -> None:
    manifest = AnalyzerManifest(
        name="evidence",
        version="1",
        description="test",
        target_databases=("cases",),
        header_signals=("증빙", "지원사업"),
        knowledge_topics=("government-support-evidence",),
    )
    record = SourceRecord(
        key="CASE-SECRET-001",
        sheet="2026",
        row=2,
        values={
            "고객명": "홍길동",
            "담당자 이메일": "person@example.com",
            "사건번호": "CASE-SECRET-001",
            "지원사업명": "2026 혁신바우처",
            "사업연도": 2026,
            "출원국": "KR",
            "비고": "개인적인 사건 설명",
        },
        source_refs=[],
    )

    query = AnalyzerKnowledgeProvider._query_text(
        manifest,
        [RecordChange(ChangeKind.CREATE, record.key, None, record)],
    )

    assert "홍길동" not in query
    assert "person@example.com" not in query
    assert "CASE-SECRET-001" not in query
    assert "개인적인 사건 설명" not in query
    assert "2026 혁신바우처" in query
    assert "사업연도 2026" in query
    assert "출원국 KR" in query
