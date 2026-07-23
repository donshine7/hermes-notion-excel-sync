from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage

import pytest

import notion_excel_sync.adapters.hiworks_mail as hiworks_mail
from notion_excel_sync.adapters.hiworks_mail import (
    HIWORKS_REQUIRED_CIPHER,
    HiworksIncrementalMailReader,
    HiworksMailAuthenticationError,
    HiworksMailError,
    HiworksMailProtocolError,
    HiworksPop3Credentials,
    HiworksPop3SslClient,
)


def _message(
    *,
    message_id: str,
    subject: str = "검토 요청",
    body: str = "본문",
    attachment: tuple[str, bytes] | None = None,
    date_header: str | None = "Wed, 08 Jul 2026 10:30:00 +0900",
) -> bytes:
    message = EmailMessage()
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "Receiver <receiver@example.com>"
    message["Cc"] = "Reviewer <reviewer@example.com>"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if date_header is not None:
        message["Date"] = date_header
    message.set_content(body)
    if attachment is not None:
        filename, payload = attachment
        message.add_attachment(
            payload,
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    return message.as_bytes(policy=policy.SMTP)


class FakePop3Session:
    def __init__(
        self,
        messages: dict[int, bytes],
        *,
        uidls: dict[int, str] | None = None,
        declared_sizes: dict[int, int] | None = None,
        uidl_lines: list[bytes] | None = None,
        list_lines: list[bytes] | None = None,
        top_messages: dict[int, bytes] | None = None,
        fail_top: bool = False,
    ) -> None:
        self.messages = messages
        self.uidls = uidls or {
            number: f"uid-{number}" for number in sorted(messages)
        }
        self.declared_sizes = declared_sizes or {
            number: len(raw) for number, raw in messages.items()
        }
        self.custom_uidl_lines = uidl_lines
        self.custom_list_lines = list_lines
        self.top_messages = top_messages or {}
        self.fail_top = fail_top
        self.calls: list[str] = []
        self.closed = False

    def uidl(self) -> tuple[bytes, list[bytes], int]:
        self.calls.append("uidl")
        lines = self.custom_uidl_lines or [
            f"{number} {uidl}".encode("ascii")
            for number, uidl in sorted(self.uidls.items())
        ]
        return b"+OK", lines, sum(len(line) for line in lines)

    def list(self) -> tuple[bytes, list[bytes], int]:
        self.calls.append("list")
        lines = self.custom_list_lines or [
            f"{number} {size}".encode("ascii")
            for number, size in sorted(self.declared_sizes.items())
        ]
        return b"+OK", lines, sum(len(line) for line in lines)

    def top(
        self,
        message_number: int,
        line_count: int,
        *,
        max_bytes: int,
    ) -> tuple[bytes, list[bytes], int]:
        del max_bytes
        self.calls.append(f"top:{message_number}:{line_count}")
        if self.fail_top:
            raise RuntimeError("server detail must not escape")
        raw = self.top_messages.get(
            message_number,
            self.messages[message_number],
        )
        header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        lines = header.split(b"\r\n")
        return b"+OK", lines, len(header)

    def retr(
        self,
        message_number: int,
        *,
        max_bytes: int,
    ) -> tuple[bytes, list[bytes], int]:
        del max_bytes
        self.calls.append(f"retr:{message_number}")
        raw = self.messages[message_number]
        return b"+OK", raw.split(b"\r\n"), len(raw)

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True


class FakeSslContext:
    def __init__(self) -> None:
        self.ciphers: list[str] = []

    def set_ciphers(self, value: str) -> None:
        self.ciphers.append(value)


class FakePop3Box:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def user(self, value: str) -> None:
        self.calls.append(("user", value))

    def pass_(self, value: str) -> None:
        self.calls.append(("pass", value))

    def quit(self) -> None:
        self.calls.append(("quit", None))


class FakeMultilinePop3Box(FakePop3Box):
    def __init__(self, lines: list[bytes]) -> None:
        super().__init__()
        self.lines = iter([*lines, b"."])
        self.commands: list[str] = []
        self.drained_lines = 0

    def _putcmd(self, value: str) -> None:
        self.commands.append(value)

    def _getresp(self) -> bytes:
        return b"+OK"

    def _getline(self) -> tuple[bytes, int]:
        self.drained_lines += 1
        line = next(self.lines)
        return line, len(line) + 2


def test_ssl_client_uses_hiworks_cipher_user_pass_and_quit_only() -> None:
    context = FakeSslContext()
    box = FakePop3Box()
    factory_calls: list[tuple[str, int, float, object]] = []

    def factory(
        host: str,
        port: int,
        *,
        timeout: float,
        context: object,
    ) -> FakePop3Box:
        factory_calls.append((host, port, timeout, context))
        return box

    credentials = HiworksPop3Credentials(
        "user@example.com",
        "mail-only-secret",
        "pop3.example.com",
    )
    client = HiworksPop3SslClient(
        credentials,
        timeout=12,
        pop3_factory=factory,
        ssl_context_factory=lambda: context,  # type: ignore[arg-type]
    )
    client.close()

    assert "mail-only-secret" not in repr(credentials)
    assert context.ciphers == [HIWORKS_REQUIRED_CIPHER]
    assert factory_calls == [("pop3.example.com", 995, 12, context)]
    assert box.calls == [
        ("user", "user@example.com"),
        ("pass", "mail-only-secret"),
        ("quit", None),
    ]
    assert not hasattr(client, "dele")


def test_ssl_client_sanitizes_auth_failure_and_closes_connection() -> None:
    context = FakeSslContext()

    class FailingBox(FakePop3Box):
        def pass_(self, value: str) -> None:
            del value
            raise RuntimeError("server included a credential-like detail")

    box = FailingBox()
    with pytest.raises(HiworksMailAuthenticationError) as caught:
        HiworksPop3SslClient(
            HiworksPop3Credentials(
                "user@example.com",
                "mail-only-secret",
                "pop3.example.com",
            ),
            pop3_factory=lambda *args, **kwargs: box,
            ssl_context_factory=lambda: context,  # type: ignore[arg-type]
        )
    assert "credential-like" not in str(caught.value)
    assert box.calls == [("user", "user@example.com"), ("quit", None)]


@pytest.mark.parametrize(
    ("email_address", "password", "host"),
    [
        ("user@example.com\r\nDELE 1", "secret", "pop3.example.com"),
        ("user@example.com", "secret\r\nDELE 1", "pop3.example.com"),
        ("user@example.com", "secret", "pop3.example.com\nDELE 1"),
    ],
)
def test_credentials_reject_pop3_command_controls(
    email_address: str,
    password: str,
    host: str,
) -> None:
    with pytest.raises(ValueError, match="control"):
        HiworksPop3Credentials(email_address, password, host)


def test_ssl_client_drains_retr_without_retaining_bytes_past_limit() -> None:
    context = FakeSslContext()
    box = FakeMultilinePop3Box([b"1234", b"5678", b"90"])
    client = HiworksPop3SslClient(
        HiworksPop3Credentials(
            "user@example.com",
            "mail-only-secret",
            "pop3.example.com",
        ),
        pop3_factory=lambda *args, **kwargs: box,
        ssl_context_factory=lambda: context,  # type: ignore[arg-type]
    )

    response, lines, octets = client.retr(7, max_bytes=5)
    client.close()

    assert response == b"+OK"
    assert lines == []
    assert octets > 5
    assert box.commands == ["RETR 7"]
    assert box.drained_lines == 4


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("retr", ("1\r\nDELE 2",)),
        ("retr", (True,)),
        ("top", ("1\r\nDELE 2", 0)),
        ("top", (1, "0\r\nDELE 2")),
    ],
)
def test_ssl_client_rejects_pop3_command_injection(
    method: str,
    args: tuple[object, ...],
) -> None:
    context = FakeSslContext()
    box = FakeMultilinePop3Box([])
    client = HiworksPop3SslClient(
        HiworksPop3Credentials(
            "user@example.com",
            "mail-only-secret",
            "pop3.example.com",
        ),
        pop3_factory=lambda *factory_args, **factory_kwargs: box,
        ssl_context_factory=lambda: context,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError):
        getattr(client, method)(*args, max_bytes=100)
    client.close()

    assert box.commands == []
    assert all("DELE" not in command for command in box.commands)


def test_incremental_scan_skips_seen_uidl_and_uses_header_first_calls() -> None:
    session = FakePop3Session(
        {
            1: _message(message_id="<old@example.com>", subject="기존"),
            2: _message(message_id="<new@example.com>", subject="신규"),
        }
    )
    reader = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=100_000,
    )

    batch = reader.collect(
        seen_uidls={"uid-1"},
        seen_message_ids={"<old@example.com>"},
    )

    assert [item.header.uidl for item in batch.messages] == ["uid-2"]
    assert batch.processed_uidls == ("uid-2",)
    assert batch.checkpoint.seen_uidls == ("uid-1", "uid-2")
    assert batch.checkpoint.seen_message_ids == (
        "<new@example.com>",
        "<old@example.com>",
    )
    assert session.calls == ["uidl", "list", "top:2:0", "retr:2", "close"]

    second_session = FakePop3Session(session.messages)
    replay = HiworksIncrementalMailReader(
        lambda: second_session,
        max_message_bytes=100_000,
    ).collect(
        seen_uidls=batch.checkpoint.seen_uidls,
        seen_message_ids=batch.checkpoint.seen_message_ids,
    )
    assert replay.messages == ()
    assert replay.processed_uidls == ()
    assert second_session.calls == ["uidl", "list", "close"]


def test_body_is_bounded_and_attachment_content_is_not_returned() -> None:
    attachment_bytes = b"private-attachment-payload"
    session = FakePop3Session(
        {
            1: _message(
                message_id="<bounded@example.com>",
                body="가" * 200,
                attachment=("evidence.pdf", attachment_bytes),
            )
        }
    )
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=100_000,
        max_body_bytes=25,
        max_attachment_bytes=10,
    ).collect()

    item = batch.messages[0]
    assert item.body_text == "가" * 8
    assert len(item.body_text.encode("utf-8")) <= 25
    assert item.body_truncated is True
    assert item.body_loaded is True
    assert item.attachments_complete is True
    assert len(item.attachments) == 1
    attachment = item.attachments[0]
    assert attachment.filename == "evidence.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.disposition == "attachment"
    assert attachment.size_bytes == len(attachment_bytes)
    assert attachment.exceeds_size_limit is True
    assert attachment_bytes.decode() not in str(asdict(batch))


def test_declared_large_message_uses_top_only_and_advances_checkpoint() -> None:
    session = FakePop3Session(
        {1: _message(message_id="<large@example.com>")},
        declared_sizes={1: 50_000},
    )
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=1_000,
    ).collect()

    item = batch.messages[0]
    assert item.header.subject == "검토 요청"
    assert item.body_loaded is False
    assert item.body_text == ""
    assert item.attachments == ()
    assert item.attachments_complete is False
    assert item.skip_reason == "message_size_limit"
    assert batch.checkpoint.seen_uidls == ("uid-1",)
    assert session.calls == ["uidl", "list", "top:1:0", "close"]


def test_messages_before_command_time_baseline_are_not_retrieved() -> None:
    session = FakePop3Session(
        {1: _message(message_id="<before@example.com>", body="old body")}
    )

    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=100_000,
    ).collect(sent_after=datetime(2026, 7, 15, tzinfo=UTC))

    assert session.calls == ["uidl", "list", "top:1:0", "close"]
    assert batch.messages[0].skip_reason == "before_baseline"
    assert batch.messages[0].body_loaded is False
    assert batch.checkpoint.seen_uidls == ("uid-1",)


@pytest.mark.parametrize(
    ("date_header", "baseline", "skip_reason"),
    [
        (
            "Wed, 08 Jul 2026 10:30:00 +0900",
            datetime(2026, 7, 8, 1, 30, tzinfo=UTC),
            "before_baseline",
        ),
        (
            None,
            datetime(2026, 7, 8, 1, 29, tzinfo=UTC),
            "unverifiable_timestamp",
        ),
    ],
)
def test_only_mail_proven_after_t0_can_be_retrieved(
    date_header: str | None,
    baseline: datetime,
    skip_reason: str,
) -> None:
    session = FakePop3Session(
        {
            1: _message(
                message_id="<bounded@example.com>",
                body="must not be retrieved",
                date_header=date_header,
            )
        }
    )

    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=100_000,
    ).collect(sent_after=baseline)

    assert session.calls == ["uidl", "list", "top:1:0", "close"]
    assert batch.messages[0].skip_reason == skip_reason
    assert batch.messages[0].body_loaded is False
    assert batch.checkpoint.seen_uidls == ("uid-1",)


def test_large_top_header_is_isolated_and_does_not_block_later_mail() -> None:
    session = FakePop3Session(
        {
            1: _message(
                message_id="<large-header@example.com>",
                subject="x" * 5_000,
            ),
            2: _message(message_id="<normal@example.com>", subject="정상"),
        }
    )
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_header_bytes=1_000,
        max_message_bytes=100_000,
    ).collect()

    assert batch.processed_uidls == ("uid-1", "uid-2")
    assert batch.checkpoint.seen_uidls == ("uid-1", "uid-2")
    assert batch.messages[0].header.uidl == "uid-1"
    assert batch.messages[0].skip_reason == "header_size_limit"
    assert batch.messages[1].header.subject == "정상"
    assert batch.messages[1].body_loaded is True
    assert "retr:1" not in session.calls
    assert "retr:2" in session.calls


def test_retr_size_over_limit_is_not_parsed() -> None:
    session = FakePop3Session(
        {
            1: _message(
                message_id="<lying-size@example.com>",
                body="x" * 2_000,
            )
        },
        declared_sizes={1: 100},
    )
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=500,
    ).collect()

    item = batch.messages[0]
    assert item.body_loaded is False
    assert item.skip_reason == "retrieved_size_limit"
    assert session.calls == ["uidl", "list", "top:1:0", "retr:1", "close"]


def test_duplicate_message_id_is_checkpointed_without_second_retr() -> None:
    messages = {
        1: _message(message_id="<duplicate@example.com>", body="first"),
        2: _message(message_id="<duplicate@example.com>", body="second"),
    }
    session = FakePop3Session(messages)
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=100_000,
    ).collect()

    assert len(batch.messages) == 1
    assert batch.messages[0].body_text == "first"
    assert batch.processed_uidls == ("uid-1", "uid-2")
    assert batch.duplicate_uidls == ("uid-2",)
    assert batch.checkpoint.seen_uidls == ("uid-1", "uid-2")
    assert session.calls == [
        "uidl",
        "list",
        "top:1:0",
        "retr:1",
        "top:2:0",
        "close",
    ]


def test_message_id_local_part_case_is_not_deduplicated() -> None:
    session = FakePop3Session(
        {
            1: _message(message_id="<Case@domain.example>", body="upper"),
            2: _message(message_id="<case@domain.example>", body="lower"),
        }
    )
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=100_000,
    ).collect()

    assert [item.body_text for item in batch.messages] == ["upper", "lower"]
    assert batch.duplicate_uidls == ()
    assert batch.checkpoint.seen_message_ids == (
        "<Case@domain.example>",
        "<case@domain.example>",
    )
    assert "retr:1" in session.calls
    assert "retr:2" in session.calls


def test_max_messages_leaves_unprocessed_uidls_out_of_checkpoint() -> None:
    session = FakePop3Session(
        {
            1: _message(message_id="<one@example.com>"),
            2: _message(message_id="<two@example.com>"),
            3: _message(message_id="<three@example.com>"),
        }
    )
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_messages=2,
        max_message_bytes=100_000,
    ).collect()

    assert batch.processed_uidls == ("uid-1", "uid-2")
    assert batch.remaining_new_count == 1
    assert batch.checkpoint.seen_uidls == ("uid-1", "uid-2")
    assert "top:3:0" not in session.calls


def test_duplicate_uidl_fails_closed_and_always_closes() -> None:
    session = FakePop3Session(
        {
            1: _message(message_id="<one@example.com>"),
            2: _message(message_id="<two@example.com>"),
        },
        uidl_lines=[b"1 duplicate", b"2 duplicate"],
    )

    with pytest.raises(HiworksMailProtocolError, match="duplicates"):
        HiworksIncrementalMailReader(lambda: session).collect()
    assert session.calls == ["uidl", "close"]
    assert session.closed is True


def test_failure_returns_no_partial_checkpoint_and_hides_server_detail() -> None:
    session = FakePop3Session(
        {1: _message(message_id="<one@example.com>")},
        fail_top=True,
    )

    with pytest.raises(HiworksMailError) as caught:
        HiworksIncrementalMailReader(lambda: session).collect()
    assert "server detail" not in str(caught.value)
    assert session.calls == ["uidl", "list", "top:1:0", "close"]
    assert session.closed is True


def test_top_and_retr_identity_mismatch_fails_closed() -> None:
    retrieved = _message(message_id="<retr@example.com>")
    session = FakePop3Session(
        {1: retrieved},
        top_messages={1: _message(message_id="<top@example.com>")},
    )

    with pytest.raises(HiworksMailProtocolError, match="different messages"):
        HiworksIncrementalMailReader(
            lambda: session,
            max_message_bytes=100_000,
        ).collect()
    assert session.calls == ["uidl", "list", "top:1:0", "retr:1", "close"]


def test_mime_failure_is_isolated_and_does_not_block_later_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakePop3Session(
        {
            1: _message(message_id="<poison@example.com>"),
            2: _message(message_id="<normal@example.com>", body="normal body"),
        }
    )
    original_extract = hiworks_mail._extract_content

    def extract_or_fail(message: object, **limits: int):
        if str(message.get("Message-ID")) == "<poison@example.com>":  # type: ignore[attr-defined]
            raise RecursionError("nested MIME")
        return original_extract(message, **limits)  # type: ignore[arg-type]

    monkeypatch.setattr(hiworks_mail, "_extract_content", extract_or_fail)
    batch = HiworksIncrementalMailReader(
        lambda: session,
        max_message_bytes=100_000,
    ).collect()

    assert batch.processed_uidls == ("uid-1", "uid-2")
    assert batch.checkpoint.seen_uidls == ("uid-1", "uid-2")
    assert batch.messages[0].skip_reason == "mime_parse_failed"
    assert batch.messages[0].body_loaded is False
    assert batch.messages[1].body_text == "normal body"


def test_collection_limits_and_uidl_checkpoint_are_validated() -> None:
    with pytest.raises(ValueError, match="positive"):
        HiworksIncrementalMailReader(lambda: FakePop3Session({}), max_messages=0)
    with pytest.raises(HiworksMailProtocolError, match="UIDL"):
        HiworksIncrementalMailReader(lambda: FakePop3Session({})).collect(
            seen_uidls={"contains space"},
        )
