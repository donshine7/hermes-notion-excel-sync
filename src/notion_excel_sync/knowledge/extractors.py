from __future__ import annotations

import csv
import io
import re
import stat
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from xml.etree import ElementTree

from notion_excel_sync.knowledge.models import EXTRACTOR_VERSION


class UnsafeDocumentError(ValueError):
    """Raised when a document violates the inert extraction contract."""


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_input_bytes: int = 20 * 1024 * 1024
    max_archive_entries: int = 5_000
    max_archive_expanded_bytes: int = 120 * 1024 * 1024
    max_entry_bytes: int = 32 * 1024 * 1024
    max_compression_ratio: int = 1_000
    max_xml_bytes: int = 24 * 1024 * 1024
    max_cells: int = 200_000
    max_output_chars: int = 2_000_000


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    warnings: tuple[str, ...] = ()
    extractor_version: str = EXTRACTOR_VERSION


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise UnsafeDocumentError("text_contains_nul")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnsafeDocumentError("unsupported_text_encoding")


class _InertHtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript", "iframe", "object"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag.casefold() in {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "iframe", "object"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif self._ignored_depth == 0 and tag.casefold() in {
            "p",
            "div",
            "li",
            "tr",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def _safe_zip(data: bytes, limits: ExtractionLimits) -> zipfile.ZipFile:
    if not data.startswith(b"PK"):
        raise UnsafeDocumentError("ooxml_magic_mismatch")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsafeDocumentError("invalid_zip_container") from exc
    infos = archive.infolist()
    if len(infos) > limits.max_archive_entries:
        archive.close()
        raise UnsafeDocumentError("archive_entry_limit")
    expanded = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name.startswith("/"):
            archive.close()
            raise UnsafeDocumentError("archive_path_traversal")
        if info.flag_bits & 0x1:
            archive.close()
            raise UnsafeDocumentError("encrypted_archive_entry")
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            archive.close()
            raise UnsafeDocumentError("archive_symlink")
        if info.file_size > limits.max_entry_bytes:
            archive.close()
            raise UnsafeDocumentError("archive_entry_too_large")
        expanded += info.file_size
        if expanded > limits.max_archive_expanded_bytes:
            archive.close()
            raise UnsafeDocumentError("archive_expanded_size_limit")
        if info.file_size and not info.compress_size:
            archive.close()
            raise UnsafeDocumentError("archive_invalid_compression_ratio")
        if info.compress_size and info.file_size / info.compress_size > limits.max_compression_ratio:
            archive.close()
            raise UnsafeDocumentError("archive_compression_ratio_limit")
    return archive


def _xml_root(raw: bytes, limits: ExtractionLimits) -> ElementTree.Element:
    if len(raw) > limits.max_xml_bytes:
        raise UnsafeDocumentError("xml_size_limit")
    # A declaration can be preceded by more than a small prefix of whitespace.
    # Scan the complete bounded XML part before handing it to the parser.
    declaration_scan = raw.replace(b"\x00", b"")
    if re.search(
        br"<!\s*(?:DOCTYPE|ENTITY)\b",
        declaration_scan,
        flags=re.IGNORECASE,
    ):
        raise UnsafeDocumentError("xml_doctype_or_entity")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise UnsafeDocumentError("invalid_xml") from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _natural_xml_key(name: str) -> tuple[str, int, str]:
    match = re.search(r"(\d+)(?=\.xml$)", name)
    return (name.rsplit("/", 1)[0], int(match.group(1)) if match else -1, name)


def _word_text(archive: zipfile.ZipFile, limits: ExtractionLimits) -> str:
    names = [
        name
        for name in archive.namelist()
        if name == "word/document.xml"
        or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
        or name in {"word/footnotes.xml", "word/endnotes.xml"}
    ]
    if "word/document.xml" not in names:
        raise UnsafeDocumentError("docx_document_xml_missing")
    output: list[str] = []
    for name in sorted(names, key=_natural_xml_key):
        root = _xml_root(archive.read(name), limits)
        for paragraph in (node for node in root.iter() if _local_name(node.tag) == "p"):
            pieces: list[str] = []
            for node in paragraph.iter():
                local = _local_name(node.tag)
                if local == "t" and node.text:
                    pieces.append(node.text)
                elif local == "tab":
                    pieces.append("\t")
                elif local in {"br", "cr"}:
                    pieces.append("\n")
            line = "".join(pieces).strip()
            if line:
                output.append(line)
    return "\n".join(output)


def _presentation_text(archive: zipfile.ZipFile, limits: ExtractionLimits) -> str:
    names = sorted(
        (
            name
            for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ),
        key=_natural_xml_key,
    )
    if not names:
        raise UnsafeDocumentError("pptx_slide_xml_missing")
    output: list[str] = []
    for number, name in enumerate(names, start=1):
        root = _xml_root(archive.read(name), limits)
        text = " ".join(
            node.text.strip()
            for node in root.iter()
            if _local_name(node.tag) == "t" and node.text and node.text.strip()
        )
        if text:
            output.append(f"[slide {number}] {text}")
    return "\n".join(output)


def _shared_strings(archive: zipfile.ZipFile, limits: ExtractionLimits) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml_root(archive.read("xl/sharedStrings.xml"), limits)
    values: list[str] = []
    for item in (node for node in root.iter() if _local_name(node.tag) == "si"):
        values.append(
            "".join(
                node.text or ""
                for node in item.iter()
                if _local_name(node.tag) == "t"
            )
        )
    return values


def _spreadsheet_text(
    archive: zipfile.ZipFile,
    limits: ExtractionLimits,
) -> tuple[str, bool]:
    sheet_names = sorted(
        (
            name
            for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        ),
        key=_natural_xml_key,
    )
    if not sheet_names:
        raise UnsafeDocumentError("xlsx_sheet_xml_missing")
    shared = _shared_strings(archive, limits)
    output: list[str] = []
    cells = 0
    formulas_ignored = False
    for sheet_number, name in enumerate(sheet_names, start=1):
        root = _xml_root(archive.read(name), limits)
        output.append(f"[sheet {sheet_number}]")
        for row in (node for node in root.iter() if _local_name(node.tag) == "row"):
            values: list[str] = []
            for cell in (node for node in row if _local_name(node.tag) == "c"):
                cells += 1
                if cells > limits.max_cells:
                    raise UnsafeDocumentError("xlsx_cell_limit")
                cell_type = cell.attrib.get("t", "")
                formula = next(
                    (
                        node.text
                        for node in cell
                        if _local_name(node.tag) == "f" and node.text
                    ),
                    None,
                )
                formulas_ignored = formulas_ignored or formula is not None
                inline = "".join(
                    node.text or ""
                    for node in cell.iter()
                    if _local_name(node.tag) == "t"
                )
                raw = next(
                    (
                        node.text
                        for node in cell
                        if _local_name(node.tag) == "v" and node.text is not None
                    ),
                    "",
                )
                if cell_type == "s" and raw:
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError) as exc:
                        raise UnsafeDocumentError("xlsx_shared_string_reference") from exc
                else:
                    value = inline or raw
                values.append(value)
            if any(value.strip() for value in values):
                output.append("\t".join(values).rstrip())
    return "\n".join(output), formulas_ignored


def _hwpx_text(archive: zipfile.ZipFile, limits: ExtractionLimits) -> str:
    names = sorted(
        (
            name
            for name in archive.namelist()
            if re.fullmatch(r"Contents/section\d+\.xml", name, flags=re.IGNORECASE)
        ),
        key=_natural_xml_key,
    )
    if not names:
        raise UnsafeDocumentError("hwpx_section_xml_missing")
    output: list[str] = []
    for name in names:
        root = _xml_root(archive.read(name), limits)
        pieces = [
            node.text.strip()
            for node in root.iter()
            if _local_name(node.tag) in {"t", "text"}
            and node.text
            and node.text.strip()
        ]
        if pieces:
            output.append(" ".join(pieces))
    return "\n".join(output)


def extract_document(
    data: bytes,
    extension: str,
    *,
    limits: ExtractionLimits | None = None,
) -> ExtractedText:
    """Extract inert text without executing macros, links, or embedded objects."""

    limits = limits or ExtractionLimits()
    if not data or len(data) > limits.max_input_bytes:
        raise UnsafeDocumentError("input_size_limit")
    suffix = extension.casefold()
    warnings: list[str] = []
    if suffix in {".txt", ".md", ".csv"}:
        text = _decode_text(data)
        if suffix == ".csv":
            try:
                rows = list(csv.reader(io.StringIO(text)))
            except csv.Error as exc:
                raise UnsafeDocumentError("invalid_csv") from exc
            text = "\n".join("\t".join(row) for row in rows)
    elif suffix in {".html", ".htm"}:
        parser = _InertHtmlTextExtractor()
        parser.feed(_decode_text(data))
        parser.close()
        text = "".join(parser.parts)
    elif suffix in {".docx", ".pptx", ".xlsx", ".hwpx"}:
        archive = _safe_zip(data, limits)
        try:
            if suffix == ".docx":
                text = _word_text(archive, limits)
            elif suffix == ".pptx":
                text = _presentation_text(archive, limits)
            elif suffix == ".xlsx":
                text, formulas_ignored = _spreadsheet_text(archive, limits)
                if formulas_ignored:
                    warnings.append("spreadsheet_formulas_ignored")
            else:
                text = _hwpx_text(archive, limits)
        finally:
            archive.close()
        warnings.append("embedded_objects_and_external_links_ignored")
    else:
        raise UnsafeDocumentError("unsupported_extension")
    if len(text) > limits.max_output_chars:
        raise UnsafeDocumentError("extracted_text_size_limit")
    if not text.strip():
        raise UnsafeDocumentError("no_extractable_text")
    return ExtractedText(text=text, warnings=tuple(warnings))
