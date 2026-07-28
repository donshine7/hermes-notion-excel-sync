from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from notion_excel_sync.adapters.hiworks_mail import (
    HiworksAttachmentMetadata,
    HiworksMailBatch,
    HiworksMailCheckpoint,
    HiworksMailHeader,
    HiworksMailMessage,
)
from notion_excel_sync.config import EmailConfig, WikiConfig
from notion_excel_sync.knowledge.projection import WikiProjection
from notion_excel_sync.workflow.mail_ingestion import (
    HIWORKS_MAIL_CHECKPOINT_KEY,
    WIKI_BOOTSTRAP_CHECKPOINT_KEY,
    HiworksMailCheckpointError,
    HiworksMailIngestionService,
    HiworksMailIngestionStatus,
)


class FakeCheckpoints:
    def __init__(
        self,
        values: dict[str, dict[str, Any]] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.values = values or {}
        self.events = events if events is not None else []
        self.saves: list[tuple[str, dict[str, Any], str | None]] = []

    def get_ingestion_checkpoint(self, stream_key: str) -> dict[str, Any] | None:
        value = self.values.get(stream_key)
        return dict(value) if value is not None else None

    def save_ingestion_checkpoint(
        self,
        stream_key: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> None:
        self.events.append("save")
        self.saves.append((stream_key, dict(payload), actor))
        self.values[stream_key] = dict(payload)


@dataclass
class FakeReader:
    batch: HiworksMailBatch
    calls: list[dict[str, Any]]

    def collect(self, **kwargs: Any) -> HiworksMailBatch:
        self.calls.append(kwargs)
        return self.batch


class FakeOutput:
    def __init__(
        self,
        events: list[str],
        captured: list[tuple[HiworksMailMessage, ...]],
        *,
        fail: bool = False,
    ) -> None:
        self.events = events
        self.captured = captured
        self.fail = fail

    def publish_mail_messages(
        self,
        messages: tuple[HiworksMailMessage, ...],
        *,
        baseline_at: str,
        collected_at: datetime,
    ) -> WikiProjection:
        self.events.append("publish")
        self.captured.append(messages)
        if self.fail:
            raise RuntimeError("synthetic publish failure")
        assert baseline_at == "2026-07-23T00:00:00+00:00"
        assert collected_at == datetime(2026, 7, 24, tzinfo=UTC)
        return WikiProjection(
            layer="mail",
            generation="mail-generation-1",
            output_path=Path("C:/synthetic/wiki/mail"),
            document_count=len(messages),
        )


def _message(
    uidl: str,
    *,
    sent_at: datetime | None,
    skip_reason: str | None = None,
    attachment: bool = False,
) -> HiworksMailMessage:
    attachments = (
        HiworksAttachmentMetadata(
            filename="evidence.pdf",
            content_type="application/pdf",
            disposition="attachment",
            content_id=None,
            transfer_encoding="base64",
            size_bytes=123,
            exceeds_size_limit=False,
            is_inline=False,
        ),
    ) if attachment else ()
    return HiworksMailMessage(
        header=HiworksMailHeader(
            uidl=uidl,
            pop3_number=1,
            size_bytes=500,
            message_id=f"<{uidl}@example.com>",
            sent_at=sent_at,
            subject="Synthetic case update",
            sender="sender@example.com",
            to="recipient@example.com",
            cc="",
        ),
        body_text="Synthetic body",
        body_loaded=True,
        body_truncated=False,
        attachments=attachments,
        attachments_complete=True,
        skip_reason=skip_reason,
    )


def _active_configs(root: Path) -> tuple[EmailConfig, WikiConfig]:
    source = root / "source"
    output = root / "wiki"
    source.mkdir()
    return (
        EmailConfig(
            enabled=True,
            username="reader@example.com",
            password_secret="HIWORKS_MAIL_PASSWORD",
        ),
        WikiConfig(
            enabled=True,
            source_root=source,
            output_root=output,
        ),
    )


@pytest.mark.parametrize(
    ("enabled", "bootstrap", "expected"),
    [
        (False, None, HiworksMailIngestionStatus.DISABLED),
        (
            True,
            {"phase": "started", "t0": "2026-07-23T00:00:00+00:00"},
            HiworksMailIngestionStatus.WAITING_FOR_BOOTSTRAP,
        ),
    ],
)
def test_disabled_or_incomplete_bootstrap_skips_without_reading_secret(
    enabled: bool,
    bootstrap: dict[str, Any] | None,
    expected: HiworksMailIngestionStatus,
) -> None:
    checkpoints = FakeCheckpoints(
        {WIKI_BOOTSTRAP_CHECKPOINT_KEY: bootstrap} if bootstrap else {}
    )

    def forbidden_secret(_name: str) -> str:
        raise AssertionError("secret must not be read")

    result = HiworksMailIngestionService(
        email=EmailConfig(enabled=enabled),
        wiki=WikiConfig(),
        checkpoints=checkpoints,
        secret_resolver=forbidden_secret,
    ).ingest()

    assert result.status is expected
    assert result.new_count == 0
    assert result.generation is None
    assert result.remaining == 0
    assert checkpoints.saves == []


def test_success_restores_checkpoint_filters_t0_and_publishes_before_save() -> None:
    with tempfile.TemporaryDirectory() as folder:
        email, wiki = _active_configs(Path(folder))
        events: list[str] = []
        checkpoints = FakeCheckpoints(
            {
                WIKI_BOOTSTRAP_CHECKPOINT_KEY: {
                    "phase": "complete",
                    "t0": "2026-07-23T09:00:00+09:00",
                },
                HIWORKS_MAIL_CHECKPOINT_KEY: {
                    "baseline_at": "2026-07-23T00:00:00Z",
                    "seen_uidls": ["uid-old"],
                    "seen_message_ids": ["<old@example.com>"],
                },
            },
            events,
        )
        after = _message(
            "uid-after",
            sent_at=datetime(2026, 7, 23, 0, 0, 1, tzinfo=UTC),
            attachment=True,
        )
        equal = _message(
            "uid-equal",
            sent_at=datetime(2026, 7, 23, tzinfo=UTC),
        )
        unknown = _message("uid-unknown", sent_at=None)
        before = _message(
            "uid-before",
            sent_at=datetime(2026, 7, 22, tzinfo=UTC),
            skip_reason="before_baseline",
        )
        batch = HiworksMailBatch(
            messages=(after, equal, unknown, before),
            processed_uidls=("uid-after", "uid-equal", "uid-unknown", "uid-before"),
            duplicate_uidls=(),
            remaining_new_count=3,
            checkpoint=HiworksMailCheckpoint(
                seen_uidls=(
                    "uid-old",
                    "uid-after",
                    "uid-equal",
                    "uid-unknown",
                    "uid-before",
                ),
                seen_message_ids=(
                    "<old@example.com>",
                    "<uid-after@example.com>",
                ),
            ),
        )
        reader_calls: list[dict[str, Any]] = []
        reader = FakeReader(batch=batch, calls=reader_calls)
        credentials: list[Any] = []

        def session_factory(value: Any) -> object:
            credentials.append(value)
            return object()

        def reader_factory(open_session: Any, **limits: int) -> FakeReader:
            assert open_session() is not None
            assert limits["max_message_bytes"] == email.max_message_bytes
            return reader

        captured: list[tuple[HiworksMailMessage, ...]] = []
        result = HiworksMailIngestionService(
            email=email,
            wiki=wiki,
            checkpoints=checkpoints,
            secret_resolver=lambda name: (
                "synthetic-password"
                if name == "HIWORKS_MAIL_PASSWORD"
                else None
            ),
            session_factory=session_factory,  # type: ignore[arg-type]
            reader_factory=reader_factory,
            output_store_factory=lambda *_args, **_kwargs: FakeOutput(
                events,
                captured,
            ),
            clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
        ).ingest(actor="telegram-user")

        assert result.status is HiworksMailIngestionStatus.COMPLETED
        assert result.new_count == 1
        assert result.generation == "mail-generation-1"
        assert result.remaining == 3
        assert captured == [(after,)]
        assert captured[0][0].attachments[0].filename == "evidence.pdf"
        assert events == ["publish", "save"]
        assert reader_calls == [
            {
                "seen_uidls": ("uid-old",),
                "seen_message_ids": ("<old@example.com>",),
                "sent_after": datetime(2026, 7, 23, tzinfo=UTC),
            }
        ]
        assert credentials[0].email_address == "reader@example.com"
        assert checkpoints.saves[0][0] == HIWORKS_MAIL_CHECKPOINT_KEY
        saved = checkpoints.saves[0][1]
        assert saved["seen_uidls"] == list(batch.checkpoint.seen_uidls)
        assert saved["generation"] == "mail-generation-1"
        assert saved["published_count"] == 1
        assert checkpoints.saves[0][2] == "telegram-user"


def test_publish_failure_never_advances_mail_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as folder:
        email, wiki = _active_configs(Path(folder))
        events: list[str] = []
        original = {
            "baseline_at": "2026-07-23T00:00:00+00:00",
            "seen_uidls": ["uid-old"],
            "seen_message_ids": [],
        }
        checkpoints = FakeCheckpoints(
            {
                WIKI_BOOTSTRAP_CHECKPOINT_KEY: {
                    "phase": "complete",
                    "t0": "2026-07-23T00:00:00+00:00",
                },
                HIWORKS_MAIL_CHECKPOINT_KEY: original,
            },
            events,
        )
        message = _message(
            "uid-new",
            sent_at=datetime(2026, 7, 23, 0, 0, 1, tzinfo=UTC),
        )
        reader = FakeReader(
            batch=HiworksMailBatch(
                messages=(message,),
                processed_uidls=("uid-new",),
                duplicate_uidls=(),
                remaining_new_count=0,
                checkpoint=HiworksMailCheckpoint(
                    seen_uidls=("uid-old", "uid-new"),
                    seen_message_ids=("<uid-new@example.com>",),
                ),
            ),
            calls=[],
        )

        with pytest.raises(RuntimeError, match="synthetic publish failure"):
            HiworksMailIngestionService(
                email=email,
                wiki=wiki,
                checkpoints=checkpoints,
                secret_resolver=lambda _name: "synthetic-password",
                reader_factory=lambda *_args, **_kwargs: reader,
                output_store_factory=lambda *_args, **_kwargs: FakeOutput(
                    events,
                    [],
                    fail=True,
                ),
                clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
            ).ingest()

        assert events == ["publish"]
        assert checkpoints.saves == []
        assert checkpoints.values[HIWORKS_MAIL_CHECKPOINT_KEY] == original


@pytest.mark.parametrize(
    "t0",
    ["2026-07-23T00:00:00", "not-a-timestamp", ""],
)
def test_complete_bootstrap_requires_trustworthy_timezone_aware_t0(t0: str) -> None:
    checkpoints = FakeCheckpoints(
        {
            WIKI_BOOTSTRAP_CHECKPOINT_KEY: {
                "phase": "complete",
                "t0": t0,
            }
        }
    )

    with pytest.raises(HiworksMailCheckpointError):
        HiworksMailIngestionService(
            email=EmailConfig(enabled=True),
            wiki=WikiConfig(),
            checkpoints=checkpoints,
            secret_resolver=lambda _name: "must-not-be-used",
        ).ingest()
    assert checkpoints.saves == []
