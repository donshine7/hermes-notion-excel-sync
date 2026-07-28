from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from notion_excel_sync.adapters.hiworks_mail import (
    HiworksAttachmentMetadata,
    HiworksMailHeader,
    HiworksMailMessage,
)
from notion_excel_sync.knowledge.projection import (
    WikiOutputStore,
    WikiProjectionError,
)
from notion_excel_sync.models import SourceRecord, WorkbookSnapshot


def _snapshot(version: str = "v1") -> WorkbookSnapshot:
    return WorkbookSnapshot(
        drive_id="local",
        item_id="local:item",
        version_id=version,
        file_hash=f"hash-{version}",
        captured_at=datetime(2026, 7, 23, tzinfo=UTC),
        records=[
            SourceRecord(
                key="2026::A-1",
                sheet="2026",
                row=2,
                values={"사건번호": "A-1", "상태": "진행"},
            ),
            SourceRecord(
                key="등록결정::B-1",
                sheet="등록결정",
                row=3,
                values={"사건번호": "B-1", "비용": 1000},
            ),
        ],
    )


def test_excel_projection_is_generation_based_and_contains_no_source_copy() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / "source"
        output = root / "wiki"
        source.mkdir()
        store = WikiOutputStore(output, source_root=source)

        first = store.publish_excel(
            _snapshot(),
            baseline_at="2026-07-23T10:00:00+09:00",
            page_size=1,
        )
        repeated = store.publish_excel(
            _snapshot(),
            baseline_at="2026-07-23T10:00:00+09:00",
            page_size=1,
        )

        assert first.generation == repeated.generation
        assert (output / "excel" / "CURRENT").read_text(encoding="utf-8").strip()
        manifest = json.loads(
            (first.output_path / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["record_count"] == 2
        assert manifest["authority"] == "excel_source"
        assert len(list((first.output_path / "sheets").glob("*.md"))) == 2
        assert not list(output.rglob("*.xlsx"))


def test_data_status_and_root_readme_publish_only_to_output() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / "source"
        output = root / "wiki"
        source.mkdir()
        marker = source / "immutable.txt"
        marker.write_text("original", encoding="utf-8")
        store = WikiOutputStore(output, source_root=source)

        projection = store.publish_data_status(
            generation="generation-1",
            baseline_at="2026-07-23T10:00:00+09:00",
            catalog_entries=12,
            indexed_documents=8,
            quarantined=4,
            full_rebuild=True,
            skipped_reasons={"unsupported_extension": 4},
        )
        store.publish_root_readme(updated_at=datetime(2026, 7, 23, tzinfo=UTC))

        assert projection.document_count == 8
        assert (output / "README.md").is_file()
        assert marker.read_text(encoding="utf-8") == "original"
        assert list(source.iterdir()) == [marker]


def test_mail_projection_analyzes_cases_without_attachment_payloads() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / "source"
        output = root / "wiki"
        source.mkdir()
        store = WikiOutputStore(output, source_root=source)
        message = HiworksMailMessage(
            header=HiworksMailHeader(
                uidl="uid-1",
                pop3_number=1,
                size_bytes=100,
                message_id="<one@example.com>",
                sent_at=datetime(2026, 7, 23, tzinfo=UTC),
                subject="10-2026-1234567 등록결정 회신 요청",
                sender="sender@example.com",
                to="receiver@example.com",
                cc="",
            ),
            body_text="결정서를 확인하고 비용 증빙을 회신해 주세요.",
            body_loaded=True,
            body_truncated=False,
            attachments=(
                HiworksAttachmentMetadata(
                    filename="decision.pdf",
                    content_type="application/pdf",
                    disposition="attachment",
                    content_id=None,
                    transfer_encoding="base64",
                    size_bytes=1234,
                    exceeds_size_limit=False,
                    is_inline=False,
                ),
            ),
            attachments_complete=True,
        )

        projection = store.publish_mail_messages(
            [message],
            baseline_at="2026-07-23T10:00:00+09:00",
            collected_at=datetime(2026, 7, 23, tzinfo=UTC),
            ai_analyses={
                "uid-1": {
                    "summary": "등록결정 메일 검토",
                    "claims": {"event_type": "등록결정"},
                    "needs_review": True,
                    "overall_confidence": 0.91,
                    "analysis_digest": "b" * 64,
                }
            },
        )

        assert projection.document_count == 1
        metadata = json.loads(
            next((output / "mail" / "messages").glob("*.json")).read_text(
                encoding="utf-8"
            )
        )
        assert metadata["case_numbers"] == ["10-2026-1234567"]
        assert {"결정", "비용", "증빙", "응답요청"}.issubset(
            set(metadata["action_signals"])
        )
        assert "payload" not in metadata
        assert all("payload" not in item for item in metadata["attachments"])
        assert metadata["ai_analysis"]["summary"] == "등록결정 메일 검토"
        markdown = next((output / "mail" / "messages").glob("*.md")).read_text(
            encoding="utf-8"
        )
        assert "구조화 AI 분석" in markdown
        assert "Excel 사실을 대체하지 않습니다" in markdown
        assert not list(output.rglob("*.pdf"))


def test_output_must_not_overlap_source() -> None:
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "source"
        source.mkdir()

        with pytest.raises(WikiProjectionError, match="must not overlap"):
            WikiOutputStore(source / "wiki", source_root=source)
