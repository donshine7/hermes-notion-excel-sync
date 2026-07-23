from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from notion_excel_sync.adapters.hiworks_mail import HiworksMailMessage
from notion_excel_sync.models import SourceRecord, WorkbookSnapshot, sha256_json


class WikiProjectionError(RuntimeError):
    """Raised when a generated Wiki layer cannot be published atomically."""


@dataclass(frozen=True, slots=True)
class WikiProjection:
    layer: str
    generation: str
    output_path: Path
    document_count: int


@dataclass(frozen=True, slots=True)
class MailAnalysis:
    uidl: str
    message_id: str
    subject: str
    sender: str
    sent_at: str | None
    case_numbers: tuple[str, ...]
    action_signals: tuple[str, ...]
    summary: str
    body_text: str
    body_truncated: bool
    skip_reason: str | None
    attachments: tuple[dict[str, object], ...]

    @property
    def content_hash(self) -> str:
        return sha256_json(
            {
                "uidl": self.uidl,
                "message_id": self.message_id,
                "subject": self.subject,
                "sender": self.sender,
                "sent_at": self.sent_at,
                "case_numbers": self.case_numbers,
                "action_signals": self.action_signals,
                "summary": self.summary,
                "body_text": self.body_text,
                "body_truncated": self.body_truncated,
                "skip_reason": self.skip_reason,
                "attachments": self.attachments,
            }
        )


_UNSAFE_FILENAME = re.compile(r"[^0-9A-Za-z._-]+")
_CASE_NUMBER_PATTERNS = (
    re.compile(r"\b(?:10|20|30|40)-\d{4}-\d{4,10}\b", re.IGNORECASE),
    re.compile(r"\bPCT/[A-Z]{2}\d{4}/\d{4,10}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{1,6}-\d{2,10}\b", re.IGNORECASE),
)
_ACTION_KEYWORDS = {
    "기한": ("기한", "마감", "deadline", "due date"),
    "비용": ("비용", "청구", "입금", "invoice", "payment"),
    "증빙": ("증빙", "지원사업", "바우처", "evidence"),
    "응답요청": ("회신", "답변", "검토 요청", "reply", "respond"),
    "결정": ("등록결정", "거절결정", "결정서", "decision"),
}


def _safe_filename(value: str, *, fallback: str) -> str:
    normalized = _UNSAFE_FILENAME.sub("-", value.strip()).strip(".-")
    return (normalized[:80] or fallback).casefold()


def _markdown_value(value: object, *, limit: int = 500) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    rendered = rendered.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def analyze_mail_message(message: HiworksMailMessage) -> MailAnalysis:
    combined = f"{message.header.subject}\n{message.body_text}"
    case_numbers = {
        match.group(0).upper()
        for pattern in _CASE_NUMBER_PATTERNS
        for match in pattern.finditer(combined)
    }
    folded = combined.casefold()
    action_signals = tuple(
        label
        for label, keywords in _ACTION_KEYWORDS.items()
        if any(keyword.casefold() in folded for keyword in keywords)
    )
    body_summary = " ".join(message.body_text.split())
    summary_parts = [message.header.subject.strip()]
    if body_summary:
        summary_parts.append(body_summary[:500])
    summary = " — ".join(part for part in summary_parts if part) or "(제목 없음)"
    attachments = tuple(
        {
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "disposition": attachment.disposition,
            "content_id": attachment.content_id,
            "size_bytes": attachment.size_bytes,
            "exceeds_size_limit": attachment.exceeds_size_limit,
            "is_inline": attachment.is_inline,
        }
        for attachment in message.attachments
    )
    return MailAnalysis(
        uidl=message.header.uidl,
        message_id=message.header.message_id,
        subject=message.header.subject,
        sender=message.header.sender,
        sent_at=(
            message.header.sent_at.isoformat()
            if message.header.sent_at is not None
            else None
        ),
        case_numbers=tuple(sorted(case_numbers)),
        action_signals=action_signals,
        summary=summary,
        body_text=message.body_text,
        body_truncated=message.body_truncated,
        skip_reason=message.skip_reason,
        attachments=attachments,
    )


class WikiOutputStore:
    """Publish generated Wiki views outside the immutable source tree.

    Each layer uses immutable generation directories and a small ``CURRENT``
    pointer. A failed build never replaces the last complete generation.
    """

    def __init__(
        self,
        output_root: str | Path,
        *,
        source_root: str | Path,
    ) -> None:
        output = Path(output_root)
        source = Path(source_root)
        if not output.is_absolute() or not source.is_absolute():
            raise ValueError("Wiki output and source roots must be absolute")
        output = Path(os.path.abspath(output))
        source = Path(os.path.abspath(source))
        try:
            common = os.path.commonpath((output, source))
        except ValueError:
            common = ""
        if os.path.normcase(common) in {
            os.path.normcase(os.fspath(output)),
            os.path.normcase(os.fspath(source)),
        }:
            raise WikiProjectionError("Wiki output must not overlap the source root")
        self.output_root = output
        self.source_root = source

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _write_text(cls, path: Path, value: str) -> None:
        cls._write_bytes(path, value.encode("utf-8"))

    def _publish_staging(
        self,
        *,
        layer: str,
        generation: str,
        staging: Path,
        document_count: int,
    ) -> WikiProjection:
        layer_root = self.output_root / layer
        generations = layer_root / "generations"
        generations.mkdir(parents=True, exist_ok=True)
        target = generations / generation
        if target.exists():
            shutil.rmtree(staging)
        else:
            os.replace(staging, target)
        self._write_text(layer_root / "CURRENT", generation + "\n")
        return WikiProjection(layer, generation, target, document_count)

    def publish_excel(
        self,
        snapshot: WorkbookSnapshot,
        *,
        baseline_at: str,
        page_size: int = 500,
    ) -> WikiProjection:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        generation = sha256_json(
            {
                "schema": 1,
                "version_id": snapshot.version_id,
                "file_hash": snapshot.file_hash,
                "records": [
                    (record.key, record.content_hash) for record in snapshot.records
                ],
            }
        )
        layer_root = self.output_root / "excel"
        target = layer_root / "generations" / generation
        if target.is_dir():
            self._write_text(layer_root / "CURRENT", generation + "\n")
            return WikiProjection("excel", generation, target, len(snapshot.records))

        staging = layer_root / f".staging-{uuid4().hex}"
        try:
            staging.mkdir(parents=True, exist_ok=False)
            by_sheet: dict[str, list[SourceRecord]] = {}
            for record in snapshot.records:
                by_sheet.setdefault(record.sheet, []).append(record)
            manifest = {
                "schema_version": 1,
                "authority": "excel_source",
                "baseline_at": baseline_at,
                "captured_at": snapshot.captured_at.isoformat(),
                "version_id": snapshot.version_id,
                "file_hash": snapshot.file_hash,
                "record_count": len(snapshot.records),
                "sheets": {
                    sheet: len(records) for sheet, records in sorted(by_sheet.items())
                },
                "generation": generation,
            }
            self._write_text(
                staging / "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
            with (staging / "records.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
                for record in sorted(
                    snapshot.records,
                    key=lambda item: (item.sheet.casefold(), item.row, item.key),
                ):
                    stream.write(
                        json.dumps(
                            {
                                "key": record.key,
                                "sheet": record.sheet,
                                "row": record.row,
                                "values": record.values,
                                "content_hash": record.content_hash,
                                "source_version_id": snapshot.version_id,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        )
                        + "\n"
                    )
            sheets_root = staging / "sheets"
            sheets_root.mkdir()
            index_lines = [
                "# Excel 업무 Wiki",
                "",
                f"- 기준 시각: {baseline_at}",
                f"- 원본 버전: `{snapshot.version_id}`",
                f"- 레코드: {len(snapshot.records)}건",
                "- 이 계층은 원본 Excel을 변경하지 않고 생성된 읽기 전용 투영입니다.",
                "",
                "## 시트",
                "",
            ]
            for sheet, records in sorted(by_sheet.items()):
                ordered = sorted(records, key=lambda item: (item.row, item.key))
                sheet_slug = _safe_filename(
                    sheet,
                    fallback=sha256_json(sheet)[:16],
                )
                page_names: list[str] = []
                for page_number, offset in enumerate(
                    range(0, len(ordered), page_size),
                    start=1,
                ):
                    page_name = f"{sheet_slug}-{page_number:04d}.md"
                    page_names.append(page_name)
                    lines = [
                        f"# {sheet} · {page_number}",
                        "",
                        "| 행 | 사건/레코드 키 | 내용 |",
                        "|---:|---|---|",
                    ]
                    for record in ordered[offset : offset + page_size]:
                        lines.append(
                            f"| {record.row} | `{record.key}` | "
                            f"{_markdown_value(record.values)} |"
                        )
                    self._write_text(sheets_root / page_name, "\n".join(lines) + "\n")
                links = ", ".join(f"[{name}](sheets/{name})" for name in page_names)
                index_lines.append(f"- {sheet}: {len(records)}건 — {links}")
            self._write_text(staging / "README.md", "\n".join(index_lines) + "\n")
            return self._publish_staging(
                layer="excel",
                generation=generation,
                staging=staging,
                document_count=len(snapshot.records),
            )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def publish_data_status(
        self,
        *,
        generation: str,
        baseline_at: str,
        catalog_entries: int,
        indexed_documents: int,
        quarantined: int,
        full_rebuild: bool,
        skipped_reasons: dict[str, int],
    ) -> WikiProjection:
        layer_root = self.output_root / "data"
        target = layer_root / "generations" / generation
        if target.is_dir():
            self._write_text(layer_root / "CURRENT", generation + "\n")
            return WikiProjection("data", generation, target, indexed_documents)
        staging = layer_root / f".staging-{uuid4().hex}"
        try:
            staging.mkdir(parents=True, exist_ok=False)
            payload = {
                "schema_version": 1,
                "authority": "local_data_source",
                "baseline_at": baseline_at,
                "generation": generation,
                "catalog_entries": catalog_entries,
                "indexed_documents": indexed_documents,
                "quarantined": quarantined,
                "full_rebuild": full_rebuild,
                "skipped_reasons": dict(sorted(skipped_reasons.items())),
            }
            self._write_text(
                staging / "manifest.json",
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
            )
            lines = [
                "# 데이터 원본 Wiki 상태",
                "",
                f"- 기준 시각: {baseline_at}",
                f"- 세대: `{generation}`",
                f"- 목록 항목: {catalog_entries}건",
                f"- 인덱싱 문서: {indexed_documents}건",
                f"- 격리/제외: {quarantined}건",
                f"- 방식: {'전체 구축' if full_rebuild else '변경분 반영'}",
                "",
                "원본 파일은 복사·수정하지 않았으며, 상세 본문은 보호된 로컬 검색 "
                "인덱스에서만 사용합니다.",
            ]
            self._write_text(staging / "README.md", "\n".join(lines) + "\n")
            return self._publish_staging(
                layer="data",
                generation=generation,
                staging=staging,
                document_count=indexed_documents,
            )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def publish_mail_messages(
        self,
        messages: Iterable[HiworksMailMessage],
        *,
        baseline_at: str,
        collected_at: datetime,
    ) -> WikiProjection:
        mail_root = self.output_root / "mail"
        message_root = mail_root / "messages"
        message_root.mkdir(parents=True, exist_ok=True)
        analyses = [analyze_mail_message(message) for message in messages]
        for analysis in analyses:
            locator = sha256_json({"uidl": analysis.uidl})[:32]
            payload = {
                "schema_version": 1,
                "authority": "hiworks_email",
                "untrusted_content": True,
                "baseline_at": baseline_at,
                "collected_at": collected_at.isoformat(),
                "uidl": analysis.uidl,
                "message_id": analysis.message_id,
                "subject": analysis.subject,
                "sender": analysis.sender,
                "sent_at": analysis.sent_at,
                "case_numbers": analysis.case_numbers,
                "action_signals": analysis.action_signals,
                "summary": analysis.summary,
                "body_text": analysis.body_text,
                "body_truncated": analysis.body_truncated,
                "skip_reason": analysis.skip_reason,
                "attachments": analysis.attachments,
                "content_hash": analysis.content_hash,
            }
            self._write_text(
                message_root / f"{locator}.json",
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
            )
            lines = [
                f"# {analysis.subject or '(제목 없음)'}",
                "",
                "> 이 문서는 Hiworks 메일에서 수집된 신뢰되지 않은 참고정보입니다. "
                "Excel 원본 사실을 대체하지 않습니다.",
                "",
                f"- 발신자: {_markdown_value(analysis.sender)}",
                f"- 발송시각: {analysis.sent_at or '확인 불가'}",
                f"- 사건번호: {', '.join(analysis.case_numbers) or '자동 식별 없음'}",
                f"- 조치 신호: {', '.join(analysis.action_signals) or '없음'}",
                f"- 요약: {_markdown_value(analysis.summary)}",
                f"- 본문 잘림: {'예' if analysis.body_truncated else '아니오'}",
                f"- 처리 제한: {analysis.skip_reason or '없음'}",
                "",
                "## 본문",
                "",
                analysis.body_text or "(본문을 불러오지 않음)",
                "",
                "## 첨부 메타데이터",
                "",
                _markdown_value(analysis.attachments, limit=5000),
            ]
            self._write_text(message_root / f"{locator}.md", "\n".join(lines) + "\n")

        catalog_rows: list[dict[str, object]] = []
        for metadata_path in sorted(message_root.glob("*.json")):
            try:
                row = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WikiProjectionError("Mail Wiki metadata is invalid") from exc
            if not isinstance(row, dict) or not row.get("content_hash"):
                raise WikiProjectionError("Mail Wiki metadata is invalid")
            catalog_rows.append(row)
        generation = sha256_json(
            [
                (str(row.get("uidl")), str(row.get("content_hash")))
                for row in catalog_rows
            ]
        )
        target = mail_root / "generations" / generation
        if target.is_dir():
            self._write_text(mail_root / "CURRENT", generation + "\n")
            return WikiProjection("mail", generation, target, len(catalog_rows))
        staging = mail_root / f".staging-{uuid4().hex}"
        try:
            staging.mkdir(parents=True, exist_ok=False)
            manifest = {
                "schema_version": 1,
                "authority": "hiworks_email",
                "untrusted_content": True,
                "baseline_at": baseline_at,
                "collected_at": collected_at.isoformat(),
                "generation": generation,
                "message_count": len(catalog_rows),
                "messages": [
                    {
                        "uidl_hash": sha256_json({"uidl": row.get("uidl")})[:32],
                        "sent_at": row.get("sent_at"),
                        "case_numbers": row.get("case_numbers", []),
                        "action_signals": row.get("action_signals", []),
                        "content_hash": row.get("content_hash"),
                    }
                    for row in catalog_rows
                ],
            }
            self._write_text(
                staging / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
            )
            self._write_text(
                staging / "README.md",
                "\n".join(
                    [
                        "# Hiworks 메일 Wiki",
                        "",
                        f"- 최초 기준 시각: {baseline_at}",
                        f"- 누적 메일: {len(catalog_rows)}건",
                        f"- 세대: `{generation}`",
                        "",
                        "본문은 `../messages/`에 UIDL 해시 파일로 저장되며 첨부파일 "
                        "payload는 저장하지 않습니다.",
                    ]
                )
                + "\n",
            )
            return self._publish_staging(
                layer="mail",
                generation=generation,
                staging=staging,
                document_count=len(catalog_rows),
            )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def publish_root_readme(self, *, updated_at: datetime) -> None:
        self._write_text(
            self.output_root / "README.md",
            "\n".join(
                [
                    "# 업무 LLM Wiki",
                    "",
                    f"최근 갱신: {updated_at.isoformat()}",
                    "",
                    "- `excel/`: 원본 Excel의 현재 읽기 전용 투영",
                    "- `data/`: 데이터 원본 폴더의 보호된 검색 인덱스 상태",
                    "- `mail/`: 최초 구축 시점 이후 Hiworks 메일 분석",
                    "- `corrections/`: Telegram에서 승인된 사용자 보정 오버레이",
                    "",
                    "원본 Excel과 원본 폴더는 이 Wiki 생성 과정에서 변경되지 않습니다.",
                ]
            )
            + "\n",
        )
