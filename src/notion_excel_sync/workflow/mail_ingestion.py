from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from notion_excel_sync.adapters.hiworks_mail import (
    HiworksIncrementalMailReader,
    HiworksMailBatch,
    HiworksMailMessage,
    HiworksPop3Credentials,
    HiworksPop3SslClient,
    Pop3ReadOnlySession,
)
from notion_excel_sync.config import EmailConfig, WikiConfig
from notion_excel_sync.knowledge.projection import WikiOutputStore, WikiProjection

from notion_excel_sync.ai.service import (
    StructuredAIService,
    compact_analysis_payload,
)


WIKI_BOOTSTRAP_CHECKPOINT_KEY = "wiki-bootstrap"
HIWORKS_MAIL_CHECKPOINT_KEY = "email-hiworks"


class HiworksMailIngestionError(RuntimeError):
    """Base error for the trusted, read-only Hiworks ingestion workflow."""


class HiworksMailIngestionConfigurationError(HiworksMailIngestionError):
    """Raised when active mail ingestion is not configured safely."""


class HiworksMailCheckpointError(HiworksMailIngestionError):
    """Raised when a persisted mail or Wiki checkpoint cannot be trusted."""


class HiworksMailIngestionStatus(StrEnum):
    DISABLED = "disabled"
    WAITING_FOR_BOOTSTRAP = "waiting_for_bootstrap"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class HiworksMailIngestionResult:
    status: HiworksMailIngestionStatus
    new_count: int
    generation: str | None
    remaining: int
    ai_analyzed_count: int = 0
    ai_failure_count: int = 0


class IngestionCheckpointStore(Protocol):
    def get_ingestion_checkpoint(self, stream_key: str) -> dict[str, Any] | None: ...

    def save_ingestion_checkpoint(
        self,
        stream_key: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> None: ...


class IncrementalMailReader(Protocol):
    def collect(
        self,
        *,
        seen_uidls: Sequence[str] = (),
        seen_message_ids: Sequence[str] = (),
        sent_after: datetime | None = None,
    ) -> HiworksMailBatch: ...


class MailWikiOutput(Protocol):
    def publish_mail_messages(
        self,
        messages: Sequence[HiworksMailMessage],
        *,
        baseline_at: str,
        collected_at: datetime,
    ) -> WikiProjection: ...


SecretResolver = Callable[[str], str | None]
SessionFactory = Callable[[HiworksPop3Credentials], Pop3ReadOnlySession]
ReaderFactory = Callable[..., IncrementalMailReader]
OutputStoreFactory = Callable[..., MailWikiOutput]


def _default_session_factory(
    credentials: HiworksPop3Credentials,
) -> Pop3ReadOnlySession:
    return HiworksPop3SslClient(credentials)


def _aware_utc(value: object, *, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise HiworksMailCheckpointError(f"{label} is missing")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HiworksMailCheckpointError(
                f"{label} must be an ISO-8601 timestamp"
            ) from exc
    if parsed.tzinfo is None:
        raise HiworksMailCheckpointError(
            f"{label} must include an explicit timezone"
        )
    return parsed.astimezone(UTC)


def _checkpoint_values(
    checkpoint: Mapping[str, Any] | None,
    key: str,
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    raw = checkpoint.get(key, ())
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise HiworksMailCheckpointError(f"Mail checkpoint {key} must be a list")
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise HiworksMailCheckpointError(
                f"Mail checkpoint {key} contains an invalid value"
            )
        values.append(value)
    if len(values) != len(set(values)):
        raise HiworksMailCheckpointError(
            f"Mail checkpoint {key} contains duplicate values"
        )
    return tuple(values)


def _strictly_after(
    message: HiworksMailMessage,
    baseline: datetime,
) -> bool:
    if message.skip_reason == "before_baseline":
        return False
    sent_at = message.header.sent_at
    if sent_at is None or sent_at.tzinfo is None:
        return False
    return sent_at.astimezone(UTC) > baseline


class HiworksMailIngestionService:
    """Publish post-bootstrap Hiworks mail to the supplementary Wiki layer.

    This workflow has no Notion client and no source-file writer. Its only
    durable mutations are generated Wiki output and the independent
    ``email-hiworks`` ingestion checkpoint.
    """

    def __init__(
        self,
        *,
        email: EmailConfig,
        wiki: WikiConfig,
        checkpoints: IngestionCheckpointStore,
        secret_resolver: SecretResolver,
        session_factory: SessionFactory = _default_session_factory,
        reader_factory: ReaderFactory = HiworksIncrementalMailReader,
        output_store_factory: OutputStoreFactory = WikiOutputStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ai_service: StructuredAIService | None = None,
        bootstrap_checkpoint_key: str = WIKI_BOOTSTRAP_CHECKPOINT_KEY,
        mail_checkpoint_key: str = HIWORKS_MAIL_CHECKPOINT_KEY,
    ) -> None:
        if not bootstrap_checkpoint_key.strip() or not mail_checkpoint_key.strip():
            raise ValueError("Ingestion checkpoint keys must not be empty")
        self.email = email
        self.wiki = wiki
        self.checkpoints = checkpoints
        self.secret_resolver = secret_resolver
        self.session_factory = session_factory
        self.reader_factory = reader_factory
        self.output_store_factory = output_store_factory
        self.clock = clock
        self.ai_service = ai_service
        self.bootstrap_checkpoint_key = bootstrap_checkpoint_key
        self.mail_checkpoint_key = mail_checkpoint_key

    def ingest(self, *, actor: str | None = None) -> HiworksMailIngestionResult:
        if not self.email.enabled:
            return HiworksMailIngestionResult(
                status=HiworksMailIngestionStatus.DISABLED,
                new_count=0,
                generation=None,
                remaining=0,
            )

        bootstrap = self.checkpoints.get_ingestion_checkpoint(
            self.bootstrap_checkpoint_key
        )
        if bootstrap is None or bootstrap.get("phase") != "complete":
            return HiworksMailIngestionResult(
                status=HiworksMailIngestionStatus.WAITING_FOR_BOOTSTRAP,
                new_count=0,
                generation=None,
                remaining=0,
            )

        baseline = _aware_utc(
            bootstrap.get("t0"),
            label="Wiki bootstrap t0",
        )
        collected_at = self.clock()
        if collected_at.tzinfo is None:
            raise HiworksMailIngestionConfigurationError(
                "Mail ingestion clock must return a timezone-aware datetime"
            )
        collected_at = collected_at.astimezone(UTC)

        username = (self.email.username or "").strip()
        if not username:
            raise HiworksMailIngestionConfigurationError(
                "Hiworks username is required"
            )
        if not self.email.use_ssl:
            raise HiworksMailIngestionConfigurationError(
                "Hiworks ingestion permits POP3 SSL only"
            )
        if not self.email.header_first:
            raise HiworksMailIngestionConfigurationError(
                "Hiworks ingestion requires header-first collection"
            )
        if not self.wiki.enabled:
            raise HiworksMailIngestionConfigurationError(
                "Hiworks ingestion requires the Wiki to be enabled"
            )
        if self.wiki.source_root is None or self.wiki.output_root is None:
            raise HiworksMailIngestionConfigurationError(
                "Hiworks ingestion requires Wiki source and output roots"
            )
        if not Path(self.wiki.source_root).is_absolute() or not Path(
            self.wiki.output_root
        ).is_absolute():
            raise HiworksMailIngestionConfigurationError(
                "Wiki source and output roots must be absolute"
            )

        password = self.secret_resolver(self.email.password_secret)
        if not password:
            raise HiworksMailIngestionConfigurationError(
                "Hiworks mail password is unavailable"
            )
        credentials = HiworksPop3Credentials(
            email_address=username,
            password=password,
            host=self.email.host,
            port=self.email.port,
        )

        previous = self.checkpoints.get_ingestion_checkpoint(
            self.mail_checkpoint_key
        )
        if previous is not None and "baseline_at" in previous:
            previous_baseline = _aware_utc(
                previous["baseline_at"],
                label="Hiworks checkpoint baseline_at",
            )
            if previous_baseline != baseline:
                raise HiworksMailCheckpointError(
                    "Hiworks checkpoint belongs to a different Wiki baseline"
                )
        seen_uidls = _checkpoint_values(previous, "seen_uidls")
        seen_message_ids = _checkpoint_values(previous, "seen_message_ids")

        reader = self.reader_factory(
            lambda: self.session_factory(credentials),
            max_message_bytes=self.email.max_message_bytes,
            max_body_bytes=self.email.max_body_bytes,
            max_attachment_bytes=self.email.max_attachment_bytes,
        )
        batch = reader.collect(
            seen_uidls=seen_uidls,
            seen_message_ids=seen_message_ids,
            sent_after=baseline,
        )
        publishable = tuple(
            message
            for message in batch.messages
            if _strictly_after(message, baseline)
        )

        output = self.output_store_factory(
            self.wiki.output_root,
            source_root=self.wiki.source_root,
        )
        ai_analyses: dict[str, Mapping[str, object]] = {}
        ai_completed = 0
        ai_failures = 0
        if self.ai_service is not None:
            for message in publishable:
                failures_before = self.ai_service.failure_count
                analysis = self.ai_service.analyze_mail_message(message)
                ai_failures += (
                    self.ai_service.failure_count - failures_before
                )
                if analysis is None:
                    continue
                ai_completed += 1
                if self.ai_service.rollout_mode != "shadow":
                    ai_analyses[message.header.uidl] = compact_analysis_payload(
                        analysis
                    )
        if ai_analyses:
            projection = output.publish_mail_messages(
                publishable,
                baseline_at=baseline.isoformat(),
                collected_at=collected_at,
                ai_analyses=ai_analyses,
            )
        else:
            projection = output.publish_mail_messages(
                publishable,
                baseline_at=baseline.isoformat(),
                collected_at=collected_at,
            )

        checkpoint = {
            "schema_version": 1,
            "baseline_at": baseline.isoformat(),
            "seen_uidls": list(batch.checkpoint.seen_uidls),
            "seen_message_ids": list(batch.checkpoint.seen_message_ids),
            "generation": projection.generation,
            "published_count": len(publishable),
            "remaining_new_count": batch.remaining_new_count,
            "ai_analyzed_count": ai_completed,
            "ai_failure_count": ai_failures,
            "updated_at": collected_at.isoformat(),
        }
        self.checkpoints.save_ingestion_checkpoint(
            self.mail_checkpoint_key,
            checkpoint,
            actor=actor,
        )
        return HiworksMailIngestionResult(
            status=HiworksMailIngestionStatus.COMPLETED,
            new_count=len(publishable),
            generation=projection.generation,
            remaining=batch.remaining_new_count,
            ai_analyzed_count=ai_completed,
            ai_failure_count=ai_failures,
        )


__all__ = [
    "HIWORKS_MAIL_CHECKPOINT_KEY",
    "WIKI_BOOTSTRAP_CHECKPOINT_KEY",
    "HiworksMailCheckpointError",
    "HiworksMailIngestionConfigurationError",
    "HiworksMailIngestionError",
    "HiworksMailIngestionResult",
    "HiworksMailIngestionService",
    "HiworksMailIngestionStatus",
    "IngestionCheckpointStore",
]
