from __future__ import annotations

from copy import deepcopy
from itertools import permutations

from notion_excel_sync.models import (
    ChangeKind,
    KnowledgeRef,
    ProposedChange,
    ReviewItem,
    SourceRef,
    sha256_json,
)
from notion_excel_sync.persistence.database import StateDatabase
from notion_excel_sync.workflow.proposals import ProposalService
from notion_excel_sync.workflow.sync import SyncPreparationService


def _source_ref(*, row: int = 2, raw_value: object = "합성값") -> SourceRef:
    return SourceRef(
        drive_id="synthetic-local-source",
        item_id="synthetic-workbook",
        version_id="synthetic-version",
        file_hash="a" * 64,
        sheet="2026",
        row=row,
        cells=f"A{row}:C{row}",
        raw_value=raw_value,
    )


def _knowledge_ref(*, chunk: str = "paragraph:1#chunk:0") -> KnowledgeRef:
    return KnowledgeRef(
        drive_id="synthetic-wiki",
        item_id="synthetic-guidance",
        version_id="synthetic-generation",
        content_hash="b" * 64,
        chunk_locator=chunk,
        chunk_hash="c" * 64,
        security_classification="internal",
        display_name="합성 업무 지침",
    )


def _review(
    *,
    title: str = "동일한 사람이 읽는 제목",
    reason: str = "첫 번째 합성 근거",
    row: int = 2,
    current_value: object = "접수",
    proposed_value: object = "진행",
    knowledge_refs: list[KnowledgeRef] | None = None,
) -> ReviewItem:
    return ReviewItem(
        review_type="정책 충돌",
        title=title,
        reason=reason,
        current_value=current_value,
        proposed_value=proposed_value,
        confidence=0.75,
        source_refs=[_source_ref(row=row, raw_value=current_value)],
        knowledge_refs=list(knowledge_refs or []),
    )


def _service(database: StateDatabase) -> SyncPreparationService:
    service = SyncPreparationService.__new__(SyncPreparationService)
    service.database = database
    return service


def _legacy_key(review: ReviewItem, version_id: str = "workbook-v2") -> str:
    return "review:" + sha256_json(
        {
            "version": version_id,
            "title": review.title,
            "refs": [
                (ref.sheet, ref.row, ref.cells) for ref in review.source_refs
            ],
        }
    )[:20]


def _signature(changes: list[ProposedChange]) -> list[tuple[object, ...]]:
    return [
        (
            change.target_database,
            change.entity_key,
            change.property_name,
            change.proposed_value,
            change.confidence,
        )
        for change in changes
    ]


def test_same_visible_review_title_gets_distinct_notion_targets(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    reviews = [
        _review(),
        _review(
            reason="두 번째 합성 근거",
            row=3,
            current_value="보류",
            proposed_value={"status": "재검토"},
        ),
    ]

    changes = service._review_changes(reviews, "workbook-v2")
    entity_keys = {change.entity_key for change in changes}
    titles = {
        str(change.proposed_value)
        for change in changes
        if change.property_name == "검토항목"
    }

    assert len(entity_keys) == 2
    assert len(titles) == 2
    assert all(title.startswith("동일한 사람이 읽는 제목") for title in titles)
    assert any("2026!A2:C2" in title for title in titles)
    assert any("2026!A3:C3" in title for title in titles)

    # This used to raise a same-proposal mutation-scope conflict because both
    # entities had the same weak title match value.
    proposal = ProposalService(database).create(
        source_version_id="workbook-v2",
        source_file_hash="d" * 64,
        requested_by="synthetic-user",
        chat_id="synthetic-chat",
        changes=changes,
    )
    assert {operation.change.entity_key for operation in proposal.operations} == entity_keys


def test_exact_duplicate_reviews_deduplicate_to_one_stable_set(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    review = _review(knowledge_refs=[_knowledge_ref()])
    review.source_refs.append(_source_ref(row=3, raw_value="두 번째 합성값"))
    reordered = deepcopy(review)
    reordered.source_refs.reverse()

    duplicated = service._review_changes(
        [review, deepcopy(review), reordered],
        "workbook-v2",
    )
    repeated = service._review_changes([reordered], "workbook-v2")

    assert len(duplicated) == 7
    assert _signature(duplicated) == _signature(repeated)
    assert len({change.entity_key for change in duplicated}) == 1


def test_duplicate_citations_normalize_without_input_order_dependence(
    tmp_path,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    first = _review(knowledge_refs=[_knowledge_ref()])
    second_source = _source_ref(row=3, raw_value="두 번째 합성값")
    first.source_refs.extend(
        [
            second_source,
            deepcopy(first.source_refs[0]),
            deepcopy(second_source),
        ]
    )
    first.knowledge_refs.append(deepcopy(first.knowledge_refs[0]))
    reordered = deepcopy(first)
    reordered.source_refs.reverse()
    reordered.knowledge_refs.reverse()

    with_duplicates = service._review_changes([first], "workbook-v2")
    reordered_duplicates = service._review_changes(
        [reordered],
        "workbook-v2",
    )
    without_duplicates = deepcopy(first)
    without_duplicates.source_refs = [
        first.source_refs[0],
        second_source,
    ]
    without_duplicates.knowledge_refs = [first.knowledge_refs[0]]
    normalized = service._review_changes(
        [without_duplicates],
        "workbook-v2",
    )

    assert _signature(with_duplicates) == _signature(reordered_duplicates)
    assert _signature(with_duplicates) == _signature(normalized)
    assert len({change.entity_key for change in with_duplicates}) == 1
    assert all(len(change.source_refs) == 2 for change in with_duplicates)
    assert all(len(change.knowledge_refs) == 1 for change in with_duplicates)


def test_many_citation_orders_keep_legacy_mapping_lookups_bounded(
    tmp_path,
    monkeypatch,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    review = _review()
    review.source_refs.extend(
        [
            _source_ref(row=3, raw_value="두 번째 합성값"),
            _source_ref(row=4, raw_value="세 번째 합성값"),
        ]
    )
    variants: list[ReviewItem] = []
    for ordered_refs in permutations(review.source_refs):
        variant = deepcopy(review)
        variant.source_refs = list(ordered_refs)
        variants.append(variant)
    original_get_mapping = database.get_entity_mapping
    mapping_lookups: list[tuple[str, str]] = []

    def recording_get_mapping(
        database_name: str,
        entity_key: str,
    ) -> dict[str, str] | None:
        mapping_lookups.append((database_name, entity_key))
        return original_get_mapping(database_name, entity_key)

    monkeypatch.setattr(
        database,
        "get_entity_mapping",
        recording_get_mapping,
    )

    changes = service._review_changes(variants, "workbook-v2")

    assert len({change.entity_key for change in changes}) == 1
    assert len(mapping_lookups) <= 3
    assert len(mapping_lookups) == len(set(mapping_lookups))


def test_knowledge_reference_participates_in_review_identity(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)

    changes = service._review_changes(
        [
            _review(knowledge_refs=[_knowledge_ref(chunk="paragraph:1#chunk:0")]),
            _review(knowledge_refs=[_knowledge_ref(chunk="paragraph:2#chunk:0")]),
        ],
        "workbook-v2",
    )

    assert len({change.entity_key for change in changes}) == 2
    assert len(
        {
            change.proposed_value
            for change in changes
            if change.property_name == "검토항목"
        }
    ) == 2


def test_review_title_is_readable_and_bounded_to_notion_limit(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    human_prefix = "사람이 읽을 수 있는 검토 제목"

    changes = service._review_changes(
        [_review(title=f"{human_prefix} {'가' * 3_000}")],
        "workbook-v2",
    )
    title = next(
        str(change.proposed_value)
        for change in changes
        if change.property_name == "검토항목"
    )

    assert len(title) <= 2_000
    assert title.startswith(human_prefix)
    assert "2026!A2:C2" in title
    assert not title.startswith("review:")


def test_unambiguous_legacy_review_mapping_remains_exact(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    review = _review()
    legacy_key = _legacy_key(review)
    database.save_entity_mapping(
        "검토함",
        legacy_key,
        "synthetic-existing-review-page",
        "기존 검토 제목",
    )

    changes = service._review_changes([review], "workbook-v2")

    assert {change.entity_key for change in changes} == {legacy_key}
    assert next(
        change.proposed_value
        for change in changes
        if change.property_name == "검토항목"
    ) == "기존 검토 제목"


def test_reordered_duplicate_reuses_its_single_legacy_mapping(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    review = _review()
    review.source_refs.append(_source_ref(row=3, raw_value="두 번째 합성값"))
    reordered = deepcopy(review)
    reordered.source_refs.reverse()
    first_legacy_key = _legacy_key(review)
    second_legacy_key = _legacy_key(reordered)
    assert first_legacy_key != second_legacy_key
    database.save_entity_mapping(
        "검토함",
        second_legacy_key,
        "synthetic-reordered-review-page",
        "순서 변경 전 기존 제목",
    )

    changes = service._review_changes(
        [review, reordered],
        "workbook-v2",
    )

    assert {change.entity_key for change in changes} == {second_legacy_key}
    assert next(
        change.proposed_value
        for change in changes
        if change.property_name == "검토항목"
    ) == "순서 변경 전 기존 제목"


def test_single_reordered_review_reuses_canonical_legacy_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    original = _review()
    original.source_refs.append(
        _source_ref(row=3, raw_value="두 번째 합성값")
    )
    reordered = deepcopy(original)
    reordered.source_refs.reverse()
    canonical_legacy_key = _legacy_key(original)
    current_order_key = _legacy_key(reordered)
    assert canonical_legacy_key != current_order_key
    database.save_entity_mapping(
        "검토함",
        canonical_legacy_key,
        "synthetic-canonical-review-page",
        "표준 순서로 생성된 기존 제목",
    )
    original_get_mapping = database.get_entity_mapping
    mapping_lookups: list[tuple[str, str]] = []

    def recording_get_mapping(
        database_name: str,
        entity_key: str,
    ) -> dict[str, str] | None:
        mapping_lookups.append((database_name, entity_key))
        return original_get_mapping(database_name, entity_key)

    monkeypatch.setattr(
        database,
        "get_entity_mapping",
        recording_get_mapping,
    )

    changes = service._review_changes([reordered], "workbook-v2")

    assert {change.entity_key for change in changes} == {
        canonical_legacy_key
    }
    assert next(
        change.proposed_value
        for change in changes
        if change.property_name == "검토항목"
    ) == "표준 순서로 생성된 기존 제목"
    assert len(mapping_lookups) == 3
    assert len(set(mapping_lookups)) == 3


def test_single_reordered_review_conflicting_legacy_candidates_fail_closed(
    tmp_path,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    original = _review()
    original.source_refs.append(
        _source_ref(row=3, raw_value="두 번째 합성값")
    )
    reordered = deepcopy(original)
    reordered.source_refs.reverse()
    legacy_keys = {_legacy_key(original), _legacy_key(reordered)}
    assert len(legacy_keys) == 2
    for index, legacy_key in enumerate(sorted(legacy_keys), start=1):
        database.save_entity_mapping(
            "검토함",
            legacy_key,
            f"synthetic-conflicting-single-page-{index}",
            f"충돌하는 단일 입력 제목 {index}",
        )

    changes = service._review_changes([reordered], "workbook-v2")
    entity_keys = {change.entity_key for change in changes}
    title = next(
        str(change.proposed_value)
        for change in changes
        if change.property_name == "검토항목"
    )

    assert len(entity_keys) == 1
    assert entity_keys.isdisjoint(legacy_keys)
    assert title.startswith(original.title)
    assert "충돌하는 단일 입력 제목" not in title


def test_conflicting_reordered_legacy_mappings_are_not_reused(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    review = _review()
    review.source_refs.append(_source_ref(row=3, raw_value="두 번째 합성값"))
    reordered = deepcopy(review)
    reordered.source_refs.reverse()
    legacy_keys = {_legacy_key(review), _legacy_key(reordered)}
    assert len(legacy_keys) == 2
    for index, legacy_key in enumerate(sorted(legacy_keys), start=1):
        database.save_entity_mapping(
            "검토함",
            legacy_key,
            f"synthetic-conflicting-review-page-{index}",
            f"서로 다른 기존 제목 {index}",
        )

    changes = service._review_changes(
        [review, reordered],
        "workbook-v2",
    )
    entity_keys = {change.entity_key for change in changes}
    title = next(
        str(change.proposed_value)
        for change in changes
        if change.property_name == "검토항목"
    )

    assert len(entity_keys) == 1
    assert entity_keys.isdisjoint(legacy_keys)
    assert title.startswith(review.title)
    assert "서로 다른 기존 제목" not in title


def test_history_titles_are_readable_append_only_and_operation_unique() -> None:
    def change(operation_id: str) -> ProposedChange:
        return ProposedChange(
            target_database="한국 특허 사건",
            entity_key="SS-SYNTHETIC-001",
            property_name="현재상태",
            kind=ChangeKind.UPDATE,
            current_value="접수",
            proposed_value="진행",
            analyzer="synthetic",
            analyzer_version="1",
            confidence=1.0,
            reason="합성 변경",
            source_refs=[_source_ref()],
            operation_id=operation_id,
        )

    first = SyncPreparationService._history_changes([change("operation-one")])
    repeated = SyncPreparationService._history_changes([change("operation-one")])
    second = SyncPreparationService._history_changes([change("operation-two")])
    first_title = next(
        item.proposed_value for item in first if item.property_name == "변경명"
    )
    repeated_title = next(
        item.proposed_value for item in repeated if item.property_name == "변경명"
    )
    second_title = next(
        item.proposed_value for item in second if item.property_name == "변경명"
    )

    assert first_title == repeated_title
    assert first_title != second_title
    assert "SS-SYNTHETIC-001 · 현재상태" in str(first_title)
    assert "operation-one" in str(first_title)
    assert len(str(first_title)) <= 2_000


def test_only_deferred_history_is_refreshed_before_a_new_proposal(tmp_path) -> None:
    class RecordingReader:
        def __init__(self) -> None:
            self.calls: list[set[tuple[str, str, str]]] = []

        def load(self, keys, title_values):
            del title_values
            captured = set(keys)
            self.calls.append(captured)
            return {key: "existing synthetic value" for key in captured}

    def change(operation_id: str) -> ProposedChange:
        return ProposedChange(
            target_database="한국 특허 사건",
            entity_key=f"case:{operation_id}",
            property_name="현재상태",
            kind=ChangeKind.UPDATE,
            current_value="접수",
            proposed_value="진행",
            analyzer="synthetic",
            analyzer_version="1",
            confidence=1.0,
            reason="합성 변경",
            source_refs=[_source_ref()],
            operation_id=operation_id,
        )

    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    service = _service(database)
    reader = RecordingReader()
    service.notion_reader = reader
    ordinary = change("ordinary-operation")
    service._refresh_current_values([ordinary])

    fresh_history = service._history_changes([change("fresh-operation")])
    pending_history = service._history_changes([change("pending-operation")])
    history_changes = [*fresh_history, *pending_history]
    service._refresh_pending_history_values(history_changes, pending_history)

    assert reader.calls[0] == {
        ("한국 특허 사건", "case:ordinary-operation", "현재상태")
    }
    assert reader.calls[1]
    assert all(key[0] == "변경이력" for key in reader.calls[1])
    assert all(key[1] == "history:pending-operation" for key in reader.calls[1])
    assert all(change.current_value is None for change in fresh_history)
    assert all(
        change.current_value == "existing synthetic value"
        for change in pending_history
    )
