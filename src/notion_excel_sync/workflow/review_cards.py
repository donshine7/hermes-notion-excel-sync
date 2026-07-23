from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from notion_excel_sync.models import (
    CORRECTION_OVERLAY_RECOVERY_MODE,
    JsonValue,
    KnowledgeRef,
    ProposalAction,
    ProposalOperation,
    ProposalPurpose,
    ProposalRevision,
    SourceRef,
)


DEFAULT_CARDS_PER_PAGE = 5
HISTORY_DATABASE = "변경이력"
_CASE_NUMBER_PROPERTIES = frozenset(
    {
        "당소사건번호",
        "사건번호",
        "관리번호",
        "케이스번호",
        "출원번호",
        "증빙사건번호",
    }
)


class ReviewCardPageError(ValueError):
    """Raised when a requested logical-card page does not exist."""


@dataclass(frozen=True, slots=True)
class ReviewCardCommands:
    apply: str
    exclude: str
    defer: str
    edit: str


@dataclass(frozen=True, slots=True)
class ReviewCard:
    """A pure Telegram-facing projection of one editable proposal operation."""

    number: int
    operation_id: str
    automatic_operation_ids: tuple[str, ...]
    case_number: str
    entity_key: str
    target_database: str
    property_name: str
    action: ProposalAction
    current_value: JsonValue
    approved_value: JsonValue
    summary: str
    change_basis: str
    excel_source_refs: tuple[str, ...]
    knowledge_refs: tuple[str, ...]
    wiki_impact: str
    automatic_notion_impact: str
    commands: ReviewCardCommands


@dataclass(frozen=True, slots=True)
class ReviewCardPage:
    proposal_id: str
    revision: int
    status: str
    digest: str
    page: int
    total_pages: int
    total_cards: int
    cards_per_page: int
    cards: tuple[ReviewCard, ...]
    automatic_history_operations: int
    unmerged_history_operations: int
    next_command: str | None
    reject_command: str
    approve_command: str | None


def build_review_cards(proposal: ProposalRevision) -> tuple[ReviewCard, ...]:
    """Project a proposal into logical cards without changing approval state.

    Generated ``변경이력`` operations are effects of their originating operation,
    not separately editable logical changes. They are therefore attached to the
    source card and excluded from pagination.
    """

    main_operations = [
        operation
        for operation in proposal.operations
        if operation.change.target_database != HISTORY_DATABASE
    ]
    history_by_source_id = _history_operations_by_source_id(proposal.operations)
    case_by_source_position = _case_numbers_by_source_position(main_operations)
    overlay_only_recovery = _is_overlay_only_correction_recovery(proposal)

    cards: list[ReviewCard] = []
    for number, operation in enumerate(main_operations, start=1):
        change = operation.change
        automatic = tuple(
            item.change.operation_id
            for item in history_by_source_id.get(change.operation_id, ())
        )
        case_number = _case_number(operation, case_by_source_position)
        excel_refs = summarize_excel_source_refs(change.source_refs)
        knowledge_refs = summarize_knowledge_refs(change.knowledge_refs)
        cards.append(
            ReviewCard(
                number=number,
                operation_id=change.operation_id,
                automatic_operation_ids=automatic,
                case_number=case_number,
                entity_key=change.entity_key,
                target_database=change.target_database,
                property_name=change.property_name,
                action=operation.action,
                current_value=change.current_value,
                approved_value=operation.approved_value,
                summary=_summary(
                    operation,
                    case_number,
                    overlay_only_recovery=overlay_only_recovery,
                ),
                change_basis=_change_basis(
                    change.reason,
                    excel_refs=excel_refs,
                    knowledge_refs=knowledge_refs,
                ),
                excel_source_refs=excel_refs,
                knowledge_refs=knowledge_refs,
                wiki_impact=_wiki_impact(proposal, operation),
                automatic_notion_impact=(
                    f"변경이력 {len(automatic)}개 operation 자동 동반"
                    if automatic
                    else "자동 동반 operation 없음"
                ),
                commands=_commands(proposal, change.operation_id),
            )
        )
    return tuple(cards)


def review_card_page(
    proposal: ProposalRevision,
    page: int = 1,
    *,
    cards_per_page: int = DEFAULT_CARDS_PER_PAGE,
) -> ReviewCardPage:
    """Return one deterministic five-card page by default."""

    if cards_per_page < 1:
        raise ValueError("cards_per_page must be positive")
    cards = build_review_cards(proposal)
    total_pages = max(1, (len(cards) + cards_per_page - 1) // cards_per_page)
    if page < 1 or page > total_pages:
        raise ReviewCardPageError(
            f"Page {page} does not exist; valid pages are 1-{total_pages}"
        )
    start = (page - 1) * cards_per_page
    selected = cards[start : start + cards_per_page]
    history_count = sum(
        operation.change.target_database == HISTORY_DATABASE
        for operation in proposal.operations
    )
    merged_history_count = sum(
        len(card.automatic_operation_ids) for card in cards
    )
    unmerged_history_count = len(
        unmerged_history_operation_ids(proposal.operations)
    )
    if history_count - merged_history_count != unmerged_history_count:
        raise AssertionError("History review-card accounting is inconsistent")
    return ReviewCardPage(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision,
        status=proposal.status.value,
        digest=proposal.digest,
        page=page,
        total_pages=total_pages,
        total_cards=len(cards),
        cards_per_page=cards_per_page,
        cards=selected,
        automatic_history_operations=history_count,
        unmerged_history_operations=unmerged_history_count,
        next_command=(
            f"/nx_show {proposal.proposal_id} {proposal.revision} {page + 1}"
            if page < total_pages
            else None
        ),
        reject_command=f"/nx_reject {proposal.proposal_id} {proposal.revision}",
        approve_command=(
            None
            if unmerged_history_count
            else (
                f"/nx_approve {proposal.proposal_id} "
                f"{proposal.revision} {proposal.digest}"
            )
        ),
    )


def render_review_card_page(
    proposal: ProposalRevision,
    page: int = 1,
    *,
    cards_per_page: int = DEFAULT_CARDS_PER_PAGE,
) -> str:
    """Render one concise Telegram review page from a proposal revision."""

    projection = review_card_page(
        proposal,
        page,
        cards_per_page=cards_per_page,
    )
    overlay_only_recovery = _is_overlay_only_correction_recovery(proposal)
    lines = [
        f"[NX_PROPOSAL_REVIEW {projection.proposal_id} r{projection.revision}]",
        f"상태: {projection.status}",
        f"Digest: {projection.digest}",
        (
            f"페이지: {projection.page}/{projection.total_pages} "
            f"(논리 변경 {projection.total_cards}건, 페이지당 {projection.cards_per_page}건)"
        ),
        (
            "자동 변경이력: "
            f"{projection.automatic_history_operations}개 "
            f"(미병합 {projection.unmerged_history_operations}개)"
        ),
    ]
    if not projection.cards:
        lines.extend(["", "검토할 논리 변경 카드가 없습니다."])
    for card in projection.cards:
        lines.extend(
            [
                "",
                f"{card.number}. 사건번호: {card.case_number}",
                f"요약: {card.summary}",
                f"대상: {card.target_database} / {card.property_name}",
            ]
        )
        if overlay_only_recovery:
            lines.extend(
                [
                    (
                        "Notion: 이전 승인에서 "
                        f"{_render_value(card.approved_value)} 반영 완료 "
                        "(이번 승인에서는 재실행하지 않음)"
                    ),
                    "이번 승인 대상: Wiki 승인 오버레이와 최종화 복구",
                ]
            )
        else:
            lines.extend(
                [
                    (
                        "Notion: "
                        f"{_render_value(card.current_value)} → "
                        f"{_render_value(card.approved_value)}"
                    ),
                    f"현재 처리: {card.action.value}",
                ]
            )
        lines.extend(
            [
                f"변경근거: {card.change_basis}",
                (
                    "Excel 근거: "
                    + (
                        "; ".join(card.excel_source_refs)
                        if card.excel_source_refs
                        else "없음"
                    )
                ),
                (
                    "Wiki 근거: "
                    + ("; ".join(card.knowledge_refs) if card.knowledge_refs else "없음")
                ),
                f"Wiki 영향: {card.wiki_impact}",
                f"자동 영향: {card.automatic_notion_impact}",
                f"Operation ID: {card.operation_id}",
            ]
        )
        if overlay_only_recovery:
            lines.append("이 복구안은 수정·거부·보류할 수 없습니다.")
        else:
            lines.extend(
                [
                    "수정 명령:",
                    card.commands.apply,
                    card.commands.exclude,
                    card.commands.defer,
                    card.commands.edit,
                ]
            )
    if projection.next_command:
        lines.extend(["", f"다음 페이지: {projection.next_command}"])
    if projection.approve_command is None:
        lines.extend(
            [
                "",
                (
                    "승인 불가: 검토 카드에 연결되지 않은 변경이력 operation "
                    f"{projection.unmerged_history_operations}개가 있습니다."
                ),
                "표시되지 않은 변경이 있는 제안에는 승인 명령을 제공하지 않습니다.",
            ]
        )
        if not overlay_only_recovery:
            lines.append(f"거부: {projection.reject_command}")
    elif overlay_only_recovery:
        lines.extend(
            [
                "",
                "허용 명령(현재 전체 digest와 정확히 일치해야 함):",
                projection.approve_command,
                "Notion 쓰기는 이전 승인에서 이미 완료되었습니다.",
                (
                    "이번 승인은 Notion을 다시 쓰지 않고 Wiki 승인 오버레이와 "
                    "최종화만 복구합니다."
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"거부: {projection.reject_command}",
                "최종 승인(현재 전체 digest와 정확히 일치해야 함):",
                projection.approve_command,
                "Notion과 Wiki는 아직 변경되지 않았습니다.",
            ]
        )
    return "\n".join(lines)


def summarize_excel_source_refs(refs: Iterable[SourceRef]) -> tuple[str, ...]:
    """Group cell-level refs into concise, stable sheet/row citations."""

    grouped: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    for ref in refs:
        grouped[(ref.sheet, ref.row, ref.version_id)].add(ref.cells)
    summaries = []
    for (sheet, row, version_id), cells in sorted(grouped.items()):
        cell_text = ", ".join(sorted(cell for cell in cells if cell)) or "셀 미상"
        summaries.append(
            f"{sheet} 행 {row} [{cell_text}] ({_version_label(version_id)})"
        )
    return tuple(summaries)


def summarize_knowledge_refs(refs: Iterable[KnowledgeRef]) -> tuple[str, ...]:
    """Return stable Wiki citations without exposing passage contents."""

    citations = {
        (
            ref.display_name or ref.item_id or "문서 미상",
            ref.version_id,
            ref.chunk_locator or "위치 미상",
            ref.chunk_hash[:12],
        )
        for ref in refs
    }
    return tuple(
        (
            f"{name} ({_version_label(version_id)}, {locator}"
            f"{f', #{chunk_hash}' if chunk_hash else ''})"
        )
        for name, version_id, locator, chunk_hash in sorted(citations)
    )


def unmerged_history_operation_ids(
    operations: Iterable[ProposalOperation],
) -> tuple[str, ...]:
    """Return generated history operations that no visible card can explain."""

    operation_list = tuple(operations)
    visible_operation_ids = {
        operation.change.operation_id
        for operation in operation_list
        if operation.change.target_database != HISTORY_DATABASE
    }
    unmerged: list[str] = []
    for operation in operation_list:
        change = operation.change
        if change.target_database != HISTORY_DATABASE:
            continue
        prefix, separator, source_operation_id = change.entity_key.partition(":")
        if (
            not separator
            or prefix != "history"
            or source_operation_id not in visible_operation_ids
        ):
            unmerged.append(change.operation_id)
    return tuple(unmerged)


def _history_operations_by_source_id(
    operations: Iterable[ProposalOperation],
) -> dict[str, tuple[ProposalOperation, ...]]:
    grouped: dict[str, list[ProposalOperation]] = defaultdict(list)
    for operation in operations:
        change = operation.change
        if change.target_database != HISTORY_DATABASE:
            continue
        prefix, separator, source_operation_id = change.entity_key.partition(":")
        if separator and prefix == "history" and source_operation_id:
            grouped[source_operation_id].append(operation)
    return {key: tuple(value) for key, value in grouped.items()}


def _case_numbers_by_source_position(
    operations: Iterable[ProposalOperation],
) -> dict[tuple[str, int], str]:
    candidates: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)
    for operation in operations:
        change = operation.change
        if _normalized_property(change.property_name) not in _CASE_NUMBER_PROPERTIES:
            continue
        value = change.entity_key.strip() or _case_value(operation.approved_value)
        if not value:
            value = _case_value(change.proposed_value)
        if not value:
            continue
        for position in _source_positions(change.source_refs):
            candidates[position].append((change.operation_id, value))
    return {
        position: sorted(values)[0][1]
        for position, values in candidates.items()
    }


def _case_number(
    operation: ProposalOperation,
    case_by_source_position: dict[tuple[str, int], str],
) -> str:
    entity_key = operation.change.entity_key.strip()
    if entity_key:
        return entity_key
    referenced = sorted(
        {
            case_by_source_position[position]
            for position in _source_positions(operation.change.source_refs)
            if position in case_by_source_position
        }
    )
    return ", ".join(referenced) if referenced else "미확인"


def _source_positions(refs: Iterable[SourceRef]) -> set[tuple[str, int]]:
    return {(ref.sheet, ref.row) for ref in refs}


def _normalized_property(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).casefold()


def _case_value(value: JsonValue) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        parts = [_case_value(item) for item in value]
        return ", ".join(part for part in parts if part)
    return ""


def _commands(
    proposal: ProposalRevision,
    operation_id: str,
) -> ReviewCardCommands:
    stem = f"/nx_set {proposal.proposal_id} {proposal.revision} {operation_id}"
    return ReviewCardCommands(
        apply=f"{stem} apply",
        exclude=f"{stem} exclude",
        defer=f"{stem} defer",
        edit=f"{stem} edit <JSON>",
    )


def _summary(
    operation: ProposalOperation,
    case_number: str,
    *,
    overlay_only_recovery: bool = False,
) -> str:
    change = operation.change
    if overlay_only_recovery:
        return (
            f"{case_number} · {change.target_database}.{change.property_name}: "
            f"이전 승인에서 {_render_value(operation.approved_value, limit=70)} "
            "Notion 반영 완료; 이번 승인은 Wiki 복구 전용"
        )
    return (
        f"{case_number} · {change.target_database}.{change.property_name}: "
        f"{_render_value(change.current_value, limit=70)} → "
        f"{_render_value(operation.approved_value, limit=70)} "
        f"({operation.action.value})"
    )


def _is_overlay_only_correction_recovery(
    proposal: ProposalRevision,
) -> bool:
    recovery = proposal.correction_binding.get("recovery")
    return (
        proposal.purpose is ProposalPurpose.USER_CORRECTION
        and isinstance(recovery, dict)
        and recovery.get("mode") == CORRECTION_OVERLAY_RECOVERY_MODE
    )


def _change_basis(
    reason: str,
    *,
    excel_refs: tuple[str, ...],
    knowledge_refs: tuple[str, ...],
) -> str:
    parts = [_single_line(reason) or "분석 사유 없음"]
    if excel_refs:
        parts.append(f"Excel {len(excel_refs)}개 위치")
    else:
        parts.append("Excel 위치 없음")
    if knowledge_refs:
        parts.append(f"Wiki {len(knowledge_refs)}개 인용")
    return " | ".join(parts)


def _wiki_impact(
    proposal: ProposalRevision,
    operation: ProposalOperation,
) -> str:
    citations = len(operation.change.knowledge_refs)
    citation_note = f" (고정 Wiki 근거 {citations}개)" if citations else ""
    if (
        _is_overlay_only_correction_recovery(proposal)
        and operation.user_override
        and operation.action is ProposalAction.EXCLUDE
    ):
        return (
            "이미 완료된 Notion 쓰기는 재실행하지 않고 "
            f"Wiki 승인 오버레이만 복구 기록{citation_note}"
        )
    if operation.action is ProposalAction.EDIT:
        return f"사용자 수정값을 Wiki 승인 오버레이에 함께 기록{citation_note}"
    if operation.action is ProposalAction.EXCLUDE:
        return f"Notion·Wiki 반영에서 제외{citation_note}"
    if operation.action is ProposalAction.DEFER:
        return f"Notion·Wiki 반영을 보류{citation_note}"
    return f"승인된 값을 Wiki 승인 오버레이에 기록{citation_note}"


def _single_line(value: str) -> str:
    return " ".join(str(value).split())


def _version_label(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        return "버전 미상"
    return normalized if normalized.casefold().startswith("v") else f"v{normalized}"


def _render_value(value: JsonValue, *, limit: int = 240) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"
