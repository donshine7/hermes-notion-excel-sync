from __future__ import annotations

import hashlib
import itertools
import json
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from notion_excel_sync.models import (
    ProposalAction,
    ProposalOperation,
    ProposalRevision,
    canonical_json,
)


class TelegramAction(StrEnum):
    SHOW_ALL = "show_all"
    SHOW_OPERATION = "show_operation"
    EDIT_VALUE = "edit_value"
    EXCLUDE = "exclude"
    DEFER = "defer"
    RESTORE = "restore"
    BACK = "back"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    action: TelegramAction
    proposal_id: str
    revision: int
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramButton:
    text: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    text: str
    keyboard: tuple[tuple[TelegramButton, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class SentTelegramMessage:
    message_id: str
    chat_id: str
    message: TelegramMessage


class TelegramInteraction(Protocol):
    def send_message(self, chat_id: str, message: TelegramMessage) -> str: ...

    def edit_message(
        self, chat_id: str, message_id: str, message: TelegramMessage
    ) -> None: ...


class TelegramCallbackRegistry:
    """Map compact Telegram callback payloads to full proposal commands.

    Telegram restricts callback data to 64 bytes.  The registry keeps identifiers and
    operation IDs server-side and emits only a short content-derived token.
    """

    def __init__(self) -> None:
        self._commands: dict[str, TelegramCommand] = {}
        self._lock = threading.RLock()

    def register(self, command: TelegramCommand) -> str:
        payload = {
            "action": command.action.value,
            "proposal_id": command.proposal_id,
            "revision": command.revision,
            "operation_id": command.operation_id,
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]
        callback_data = f"nxs:{digest}"
        with self._lock:
            existing = self._commands.get(callback_data)
            if existing is not None and existing != command:
                raise RuntimeError("Telegram callback token collision")
            self._commands[callback_data] = command
        return callback_data

    def resolve(self, callback_data: str) -> TelegramCommand:
        with self._lock:
            try:
                return self._commands[callback_data]
            except KeyError as exc:
                raise KeyError("Unknown or expired Telegram callback") from exc

    def discard_proposal(self, proposal_id: str, *, before_revision: int | None = None) -> None:
        """Invalidate callbacks, normally after an edit creates a newer revision."""

        with self._lock:
            doomed = [
                token
                for token, command in self._commands.items()
                if command.proposal_id == proposal_id
                and (before_revision is None or command.revision < before_revision)
            ]
            for token in doomed:
                del self._commands[token]


def _format_value(value: object, *, limit: int = 500) -> str:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


class TelegramProposalFormatter:
    def __init__(self, registry: TelegramCallbackRegistry | None = None) -> None:
        self.registry = registry or TelegramCallbackRegistry()

    def _button(
        self,
        text: str,
        action: TelegramAction,
        revision: ProposalRevision,
        operation_id: str | None = None,
    ) -> TelegramButton:
        command = TelegramCommand(
            action=action,
            proposal_id=revision.proposal_id,
            revision=revision.revision,
            operation_id=operation_id,
        )
        return TelegramButton(text, self.registry.register(command))

    def render_summary(self, revision: ProposalRevision) -> TelegramMessage:
        by_database: dict[str, int] = {}
        by_action = {action: 0 for action in ProposalAction}
        for operation in revision.operations:
            database = operation.change.target_database
            by_database[database] = by_database.get(database, 0) + 1
            by_action[operation.action] += 1

        lines = [
            f"[동기화 제안 {revision.proposal_id} / 개정 {revision.revision}]",
            "",
            f"원본 버전: {revision.source_version_id}",
            f"제안 digest: {revision.digest}",
            f"상태: {revision.status.value}",
            (
                f"승인 만료: {revision.expires_at.isoformat()}"
                if revision.expires_at
                else "승인 만료: 미설정"
            ),
            f"전체 변경 후보: {len(revision.operations)}건",
        ]
        if by_database:
            lines.extend(["", "영역별 변경"])
            lines.extend(
                f"- {database}: {count}건" for database, count in sorted(by_database.items())
            )
        lines.extend(
            [
                "",
                f"반영 예정: {by_action[ProposalAction.APPLY] + by_action[ProposalAction.EDIT]}건",
                f"사용자 수정: {by_action[ProposalAction.EDIT]}건",
                f"제외: {by_action[ProposalAction.EXCLUDE]}건",
                f"보류: {by_action[ProposalAction.DEFER]}건",
                "",
                "최종 승인은 현재 개정본에 표시된 작업에만 유효합니다.",
                "아래 명령 전체를 새 Telegram 메시지로 보내십시오:",
                (
                    f"/nx_approve {revision.proposal_id} "
                    f"{revision.revision} {revision.digest}"
                ),
            ]
        )
        keyboard = (
            (
                self._button("전체 변경표", TelegramAction.SHOW_ALL, revision),
            ),
            (
                self._button("거부", TelegramAction.REJECT, revision),
            ),
        )
        return TelegramMessage("\n".join(lines), keyboard)

    def render_change_list(self, revision: ProposalRevision) -> TelegramMessage:
        lines = [f"[전체 변경표 / 개정 {revision.revision}]", ""]
        keyboard_rows: list[tuple[TelegramButton, ...]] = []
        for index, operation in enumerate(revision.operations, start=1):
            change = operation.change
            lines.append(
                f"{index}. [{operation.action.value}] {change.target_database} / "
                f"{change.entity_key} / {change.property_name}"
            )
            keyboard_rows.append(
                (
                    self._button(
                        f"{index}번 상세·수정",
                        TelegramAction.SHOW_OPERATION,
                        revision,
                        change.operation_id,
                    ),
                )
            )
        if not revision.operations:
            lines.append("변경 사항이 없습니다.")
        keyboard_rows.append((self._button("요약으로", TelegramAction.BACK, revision),))
        return TelegramMessage("\n".join(lines), tuple(keyboard_rows))

    def render_operation(
        self, revision: ProposalRevision, operation_id: str
    ) -> TelegramMessage:
        operation = self._find_operation(revision, operation_id)
        change = operation.change
        refs = ", ".join(
            f"{ref.sheet}!{ref.cells}" for ref in change.source_refs
        ) or "원본 셀 정보 없음"
        lines = [
            f"[변경 상세 / 개정 {revision.revision}]",
            "",
            f"대상 DB: {change.target_database}",
            f"대상 키: {change.entity_key}",
            f"속성: {change.property_name}",
            f"변경 유형: {change.kind.value}",
            f"현재 Notion 값: {_format_value(change.current_value)}",
            f"분석 제안값: {_format_value(change.proposed_value)}",
            f"최종 승인 예정값: {_format_value(operation.approved_value)}",
            f"현재 처리: {operation.action.value}",
            f"분석기: {change.analyzer} {change.analyzer_version}",
            f"신뢰도: {change.confidence:.2f}",
            f"근거: {change.reason}",
            f"Excel 위치: {refs}",
        ]
        keyboard = (
            (
                self._button(
                    "값 수정", TelegramAction.EDIT_VALUE, revision, operation_id
                ),
                self._button("제외", TelegramAction.EXCLUDE, revision, operation_id),
            ),
            (
                self._button("보류", TelegramAction.DEFER, revision, operation_id),
                self._button(
                    "원래 제안 복원", TelegramAction.RESTORE, revision, operation_id
                ),
            ),
            (self._button("전체 변경표", TelegramAction.SHOW_ALL, revision),),
        )
        return TelegramMessage("\n".join(lines), keyboard)

    @staticmethod
    def _find_operation(
        revision: ProposalRevision, operation_id: str
    ) -> ProposalOperation:
        for operation in revision.operations:
            if operation.change.operation_id == operation_id:
                return operation
        raise KeyError(f"Operation {operation_id!r} is not part of this proposal revision")


class InMemoryTelegramInteraction:
    """A token-free interaction adapter suitable for tests and Hermes integration stubs."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._messages: dict[tuple[str, str], TelegramMessage] = {}
        self._lock = threading.RLock()

    def send_message(self, chat_id: str, message: TelegramMessage) -> str:
        with self._lock:
            message_id = str(next(self._counter))
            self._messages[(chat_id, message_id)] = message
            return message_id

    def edit_message(
        self, chat_id: str, message_id: str, message: TelegramMessage
    ) -> None:
        with self._lock:
            key = (chat_id, message_id)
            if key not in self._messages:
                raise KeyError(f"Telegram message {chat_id}/{message_id} does not exist")
            self._messages[key] = message

    def get_message(self, chat_id: str, message_id: str) -> TelegramMessage:
        with self._lock:
            try:
                return self._messages[(chat_id, message_id)]
            except KeyError as exc:
                raise KeyError(
                    f"Telegram message {chat_id}/{message_id} does not exist"
                ) from exc

    def sent_messages(self) -> tuple[SentTelegramMessage, ...]:
        with self._lock:
            return tuple(
                SentTelegramMessage(message_id, chat_id, message)
                for (chat_id, message_id), message in self._messages.items()
            )
