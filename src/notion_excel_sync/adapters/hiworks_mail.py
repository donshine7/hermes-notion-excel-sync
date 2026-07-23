from __future__ import annotations

import email.utils
import html
import poplib
import re
import ssl
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from typing import Any, Protocol


HIWORKS_POP3_PORT = 995
HIWORKS_REQUIRED_CIPHER = "AES256-GCM-SHA384"


class HiworksMailError(RuntimeError):
    """Raised when a bounded, read-only Hiworks POP3 operation fails."""


class HiworksMailAuthenticationError(HiworksMailError):
    pass


class HiworksMailProtocolError(HiworksMailError):
    pass


@dataclass(frozen=True, slots=True)
class HiworksPop3Credentials:
    email_address: str
    password: str = field(repr=False)
    host: str
    port: int = HIWORKS_POP3_PORT

    def __post_init__(self) -> None:
        if not self.email_address.strip() or not self.password or not self.host.strip():
            raise ValueError("Hiworks email, mail password, and POP3 host are required")
        if any(
            _contains_pop3_control(value)
            for value in (self.email_address, self.password, self.host)
        ):
            raise ValueError("Hiworks POP3 credentials contain control characters")
        if isinstance(self.port, bool) or not 1 <= self.port <= 65_535:
            raise ValueError("Hiworks POP3 port must be between 1 and 65535")


@dataclass(frozen=True, slots=True)
class HiworksAttachmentMetadata:
    filename: str | None
    content_type: str
    disposition: str | None
    content_id: str | None
    transfer_encoding: str | None
    size_bytes: int | None
    exceeds_size_limit: bool
    is_inline: bool


@dataclass(frozen=True, slots=True)
class HiworksMailHeader:
    uidl: str
    pop3_number: int
    size_bytes: int
    message_id: str
    sent_at: datetime | None
    subject: str
    sender: str
    to: str
    cc: str


@dataclass(frozen=True, slots=True)
class HiworksMailMessage:
    header: HiworksMailHeader
    body_text: str
    body_loaded: bool
    body_truncated: bool
    attachments: tuple[HiworksAttachmentMetadata, ...]
    attachments_complete: bool
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class HiworksMailCheckpoint:
    """Serializable membership checkpoints supplied and persisted by the caller."""

    seen_uidls: tuple[str, ...]
    seen_message_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HiworksMailBatch:
    messages: tuple[HiworksMailMessage, ...]
    processed_uidls: tuple[str, ...]
    duplicate_uidls: tuple[str, ...]
    remaining_new_count: int
    checkpoint: HiworksMailCheckpoint


Pop3Response = tuple[bytes, list[bytes], int]


class Pop3ReadOnlySession(Protocol):
    def uidl(self) -> Pop3Response: ...

    def list(self) -> Pop3Response: ...

    def top(
        self,
        message_number: int,
        line_count: int,
        *,
        max_bytes: int,
    ) -> Pop3Response: ...

    def retr(self, message_number: int, *, max_bytes: int) -> Pop3Response: ...

    def close(self) -> None: ...


class HiworksPop3SslClient:
    """Authenticated POP3_SSL client exposing reads and QUIT, but no DELE API."""

    def __init__(
        self,
        credentials: HiworksPop3Credentials,
        *,
        timeout: float = 30.0,
        pop3_factory: Callable[..., Any] | None = None,
        ssl_context_factory: Callable[[], ssl.SSLContext] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("POP3 timeout must be positive")
        context_factory = ssl_context_factory or ssl.create_default_context
        context = context_factory()
        context.set_ciphers(HIWORKS_REQUIRED_CIPHER)
        factory = pop3_factory or poplib.POP3_SSL
        box: Any | None = None
        try:
            box = factory(
                credentials.host,
                credentials.port,
                timeout=timeout,
                context=context,
            )
            box.user(credentials.email_address)
            box.pass_(credentials.password)
        except Exception:
            if box is not None:
                try:
                    box.quit()
                except Exception:
                    pass
            raise HiworksMailAuthenticationError(
                "Hiworks POP3_SSL connection or authentication failed"
            ) from None
        self._box = box

    def _open_box(self) -> Any:
        if self._box is None:
            raise HiworksMailProtocolError("Hiworks POP3 session is closed")
        return self._box

    def uidl(self) -> Pop3Response:
        return self._open_box().uidl()

    def list(self) -> Pop3Response:
        return self._open_box().list()

    def top(
        self,
        message_number: int,
        line_count: int,
        *,
        max_bytes: int,
    ) -> Pop3Response:
        _validate_pop3_number(message_number)
        if (
            isinstance(line_count, bool)
            or not isinstance(line_count, int)
            or line_count < 0
        ):
            raise ValueError("POP3 TOP line count must be a non-negative integer")
        return self._bounded_multiline(
            f"TOP {message_number} {line_count}",
            max_bytes=max_bytes,
        )

    def retr(self, message_number: int, *, max_bytes: int) -> Pop3Response:
        _validate_pop3_number(message_number)
        return self._bounded_multiline(
            f"RETR {message_number}",
            max_bytes=max_bytes,
        )

    def _bounded_multiline(self, command: str, *, max_bytes: int) -> Pop3Response:
        """Drain one multiline response while retaining at most ``max_bytes``."""

        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("POP3 response limit must be positive")
        box = self._open_box()
        box._putcmd(command)
        response = box._getresp()
        lines: list[bytes] = []
        octets = 0
        exceeded = False
        line, line_octets = box._getline()
        while line != b".":
            if line.startswith(b".."):
                line = line[1:]
                line_octets -= 1
            octets += int(line_octets)
            if not exceeded and octets <= max_bytes:
                lines.append(line)
            else:
                exceeded = True
                lines.clear()
            line, line_octets = box._getline()
        return response, lines, octets

    def close(self) -> None:
        box, self._box = self._box, None
        if box is not None:
            try:
                box.quit()
            except Exception:
                pass


def _validate_pop3_number(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("POP3 message number must be a positive integer")


def _contains_pop3_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


class HiworksIncrementalMailReader:
    """Collect new messages by stable UIDL using LIST/TOP before bounded RETR."""

    def __init__(
        self,
        session_factory: Callable[[], Pop3ReadOnlySession],
        *,
        max_messages: int = 100,
        max_header_bytes: int = 256 * 1024,
        max_message_bytes: int = 10 * 1024 * 1024,
        max_body_bytes: int = 2 * 1024 * 1024,
        max_attachment_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        if (
            max_messages <= 0
            or max_header_bytes <= 0
            or max_message_bytes <= 0
            or max_body_bytes <= 0
            or max_attachment_bytes <= 0
        ):
            raise ValueError("Hiworks mail collection limits must be positive")
        self._session_factory = session_factory
        self._max_messages = max_messages
        self._max_header_bytes = max_header_bytes
        self._max_message_bytes = max_message_bytes
        self._max_body_bytes = max_body_bytes
        self._max_attachment_bytes = max_attachment_bytes

    def collect(
        self,
        *,
        seen_uidls: Iterable[str] = (),
        seen_message_ids: Iterable[str] = (),
        sent_after: datetime | None = None,
    ) -> HiworksMailBatch:
        if sent_after is not None and sent_after.tzinfo is None:
            raise ValueError("sent_after must include a timezone")
        known_uidls = {_validated_uidl(value) for value in seen_uidls}
        known_message_ids = {
            key
            for value in seen_message_ids
            if (key := _message_id_key(str(value)))
        }
        session = self._session_factory()
        try:
            uidl_rows = _parse_uidl_rows(session.uidl()[1])
            size_rows = _parse_list_rows(session.list()[1])
            unseen = [
                (number, uidl)
                for number, uidl in sorted(uidl_rows.items())
                if uidl not in known_uidls
            ]
            candidates = unseen[: self._max_messages]
            processed_uidls: list[str] = []
            duplicate_uidls: list[str] = []
            messages: list[HiworksMailMessage] = []
            checkpoint_message_ids = set(known_message_ids)

            for number, uidl in candidates:
                size = size_rows.get(number)
                if size is None:
                    raise HiworksMailProtocolError(
                        f"LIST omitted POP3 message number {number}"
                    )
                _top_response, top_lines, top_octets = session.top(
                    number,
                    0,
                    max_bytes=self._max_header_bytes,
                )
                top_raw = _pop3_bytes_within_limit(
                    top_lines,
                    self._max_header_bytes,
                )
                if top_octets > self._max_header_bytes or top_raw is None:
                    processed_uidls.append(uidl)
                    messages.append(
                        _header_only_message(
                            _minimal_header(uidl, number, size),
                            reason="header_size_limit",
                        )
                    )
                    continue
                header = _parse_header(uidl, number, size, top_raw)
                processed_uidls.append(uidl)
                message_id_key = _message_id_key(header.message_id)
                if message_id_key and message_id_key in checkpoint_message_ids:
                    duplicate_uidls.append(uidl)
                    continue
                if message_id_key:
                    checkpoint_message_ids.add(message_id_key)

                if (
                    sent_after is not None
                    and header.sent_at is not None
                    and header.sent_at < sent_after.astimezone(UTC)
                ):
                    messages.append(
                        _header_only_message(
                            header,
                            reason="before_baseline",
                        )
                    )
                    continue

                if size > self._max_message_bytes:
                    messages.append(
                        _header_only_message(
                            header,
                            reason="message_size_limit",
                        )
                    )
                    continue

                _response, retr_lines, retr_octets = session.retr(
                    number,
                    max_bytes=self._max_message_bytes,
                )
                raw_message = _pop3_bytes_within_limit(
                    retr_lines,
                    self._max_message_bytes,
                )
                if retr_octets > self._max_message_bytes or raw_message is None:
                    messages.append(
                        _header_only_message(
                            header,
                            reason="retrieved_size_limit",
                        )
                    )
                    continue
                try:
                    message = BytesParser(policy=policy.default).parsebytes(raw_message)
                    _assert_same_message(header, message)
                    body, truncated, attachments = _extract_content(
                        message,
                        max_body_bytes=self._max_body_bytes,
                        max_attachment_bytes=self._max_attachment_bytes,
                    )
                except HiworksMailProtocolError:
                    raise
                except Exception:
                    messages.append(
                        _header_only_message(
                            header,
                            reason="mime_parse_failed",
                        )
                    )
                    continue
                messages.append(
                    HiworksMailMessage(
                        header=header,
                        body_text=body,
                        body_loaded=True,
                        body_truncated=truncated,
                        attachments=attachments,
                        attachments_complete=True,
                    )
                )
        except HiworksMailError:
            raise
        except Exception:
            raise HiworksMailError("Hiworks POP3 read failed") from None
        finally:
            try:
                session.close()
            except Exception:
                pass

        next_uidls = tuple(sorted(known_uidls.union(processed_uidls)))
        return HiworksMailBatch(
            messages=tuple(messages),
            processed_uidls=tuple(processed_uidls),
            duplicate_uidls=tuple(duplicate_uidls),
            remaining_new_count=max(0, len(unseen) - len(candidates)),
            checkpoint=HiworksMailCheckpoint(
                seen_uidls=next_uidls,
                seen_message_ids=tuple(sorted(checkpoint_message_ids)),
            ),
        )


def _validated_uidl(value: object) -> str:
    uidl = str(value).strip()
    if (
        not uidl
        or len(uidl) > 1_024
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in uidl)
    ):
        raise HiworksMailProtocolError("POP3 UIDL is invalid")
    return uidl


def _parse_uidl_rows(lines: Sequence[bytes]) -> dict[int, str]:
    result: dict[int, str] = {}
    seen_uidls: set[str] = set()
    for line in lines:
        try:
            number_text, uidl_text = bytes(line).decode("ascii").split()
            number = int(number_text)
        except (UnicodeDecodeError, ValueError):
            raise HiworksMailProtocolError("POP3 UIDL response is malformed") from None
        uidl = _validated_uidl(uidl_text)
        if number <= 0 or number in result or uidl in seen_uidls:
            raise HiworksMailProtocolError("POP3 UIDL response contains duplicates")
        result[number] = uidl
        seen_uidls.add(uidl)
    return result


def _parse_list_rows(lines: Sequence[bytes]) -> dict[int, int]:
    result: dict[int, int] = {}
    for line in lines:
        try:
            number_text, size_text = bytes(line).decode("ascii").split()
            number = int(number_text)
            size = int(size_text)
        except (UnicodeDecodeError, ValueError):
            raise HiworksMailProtocolError("POP3 LIST response is malformed") from None
        if number <= 0 or size < 0 or number in result:
            raise HiworksMailProtocolError("POP3 LIST response contains invalid rows")
        result[number] = size
    return result


def _pop3_bytes_within_limit(
    lines: Sequence[bytes],
    limit: int,
) -> bytes | None:
    total = 0
    chunks: list[bytes] = []
    for line in lines:
        chunk = bytes(line)
        total += len(chunk) + 2
        if total > limit:
            return None
        chunks.append(chunk)
    return b"\r\n".join(chunks) + b"\r\n"


def _decode_mime(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    parts: list[str] = []
    for part, charset in decode_header(text):
        if not isinstance(part, bytes):
            parts.append(part)
            continue
        try:
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        except LookupError:
            parts.append(part.decode("utf-8", errors="replace"))
    return "".join(parts).strip()


def _parse_date(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_header(
    uidl: str,
    number: int,
    size: int,
    raw_header: bytes,
) -> HiworksMailHeader:
    message = BytesParser(policy=policy.default).parsebytes(
        raw_header,
        headersonly=True,
    )
    return HiworksMailHeader(
        uidl=uidl,
        pop3_number=number,
        size_bytes=size,
        message_id=_decode_mime(message.get("Message-ID")),
        sent_at=_parse_date(message.get("Date")),
        subject=_decode_mime(message.get("Subject")),
        sender=_decode_mime(message.get("From")),
        to=_decode_mime(message.get("To")),
        cc=_decode_mime(message.get("Cc")),
    )


def _minimal_header(uidl: str, number: int, size: int) -> HiworksMailHeader:
    return HiworksMailHeader(
        uidl=uidl,
        pop3_number=number,
        size_bytes=size,
        message_id="",
        sent_at=None,
        subject="",
        sender="",
        to="",
        cc="",
    )


def _message_id_key(value: str) -> str:
    return value.strip()


def _assert_same_message(header: HiworksMailHeader, message: Message) -> None:
    retrieved_id = _message_id_key(_decode_mime(message.get("Message-ID")))
    expected_id = _message_id_key(header.message_id)
    if expected_id and retrieved_id != expected_id:
        raise HiworksMailProtocolError("TOP and RETR returned different messages")


def _header_only_message(
    header: HiworksMailHeader,
    *,
    reason: str,
) -> HiworksMailMessage:
    return HiworksMailMessage(
        header=header,
        body_text="",
        body_loaded=False,
        body_truncated=False,
        attachments=(),
        attachments_complete=False,
        skip_reason=reason,
    )


def _is_attachment(part: Message) -> bool:
    disposition = part.get_content_disposition()
    return bool(
        part.get_filename()
        or disposition == "attachment"
        or part.get_content_maintype() not in {"text", "multipart"}
    )


def _attachment_metadata(
    part: Message,
    *,
    max_attachment_bytes: int,
) -> HiworksAttachmentMetadata:
    payload = part.get_payload(decode=True)
    size = len(payload) if isinstance(payload, bytes) else None
    return HiworksAttachmentMetadata(
        filename=_decode_mime(part.get_filename()) or None,
        content_type=part.get_content_type(),
        disposition=part.get_content_disposition(),
        content_id=_decode_mime(part.get("Content-ID")) or None,
        transfer_encoding=_decode_mime(part.get("Content-Transfer-Encoding")) or None,
        size_bytes=size,
        exceeds_size_limit=bool(
            size is not None and size > max_attachment_bytes
        ),
        is_inline=part.get_content_disposition() == "inline",
    )


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _plain_from_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _clean_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _extract_content(
    message: Message,
    *,
    max_body_bytes: int,
    max_attachment_bytes: int,
) -> tuple[str, bool, tuple[HiworksAttachmentMetadata, ...]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[HiworksAttachmentMetadata] = []

    def visit(part: Message) -> None:
        if _is_attachment(part):
            attachments.append(
                _attachment_metadata(
                    part,
                    max_attachment_bytes=max_attachment_bytes,
                )
            )
            return
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, Message):
                        visit(child)
            return
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_plain_from_html(_part_text(part)))

    visit(message)
    body = _clean_text("\n\n".join(plain_parts or html_parts))
    bounded_body, truncated = _truncate_utf8(body, max_body_bytes)
    return bounded_body, truncated, tuple(attachments)


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


__all__ = [
    "HIWORKS_POP3_PORT",
    "HIWORKS_REQUIRED_CIPHER",
    "HiworksAttachmentMetadata",
    "HiworksIncrementalMailReader",
    "HiworksMailAuthenticationError",
    "HiworksMailBatch",
    "HiworksMailCheckpoint",
    "HiworksMailError",
    "HiworksMailHeader",
    "HiworksMailMessage",
    "HiworksMailProtocolError",
    "HiworksPop3Credentials",
    "HiworksPop3SslClient",
    "Pop3ReadOnlySession",
]
