from __future__ import annotations

import json
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest

from notion_excel_sync.ai.cache import AIAnalysisCache
from notion_excel_sync.ai.models import (
    AIProviderResponse,
    AIRequest,
    AISchemaError,
    validate_analysis,
)
from notion_excel_sync.ai.provider import (
    AIProviderError,
    AIProviderUnavailable,
    HermesStructuredAIProvider,
)
from notion_excel_sync.ai.service import StructuredAIService
from notion_excel_sync.domain.router import AnalysisRun, RoutingDecision
from notion_excel_sync.models import (
    AnalyzerResult,
    ChangeKind,
    ProposedChange,
    RecordChange,
    ReviewItem,
    SourceRecord,
    SourceRef,
)


class GroundedProvider:
    def __init__(self, *, party_type: str = "개인") -> None:
        self.calls = 0
        self.party_type = party_type

    def analyze(self, request: AIRequest) -> AIProviderResponse:
        self.calls += 1
        if request.task_type == "party_resolution":
            claims = [
                {
                    "field": "canonical_party_name",
                    "value": "홍길동",
                    "confidence": 0.99,
                    "inference_type": "extract",
                    "evidence": [
                        {"location": "의뢰인", "quote": "홍길동"}
                    ],
                },
                {
                    "field": "party_type",
                    "value": self.party_type,
                    "confidence": 0.98,
                    "inference_type": "classify",
                    "evidence": [
                        {"location": "의뢰인", "quote": "홍길동"}
                    ],
                },
            ]
        else:
            claims = [
                {
                    "field": "summary",
                    "value": "검토가 필요한 변경",
                    "confidence": 0.91,
                    "inference_type": "summarize",
                    "evidence": [
                        {"location": "업무", "quote": "검토 요청"}
                    ],
                }
            ]
        return AIProviderResponse(
            payload={
                "schema_version": "1.0",
                "task_type": request.task_type,
                "source_hash": request.source_hash,
                "summary": "원본 근거가 있는 구조화 분석",
                "claims": claims,
                "conflicts": [],
                "needs_review": False,
                "overall_confidence": 0.98,
            },
            provider="fake-local",
            model="fake-model",
        )


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, request: AIRequest) -> AIProviderResponse:
        self.calls += 1
        raise AIProviderError("provider unavailable")


def _source_ref() -> SourceRef:
    return SourceRef(
        drive_id="local",
        item_id="local:fixture",
        version_id="sha256:fixture",
        file_hash="a" * 64,
        sheet="Cases",
        row=2,
        cells="A2:B2",
    )


def _party_run() -> AnalysisRun:
    record = SourceRecord(
        key="CASE-1",
        sheet="Cases",
        row=2,
        values={"의뢰인": "홍길동"},
        source_refs=[_source_ref()],
    )
    change = RecordChange(ChangeKind.CREATE, record.key, None, record)
    original = ProposedChange(
        target_database="당사자",
        entity_key="party:홍길동",
        property_name="구분",
        kind=ChangeKind.CREATE,
        current_value=None,
        proposed_value="기타",
        analyzer="party",
        analyzer_version="1.1.0",
        confidence=0.6,
        reason="규칙만으로 구분하지 못함",
        source_refs=[_source_ref()],
    )
    result = AnalyzerResult(
        analyzer="party",
        version="1.1.0",
        proposed_changes=[original],
        review_items=[
            ReviewItem(
                review_type="당사자 구분",
                title="홍길동 구분 확인",
                reason="규칙만으로 구분하지 못함",
                confidence=0.6,
                source_refs=[_source_ref()],
            )
        ],
    )
    return AnalysisRun(
        change=change,
        routing=RoutingDecision(
            record_key=record.key,
            analyzer_names=("party",),
            scores={"party": 3},
            matched_signals={"party": ("header:의뢰인",)},
        ),
        results=[result],
    )


def _unrouted_run() -> AnalysisRun:
    record = SourceRecord(
        key="ROW-1",
        sheet="Cases",
        row=3,
        values={"업무": "검토 요청"},
        source_refs=[_source_ref()],
    )
    return AnalysisRun(
        change=RecordChange(ChangeKind.CREATE, record.key, None, record),
        routing=RoutingDecision(
            record_key=record.key,
            analyzer_names=(),
            scores={},
            matched_signals={},
        ),
    )


def test_validator_rejects_hallucinated_evidence() -> None:
    request = AIRequest(
        task_type="excel_row_semantics",
        source_hash="b" * 64,
        source_type="excel",
        source_locator="Cases:2",
        payload={"fields": [{"name": "업무", "value": "검토 요청"}]},
        evidence_corpus="업무: 검토 요청",
    )
    response = AIProviderResponse(
        payload={
            "schema_version": "1.0",
            "task_type": "excel_row_semantics",
            "source_hash": "b" * 64,
            "summary": "근거 없는 분석",
            "claims": [
                {
                    "field": "summary",
                    "value": "등록결정",
                    "confidence": 0.99,
                    "inference_type": "summarize",
                    "evidence": [
                        {"location": "업무", "quote": "원본에 없는 문장"}
                    ],
                }
            ],
            "conflicts": [],
            "needs_review": False,
            "overall_confidence": 0.99,
        },
        provider="fake",
        model="fake",
    )

    with pytest.raises(AISchemaError, match="absent"):
        validate_analysis(request, response)


def test_local_only_provider_refuses_external_auto_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False
    auxiliary = ModuleType("agent.auxiliary_client")

    def call_llm(**_: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("The external model must not be called")

    auxiliary.call_llm = call_llm  # type: ignore[attr-defined]
    auxiliary._resolve_task_provider_model = (  # type: ignore[attr-defined]
        lambda *_: ("auto", None, None, None, None)
    )
    auxiliary._get_cached_client = (  # type: ignore[attr-defined]
        lambda *_args, **_kwargs: (
            SimpleNamespace(base_url="https://external.example/v1"),
            "external-model",
        )
    )
    agent = ModuleType("agent")
    agent.__path__ = []  # type: ignore[attr-defined]
    agent.auxiliary_client = auxiliary  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)

    request = AIRequest(
        task_type="excel_row_semantics",
        source_hash="c" * 64,
        source_type="excel",
        source_locator="Cases:2",
        payload={"fields": [{"name": "업무", "value": "검토 요청"}]},
        evidence_corpus="업무: 검토 요청",
    )
    with pytest.raises(AIProviderUnavailable, match="non-local"):
        HermesStructuredAIProvider(privacy_mode="local_only").analyze(request)
    assert not called


def test_local_provider_receives_only_non_executing_submit_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    auxiliary = ModuleType("agent.auxiliary_client")

    def call_llm(**kwargs: object) -> object:
        captured.update(kwargs)
        request_payload = json.loads(
            kwargs["messages"][1]["content"]  # type: ignore[index]
        )
        payload = {
            "schema_version": "1.0",
            "task_type": "excel_row_semantics",
            "source_hash": request_payload["SOURCE_HASH"],
            "summary": "검토 요청 행",
            "claims": [
                {
                    "field": "summary",
                    "value": "검토 요청",
                    "confidence": 0.9,
                    "inference_type": "summarize",
                    "evidence": [{"location": "업무", "quote": "검토 요청"}],
                }
            ],
            "conflicts": [],
            "needs_review": True,
            "overall_confidence": 0.9,
        }
        return SimpleNamespace(
            model="local-model",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        tool_calls=[],
                        content=json.dumps(payload, ensure_ascii=False),
                    )
                )
            ],
        )

    auxiliary.call_llm = call_llm  # type: ignore[attr-defined]
    auxiliary._resolve_task_provider_model = (  # type: ignore[attr-defined]
        lambda *_: ("custom", "local-model", "http://127.0.0.1:11434/v1", None, None)
    )
    auxiliary._get_cached_client = (  # type: ignore[attr-defined]
        lambda *_args, **_kwargs: (None, None)
    )
    agent = ModuleType("agent")
    agent.__path__ = []  # type: ignore[attr-defined]
    agent.auxiliary_client = auxiliary  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)

    request = AIRequest(
        task_type="excel_row_semantics",
        source_hash="d" * 64,
        source_type="excel",
        source_locator="Cases:2",
        payload={"fields": [{"name": "업무", "value": "검토 요청"}]},
        evidence_corpus="업무: 검토 요청",
    )
    response = HermesStructuredAIProvider(
        privacy_mode="local_only"
    ).analyze(request)

    assert response.model == "local-model"
    tools = captured["tools"]
    assert isinstance(tools, list)
    assert [item["function"]["name"] for item in tools] == [  # type: ignore[index]
        "submit_structured_analysis"
    ]


def test_cache_is_bound_to_exact_request_digest(tmp_path: Path) -> None:
    provider = GroundedProvider()
    service = StructuredAIService(
        provider=provider,
        cache=AIAnalysisCache(tmp_path / "analysis.db"),
        rollout_mode="shadow",
    )

    first = service.enrich_excel_runs([_party_run()])
    second = service.enrich_excel_runs([_party_run()])

    assert first.completed == 1
    assert first.shadow_results == 1
    assert second.completed == 1
    assert second.cache_hits == 1
    assert provider.calls == 1
    with sqlite3.connect(tmp_path / "analysis.db") as connection:
        stored = json.loads(
            connection.execute(
                "SELECT output_json FROM analysis_cache"
            ).fetchone()[0]
        )
    assert stored["request_digest"]
    assert stored["prompt_version"] == "structured-analysis-v1"
    assert stored["model"] == "fake-model"


def test_cache_rejects_tampered_output(tmp_path: Path) -> None:
    provider = GroundedProvider()
    cache = AIAnalysisCache(tmp_path / "analysis.db")
    service = StructuredAIService(
        provider=provider,
        cache=cache,
        rollout_mode="shadow",
    )
    service.enrich_excel_runs([_party_run()])
    with sqlite3.connect(tmp_path / "analysis.db") as connection:
        connection.execute(
            "UPDATE analysis_cache SET output_digest = ?",
            ("0" * 64,),
        )
        request_digest = connection.execute(
            "SELECT request_digest FROM analysis_cache"
        ).fetchone()[0]
    with pytest.raises(ValueError, match="output digest"):
        cache.get(request_digest)


def test_provider_failure_opens_circuit_and_keeps_rules_running(tmp_path: Path) -> None:
    provider = FailingProvider()
    service = StructuredAIService(
        provider=provider,
        cache=AIAnalysisCache(tmp_path / "analysis.db"),
        rollout_mode="assist",
    )

    report = service.enrich_excel_runs([_party_run(), _unrouted_run()])

    assert provider.calls == 1
    assert report.failures == 1
    assert report.skipped == 1
    assert not report.proposed_changes
    assert not report.review_items


def test_assist_mode_materializes_review_only(tmp_path: Path) -> None:
    service = StructuredAIService(
        provider=GroundedProvider(),
        cache=AIAnalysisCache(tmp_path / "analysis.db"),
        rollout_mode="assist",
    )

    report = service.enrich_excel_runs([_party_run()])

    assert not report.proposed_changes
    assert not report.replaced_operation_ids
    assert len(report.review_items) == 1
    assert report.review_items[0].review_type == "AI 구조화 분석"
    assert report.review_items[0].confidence == 0.98


def test_verified_mode_replaces_only_the_party_type_candidate(tmp_path: Path) -> None:
    run = _party_run()
    original = run.results[0].proposed_changes[0]
    service = StructuredAIService(
        provider=GroundedProvider(party_type="개인"),
        cache=AIAnalysisCache(tmp_path / "analysis.db"),
        rollout_mode="verified",
    )

    report = service.enrich_excel_runs([run])

    assert report.replaced_operation_ids == {original.operation_id}
    assert len(report.proposed_changes) == 1
    replacement = report.proposed_changes[0]
    assert replacement.proposed_value == "개인"
    assert replacement.analyzer == "llm_party_resolution"
    assert replacement.source_refs == original.source_refs
    assert not report.review_items
