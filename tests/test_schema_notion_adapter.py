from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from notion_excel_sync.adapters.notion import (
    ApprovalRequiredError,
    HttpNotionGateway,
    NotionError,
)
from notion_excel_sync.models import SchemaApprovalReceipt, SchemaRecoveryReceipt


class _Response:
    def __init__(self, payload: object, *, headers: dict[str, str] | None = None) -> None:
        self._raw = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


NOW = datetime(2026, 7, 22, 9, tzinfo=UTC)


def _receipt(**overrides: object) -> SchemaApprovalReceipt:
    values: dict[str, object] = {
        "schema_proposal_id": "SP-schema-proposal-1",
        "revision": 1,
        "proposal_digest": "a" * 64,
        "precondition_digest": "b" * 64,
        "telegram_user_id": "user-1",
        "chat_id": "chat-1",
        "thread_id": "thread-1",
        "telegram_message_id": "message-1",
        "telegram_update_id": 1,
        "command_hash": "c" * 64,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "nonce": "nonce-1",
        "signature": "signature-1",
    }
    values.update(overrides)
    return SchemaApprovalReceipt(**values)  # type: ignore[arg-type]


def _recovery_receipt() -> SchemaRecoveryReceipt:
    return SchemaRecoveryReceipt(
        schema_proposal_id="SP-schema-proposal-1",
        revision=1,
        proposal_digest="a" * 64,
        precondition_digest="b" * 64,
        operation_id="schema-operation-1",
        raw_database_id="1" * 32,
        raw_data_source_id="2" * 32,
        canonical_database_id="1" * 32,
        canonical_data_source_id="2" * 32,
        attempt_started_at=NOW - timedelta(seconds=2),
        attempt_finished_at=NOW - timedelta(seconds=1),
        approval_receipt_nonce="approval-nonce",
        conflict_telegram_update_id=2,
        telegram_user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
        telegram_message_id="message-3",
        telegram_update_id=3,
        command_hash="d" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        nonce="recovery-nonce",
        signature="recovery-signature",
    )


def _create_body() -> dict[str, object]:
    properties: dict[str, object] = {
        "증빙명": {"title": {}},
        "증빙유형": {"select": {"options": []}},
        "인정 건수": {"number": {"format": "number"}},
        "산정방식": {"rich_text": {}},
        "실제 비용 합계": {"number": {"format": "won"}},
        "인정대상금액": {"number": {"format": "won"}},
        "정부지원금": {"number": {"format": "won"}},
        "자부담": {"number": {"format": "won"}},
        "부가세 처리": {"select": {"options": []}},
        "증빙상태": {
            "select": {
                "options": [
                    {"name": "증빙가능", "color": "green"},
                    {"name": "검토필요", "color": "yellow"},
                ]
            }
        },
        "부족액": {"number": {"format": "won"}},
        "초과액": {"number": {"format": "won"}},
        "중복증빙": {"checkbox": {}},
        "산정근거": {"rich_text": {}},
        "대상 사건": {"relation": {"data_source_id": "cases-source"}},
        "정부지원사업 그룹": {"relation": {"data_source_id": "groups-source"}},
        "관련 실제 비용": {"relation": {"data_source_id": "costs-source"}},
        "근거자료": {"relation": {"data_source_id": "evidence-source"}},
    }
    return {
        "parent": {"type": "page_id", "page_id": "parent-page"},
        "title": [{"type": "text", "text": {"content": "정부지원사업 증빙"}}],
        "description": [
            {"type": "text", "text": {"content": "[NX_SCHEMA:digest]"}}
        ],
        "is_inline": True,
        "initial_data_source": {"properties": properties},
    }


def test_get_self_uses_exact_users_me_route_and_version() -> None:
    calls: list[tuple[str, str, str]] = []

    def opener(request: object, *, timeout: float) -> _Response:
        assert timeout == 30.0
        calls.append(
            (
                request.get_method(),  # type: ignore[attr-defined]
                request.full_url,  # type: ignore[attr-defined]
                request.headers["Notion-version"],  # type: ignore[attr-defined]
            )
        )
        return _Response({"object": "user", "id": "bot-1", "type": "bot"})

    gateway = HttpNotionGateway(
        "read-token",
        lambda *_: False,
        base_url="https://notion.test/v1",
        opener=opener,
    )

    assert gateway.get_self()["id"] == "bot-1"
    assert calls == [("GET", "https://notion.test/v1/users/me", "2026-03-11")]


def test_list_block_children_follows_opaque_paginated_cursors() -> None:
    calls: list[tuple[str, str]] = []

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        method = request.get_method()  # type: ignore[attr-defined]
        url = request.full_url  # type: ignore[attr-defined]
        calls.append((method, url))
        query = parse_qs(urlparse(url).query)
        if "start_cursor" not in query:
            return _Response(
                {
                    "results": [{"id": "child-1", "type": "paragraph"}],
                    "has_more": True,
                    "next_cursor": "opaque/cursor+= value",
                }
            )
        assert query == {
            "page_size": ["2"],
            "start_cursor": ["opaque/cursor+= value"],
        }
        return _Response(
            {
                "results": [{"id": "database-1", "type": "child_database"}],
                "has_more": False,
                "next_cursor": None,
            }
        )

    gateway = HttpNotionGateway(
        "read-token",
        lambda *_: False,
        base_url="https://notion.test/v1",
        opener=opener,
    )

    children = gateway.list_block_children("parent/page", page_size=2)

    assert [item["id"] for item in children] == ["child-1", "database-1"]
    assert {method for method, _ in calls} == {"GET"}
    assert calls[0][1].startswith(
        "https://notion.test/v1/blocks/parent%2Fpage/children?page_size=2"
    )


def test_create_database_verifies_exact_scope_before_exact_post() -> None:
    expected_body = _create_body()
    precondition = {
        "parent_page_id": "parent-page",
        "expected_absent_title": "정부지원사업 증빙",
        "existing_child_database_ids": ["other-db"],
    }
    verifier_calls: list[tuple[object, str, object, object, str]] = []
    http_calls: list[tuple[str, str, object, str]] = []

    def verifier(
        receipt: SchemaApprovalReceipt,
        operation_id: str,
        body: object,
        actual_precondition: object,
        notion_version: str,
    ) -> bool:
        verifier_calls.append(
            (receipt, operation_id, body, actual_precondition, notion_version)
        )
        return (
            body == expected_body
            and actual_precondition == precondition
            and notion_version == "2026-03-11"
        )

    def opener(request: object, *, timeout: float) -> _Response:
        assert timeout == 30.0
        decoded = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        http_calls.append(
            (
                request.get_method(),  # type: ignore[attr-defined]
                request.full_url,  # type: ignore[attr-defined]
                decoded,
                request.headers["Notion-version"],  # type: ignore[attr-defined]
            )
        )
        return _Response(
            {
                "object": "database",
                "id": "database-1",
                "data_sources": [
                    {"id": "data-source-1", "name": "정부지원사업 증빙"}
                ],
            }
        )

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=verifier,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    result = gateway.create_database(
        expected_body,
        operation_id="schema-operation-1",
        approval=_receipt(),
        precondition=precondition,
    )

    assert result.database_id == "database-1"
    assert result.data_source_id == "data-source-1"
    assert verifier_calls == [
        (_receipt(), "schema-operation-1", expected_body, precondition, "2026-03-11")
    ]
    assert http_calls == [
        (
            "POST",
            "https://notion.test/v1/databases",
            expected_body,
            "2026-03-11",
        )
    ]


def test_create_database_rejects_recovery_receipt_before_network() -> None:
    calls: list[str] = []

    def opener(*_: object, **__: object) -> _Response:
        calls.append("network")
        return _Response({})

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    with pytest.raises(ApprovalRequiredError):
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=_recovery_receipt(),  # type: ignore[arg-type]
            precondition={"exact": True},
        )

    assert calls == []


def test_minimal_create_response_is_resolved_with_one_database_get() -> None:
    calls: list[tuple[str, str]] = []

    def opener(request: object, *, timeout: float) -> _Response:
        assert timeout == 30.0
        method = request.get_method()  # type: ignore[attr-defined]
        url = request.full_url  # type: ignore[attr-defined]
        calls.append((method, url))
        if method == "POST":
            return _Response({"object": "database", "id": "database/1"})
        assert method == "GET"
        return _Response(
            {
                "object": "database",
                "id": "database/1",
                "data_sources": [{"id": "data-source-1"}],
            }
        )

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    result = gateway.create_database(
        _create_body(),
        operation_id="schema-operation-1",
        approval=_receipt(),
        precondition={"expected_absent_title": "government support evidence"},
    )

    assert result.database_id == "database/1"
    assert result.data_source_id == "data-source-1"
    assert calls == [
        ("POST", "https://notion.test/v1/databases"),
        ("GET", "https://notion.test/v1/databases/database%2F1"),
    ]


@pytest.mark.parametrize(
    "lookup_payload",
    [
        {
            "object": "page",
            "id": "database-1",
            "data_sources": [{"id": "source-1"}],
        },
        {
            "object": "database",
            "id": "different-database",
            "data_sources": [{"id": "source-1"}],
        },
        {"object": "database", "id": "database-1", "data_sources": []},
        {
            "object": "database",
            "id": "database-1",
            "data_sources": [{"id": "source-1"}, {"id": "source-2"}],
        },
        {"object": "database", "id": "database-1", "data_sources": [{}]},
    ],
)
def test_malformed_minimal_create_lookup_is_unknown_without_reposting(
    lookup_payload: object,
) -> None:
    methods: list[str] = []

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        method = request.get_method()  # type: ignore[attr-defined]
        methods.append(method)
        if method == "POST":
            return _Response({"object": "database", "id": "database-1"})
        return _Response(lookup_payload)

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    with pytest.raises(NotionError) as caught:
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=_receipt(),
            precondition={"expected_absent_title": "government support evidence"},
        )

    assert caught.value.code == "invalid_response"
    assert caught.value.mutation_outcome_unknown is True
    assert methods == ["POST", "GET"]


def test_minimal_create_lookup_failure_is_safe_and_never_reposts() -> None:
    methods: list[str] = []

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        method = request.get_method()  # type: ignore[attr-defined]
        methods.append(method)
        if method == "POST":
            return _Response({"object": "database", "id": "database-1"})
        raise HTTPError(
            request.full_url,  # type: ignore[attr-defined]
            503,
            "sensitive gateway-write-token",
            {},
            io.BytesIO(b"sensitive gateway-write-token"),
        )

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    with pytest.raises(NotionError) as caught:
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=_receipt(),
            precondition={"expected_absent_title": "government support evidence"},
        )

    error = caught.value
    assert error.status == 503
    assert error.code == "verification_failed"
    assert error.mutation_outcome_unknown is True
    assert "gateway-write-token" not in str(error)
    assert "sensitive" not in str(error)
    assert error.__cause__ is None
    assert methods == ["POST", "GET"]


@pytest.mark.parametrize(
    "approval,verifier_result",
    [
        (_receipt(expires_at=NOW), True),
        (object(), True),
        (_receipt(), False),
    ],
)
def test_create_database_rejects_invalid_or_unverified_receipt_before_network(
    approval: object,
    verifier_result: bool,
) -> None:
    opener_calls = 0

    def opener(request: object, *, timeout: float) -> _Response:
        del request, timeout
        nonlocal opener_calls
        opener_calls += 1
        raise AssertionError("network must not be reached")

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: verifier_result,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    with pytest.raises(ApprovalRequiredError):
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=approval,  # type: ignore[arg-type]
            precondition={"expected_absent_title": "정부지원사업 증빙"},
        )

    assert opener_calls == 0


def test_started_exact_schema_operation_may_finish_after_receipt_ttl() -> None:
    class StartedVerifier:
        allows_expired_started_receipt = True

        def __call__(self, *args: object) -> bool:
            del args
            return True

    opener_calls = 0

    def opener(request: object, *, timeout: float) -> _Response:
        del request, timeout
        nonlocal opener_calls
        opener_calls += 1
        return _Response(
            {
                "object": "database",
                "id": "database-1",
                "data_sources": [{"id": "source-1"}],
            }
        )

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=StartedVerifier(),
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )
    result = gateway.create_database(
        _create_body(),
        operation_id="schema-operation-1",
        approval=_receipt(
            issued_at=NOW - timedelta(minutes=20),
            expires_at=NOW - timedelta(minutes=10),
        ),
        precondition={"expected_absent_title": "정부지원사업 증빙"},
    )
    assert result.database_id == "database-1"
    assert opener_calls == 1


def test_schema_receipt_allows_an_exact_empty_telegram_thread_binding() -> None:
    def opener(request: object, *, timeout: float) -> _Response:
        del request, timeout
        return _Response(
            {
                "object": "database",
                "id": "database-1",
                "data_sources": [{"id": "source-1"}],
            }
        )

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )
    result = gateway.create_database(
        _create_body(),
        operation_id="schema-operation-1",
        approval=_receipt(thread_id=""),
        precondition={"expected_absent_title": "정부지원사업 증빙"},
    )
    assert result.data_source_id == "source-1"


def test_schema_api_version_is_part_of_the_pre_network_verifier_scope() -> None:
    opener_calls = 0

    def opener(request: object, *, timeout: float) -> _Response:
        del request, timeout
        nonlocal opener_calls
        opener_calls += 1
        raise AssertionError("network must not be reached")

    def verifier(
        _receipt_value: object,
        _operation_id: str,
        _body: object,
        _precondition: object,
        notion_version: str,
    ) -> bool:
        return notion_version == "2026-03-11"

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=verifier,
        notion_version="2025-09-03",
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )
    with pytest.raises(ApprovalRequiredError):
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=_receipt(),
            precondition={"expected_absent_title": "정부지원사업 증빙"},
        )
    assert opener_calls == 0


def test_schema_http_error_is_structured_and_does_not_expose_remote_body() -> None:
    remote_body = {
        "object": "error",
        "status": 503,
        "code": "service_unavailable",
        "message": "sensitive remote details gateway-write-token",
    }

    def opener(request: object, *, timeout: float) -> _Response:
        del timeout
        raise HTTPError(
            request.full_url,  # type: ignore[attr-defined]
            503,
            "Service Unavailable gateway-write-token",
            {"x-request-id": "request-123"},
            io.BytesIO(json.dumps(remote_body).encode("utf-8")),
        )

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    with pytest.raises(NotionError) as caught:
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=_receipt(),
            precondition={"expected_absent_title": "정부지원사업 증빙"},
        )

    error = caught.value
    assert error.status == 503
    assert error.code == "service_unavailable"
    assert error.request_id == "request-123"
    assert error.mutation_outcome_unknown is True
    assert "gateway-write-token" not in str(error)
    assert "sensitive remote details" not in str(error)


def test_schema_network_error_is_unknown_and_does_not_expose_reason() -> None:
    def opener(request: object, *, timeout: float) -> _Response:
        del request, timeout
        raise URLError("connection failed near gateway-write-token")

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    with pytest.raises(NotionError) as caught:
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=_receipt(),
            precondition={"expected_absent_title": "정부지원사업 증빙"},
        )

    error = caught.value
    assert error.status is None
    assert error.code == "network_error"
    assert error.mutation_outcome_unknown is True
    assert "gateway-write-token" not in str(error)
    assert "connection failed" not in str(error)


def test_create_response_must_contain_exactly_one_data_source() -> None:
    def opener(request: object, *, timeout: float) -> _Response:
        del request, timeout
        return _Response(
            {
                "object": "database",
                "id": "database-1",
                "data_sources": [
                    {"id": "source-1"},
                    {"id": "source-2"},
                ],
            }
        )

    gateway = HttpNotionGateway(
        "gateway-write-token",
        lambda *_: False,
        schema_approval_verifier=lambda *_: True,
        base_url="https://notion.test/v1",
        opener=opener,
        now=lambda: NOW,
    )

    with pytest.raises(NotionError) as caught:
        gateway.create_database(
            _create_body(),
            operation_id="schema-operation-1",
            approval=_receipt(),
            precondition={"expected_absent_title": "정부지원사업 증빙"},
        )

    assert caught.value.code == "invalid_response"
    assert caught.value.mutation_outcome_unknown is True
