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
_TITLE_PROPERTIES = frozenset(
    {
        "당소사건번호",
        "사건번호",
        "근거명",
        "그룹명",
        "당사자명",
        "항목명",
        "증빙명",
        "업무명",
        "기일명",
        "후속관리명",
        "연락제목",
        "히스토리명",
        "검토명",
    }
)
_FRIENDLY_PROPERTY_NAMES = {
    "SHA256": "파일 식별값(SHA256)",
    "driveItem ID": "원본 파일 ID",
    "버전ID": "원본 버전",
    "수집일시": "자료 확인 시각",
    "파싱상태": "읽기 상태",
    "보안등급": "자료 등급",
    "OneDrive URL": "원본 링크",
    "이벤트ID": "이벤트 식별값",
    "원본버전": "원본 버전",
    "원본해시": "원본 파일 식별값",
}
_TECHNICAL_PROPERTIES = frozenset(
    {
        "SHA256",
        "driveItem ID",
        "버전ID",
        "수집일시",
        "파싱상태",
        "보안등급",
        "OneDrive URL",
        "이벤트ID",
        "원본버전",
        "원본해시",
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
    display_labels = _display_labels_by_entity(main_operations)
    overlay_only_recovery = _is_overlay_only_correction_recovery(proposal)

    cards: list[ReviewCard] = []
    for number, operation in enumerate(main_operations, start=1):
        change = operation.change
        automatic = tuple(
            item.change.operation_id
            for item in history_by_source_id.get(change.operation_id, ())
        )
        case_number = _case_number(
            operation,
            case_by_source_position,
            display_labels=display_labels,
        )
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
    detail_item: int | None = None,
) -> str:
    """Render a page-grouped Telegram summary or one exact detail card."""

    if cards_per_page < 1:
        raise ValueError("cards_per_page must be positive")
    cards = build_review_cards(proposal)
    groups = _group_visible_cards(cards)
    total_pages = max(1, (len(groups) + cards_per_page - 1) // cards_per_page)
    if page < 1 or page > total_pages:
        raise ReviewCardPageError(
            f"Page {page} does not exist; valid pages are 1-{total_pages}"
        )
    start = (page - 1) * cards_per_page
    selected_groups = groups[start : start + cards_per_page]
    if detail_item is not None:
        if detail_item < 1 or detail_item > len(selected_groups):
            raise ReviewCardPageError(
                "Detail item does not exist on the requested review page"
            )
        selected_groups = (selected_groups[detail_item - 1],)
    unmerged_history_count = len(
        unmerged_history_operation_ids(proposal.operations)
    )
    history_count = sum(
        operation.change.target_database == HISTORY_DATABASE
        for operation in proposal.operations
    )
    approve_command = (
        None
        if unmerged_history_count
        else (
            f"/nx_approve {proposal.proposal_id} "
            f"{proposal.revision} {proposal.digest}"
        )
    )
    reject_command = f"/nx_reject {proposal.proposal_id} {proposal.revision}"
    overlay_only_recovery = _is_overlay_only_correction_recovery(proposal)
    lines = [
        f"[NX_PROPOSAL_REVIEW {proposal.proposal_id} r{proposal.revision}]",
        f"상태: {proposal.status.value}",
        f"Digest: {proposal.digest}",
        (
            f"페이지: {page}/{total_pages} "
            f"(Notion 페이지 {len(groups)}개, 화면당 {cards_per_page}개)"
        ),
        (
            f"속성 변경: {len(cards)}건 "
            f"(자동 변경이력 {history_count}건, 미병합 {unmerged_history_count}건)"
        ),
    ]
    if detail_item is None:
        lines.append(
            "요약 화면: 시스템 식별값과 정확한 수정 명령은 각 항목의 상세 명령으로 확인"
        )
    else:
        lines.append(f"상세 화면: 이 페이지의 {detail_item}번 항목")
    if not groups:
        lines.extend(["", "검토할 Notion 페이지 변경이 없습니다."])
    for local_index, group in enumerate(selected_groups, start=1):
        first = group[0]
        display_index = detail_item or local_index
        lines.extend(
            [
                "",
                f"{display_index}. {_database_action_summary(group)}",
                (
                    f"{_subject_label(first.target_database)}: "
                    f"{_humanize_source_title(first.case_number)}"
                ),
                f"원본 위치: {_human_source_summary(group)}",
                "Notion 변경 내용:",
            ]
        )
        visible_cards = (
            group
            if detail_item is not None
            else tuple(
                card
                for card in group
                if card.property_name not in _TECHNICAL_PROPERTIES
            )
        )
        for card in visible_cards:
            property_label = _friendly_property_name(card.property_name)
            current_value = _humanize_card_value(card, card.current_value)
            approved_value = _humanize_card_value(card, card.approved_value)
            if overlay_only_recovery:
                lines.append(
                    f"- {property_label}: {_render_value(approved_value)} "
                    "(이전 승인에서 반영 완료)"
                )
            else:
                lines.append(
                    f"- {property_label}: "
                    f"{_human_change(current_value, approved_value)} "
                    f"[{_friendly_action(card.action)}]"
                )
        technical_cards = tuple(
            card for card in group if card.property_name in _TECHNICAL_PROPERTIES
        )
        if detail_item is None and technical_cards:
            technical_labels = ", ".join(
                _friendly_property_name(card.property_name)
                for card in technical_cards
            )
            lines.append(
                f"- 시스템 추적정보 {len(technical_cards)}건: "
                f"{technical_labels} (상세 화면에서 확인)"
            )
        reasons = tuple(dict.fromkeys(card.change_basis for card in group))
        lines.append(f"변경 근거: {'; '.join(reasons)}")
        knowledge_refs = tuple(
            dict.fromkeys(ref for card in group for ref in card.knowledge_refs)
        )
        if knowledge_refs:
            lines.append(f"Wiki 참고: {'; '.join(knowledge_refs)}")
        lines.append(
            "승인 후 Wiki: "
            + (
                "사용자가 승인한 값과 수정 내용을 함께 기록"
                if not overlay_only_recovery
                else "Wiki 승인 오버레이만 복구"
            )
        )
        if overlay_only_recovery:
            lines.append("이 복구안은 수정·거부·보류할 수 없습니다.")
        elif detail_item is not None:
            lines.append("세부 조정(선택 사항 — 필요한 항목의 명령만 사용):")
            for card in group:
                label = _friendly_property_name(card.property_name)
                lines.extend(
                    [
                        (
                            f"- {label} · 작업 ID {card.operation_id} "
                            f"(Operation ID: {card.operation_id})"
                        ),
                        (
                            "  반영/제외/보류: "
                            f"{card.commands.apply} | "
                            f"{card.commands.exclude} | "
                            f"{card.commands.defer}"
                        ),
                        f"  값 수정: {card.commands.edit}",
                    ]
                )
        else:
            lines.append(
                "정확한 값·수정 명령: "
                f"/nx_show {proposal.proposal_id} {proposal.revision} "
                f"{page} detail {display_index}"
            )
    if detail_item is not None:
        lines.extend(
            [
                "",
                (
                    "요약으로 돌아가기: "
                    f"/nx_show {proposal.proposal_id} {proposal.revision} {page}"
                ),
            ]
        )
    elif page < total_pages:
        lines.extend(
            [
                "",
                (
                    "다음 페이지: "
                    f"/nx_show {proposal.proposal_id} {proposal.revision} {page + 1}"
                ),
            ]
        )
    if approve_command is None:
        lines.extend(
            [
                "",
                (
                    "승인 불가: 검토 카드에 연결되지 않은 변경이력 operation "
                    f"{unmerged_history_count}개가 있습니다."
                ),
                "표시되지 않은 변경이 있는 제안에는 승인 명령을 제공하지 않습니다.",
            ]
        )
        if not overlay_only_recovery:
            lines.append(f"거부: {reject_command}")
    elif overlay_only_recovery:
        lines.extend(
            [
                "",
                "허용 명령(현재 전체 digest와 정확히 일치해야 함):",
                approve_command,
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
                f"거부: {reject_command}",
                "최종 승인(현재 전체 digest와 정확히 일치해야 함):",
                approve_command,
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
        value = _case_value(operation.approved_value)
        if not value:
            value = _case_value(change.proposed_value)
        if not value and not _is_internal_entity_key(change.entity_key.strip()):
            value = change.entity_key.strip()
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
    *,
    display_labels: dict[tuple[str, str], str] | None = None,
) -> str:
    change = operation.change
    entity_key = change.entity_key.strip()
    if entity_key and not _is_internal_entity_key(entity_key):
        return entity_key
    referenced = sorted(
        {
            case_by_source_position[position]
            for position in _source_positions(change.source_refs)
            if position in case_by_source_position
        }
    )
    if referenced:
        return ", ".join(referenced)
    label = (display_labels or {}).get(
        (change.target_database, change.entity_key)
    )
    if label:
        return label
    positions = sorted(_source_positions(change.source_refs))
    if positions:
        sheet, row = positions[0]
        return f"Excel {sheet} 시트 {row}행 자료"
    return "식별 정보 없음"


def _display_labels_by_entity(
    operations: Iterable[ProposalOperation],
) -> dict[tuple[str, str], str]:
    labels: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for operation in operations:
        change = operation.change
        if _normalized_property(change.property_name) not in _TITLE_PROPERTIES:
            continue
        value = _case_value(operation.approved_value) or _case_value(
            change.proposed_value
        )
        if value:
            labels[(change.target_database, change.entity_key)].append(
                (change.operation_id, value)
            )
    return {
        key: sorted(values)[0][1]
        for key, values in labels.items()
    }


def _group_visible_cards(
    cards: Iterable[ReviewCard],
) -> tuple[tuple[ReviewCard, ...], ...]:
    groups: list[list[ReviewCard]] = []
    indexes: dict[tuple[str, str], int] = {}
    for card in cards:
        key = (card.target_database, card.entity_key)
        index = indexes.get(key)
        if index is None:
            indexes[key] = len(groups)
            groups.append([card])
        else:
            groups[index].append(card)
    return tuple(tuple(group) for group in groups)


def _database_action_summary(cards: tuple[ReviewCard, ...]) -> str:
    database = cards[0].target_database
    if all(card.current_value is None for card in cards):
        return f"{database} 새 페이지 만들기"
    return f"{database} 페이지 수정"


def _subject_label(database: str) -> str:
    return {
        "한국 특허 사건": "사건번호",
        "근거자료": "자료",
        "그룹": "그룹",
        "당사자": "당사자",
        "비용·청구": "비용 항목",
        "정부지원사업 증빙": "증빙 항목",
        "업무·절차": "업무",
        "기일": "기일",
        "등록결정 후속관리": "등록 후속",
        "연락이력": "연락",
        "사건 히스토리": "사건 이벤트",
        "검토함": "검토 항목",
    }.get(database, "대상")


def _human_source_summary(cards: tuple[ReviewCard, ...]) -> str:
    positions: set[tuple[str, int]] = set()
    for card in cards:
        for source_ref in card.excel_source_refs:
            match = re.match(r"(.+?) 행 ([0-9]+)", source_ref)
            if match:
                positions.add((match.group(1), int(match.group(2))))
    if positions:
        return ", ".join(
            f"Excel {sheet} 시트 {row}행"
            for sheet, row in sorted(positions)
        )
    knowledge_count = len(
        {ref for card in cards for ref in card.knowledge_refs}
    )
    return f"Wiki 근거 {knowledge_count}개" if knowledge_count else "근거 위치 없음"


def _friendly_property_name(property_name: str) -> str:
    return _FRIENDLY_PROPERTY_NAMES.get(property_name, property_name)


def _friendly_action(action: ProposalAction) -> str:
    return {
        ProposalAction.APPLY: "반영",
        ProposalAction.EDIT: "사용자 수정값 반영",
        ProposalAction.EXCLUDE: "제외",
        ProposalAction.DEFER: "보류",
    }[action]


def _human_change(current: JsonValue, approved: JsonValue) -> str:
    if current is None:
        return f"새로 입력 {_render_value(approved)}"
    return f"{_render_value(current)} → {_render_value(approved)}"


def _humanize_card_value(card: ReviewCard, value: JsonValue) -> JsonValue:
    if card.property_name == "근거명" and isinstance(value, str):
        return _humanize_source_title(value)
    return value


def _humanize_source_title(value: str) -> str:
    return re.sub(
        r"\s+\(vsha256:[0-9a-f]{64}\)\Z",
        "",
        value,
        flags=re.IGNORECASE,
    )


def _is_internal_entity_key(value: str) -> bool:
    return value.casefold().startswith(
        (
            "source:",
            "history:",
            "analysis:",
            "review:",
            "party:",
            "group:",
            "workflow:",
            "deadline:",
            "cost:",
            "evidence:",
            "registration:",
            "contact:",
            "materials:",
            "case-history:",
        )
    )


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
