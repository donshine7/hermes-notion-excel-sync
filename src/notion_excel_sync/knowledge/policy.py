from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from notion_excel_sync.adapters.onedrive import DriveEntry
from notion_excel_sync.config import WikiConfig
from notion_excel_sync.models import sha256_json


_CATEGORY_BY_ROOT = {
    "2. 해외총괄": "국내·해외 절차",
    "3. 사무소규정관련": "사무소 규정·업무 매뉴얼",
    "4. 사무소사업관련": "정부지원사업·증빙",
    "4-1. 기타 과제관련(4번 항목 제외 과제)": "정부지원사업·증빙",
    "4-2. 대리인 지원 자료 및 고객 관리": "정부지원사업·증빙",
    "4-3. 절세": "비용·관납료·청구 기준",
    "5. 사무소특허제도_특허,실시권,이전,감평": "특허·상표·디자인·저작권",
    "5-1. 소송 및 권리행사": "소송·심판·권리행사",
    "6. 사무소상표제도": "특허·상표·디자인·저작권",
    "7. 사무소디자인제도": "특허·상표·디자인·저작권",
    "7-1. 저작권": "특허·상표·디자인·저작권",
    "8. 기술분류": "기술분류·검색 전략",
    "9. 법령": "법령·제도",
    "16. 당소 사무소 정보": "서식·템플릿",
}

_UNSAFE_PATH_TOKENS = (
    "공인인증",
    "인증서",
    "certificate",
    "private key",
    "private_key",
    "비밀번호",
    "password",
    "passwd",
    "secret",
    "credential",
    "주민등록",
    "인력이력",
    "참여인력",
    "인력현황",
    "직원명부",
    "임직원명부",
    "이력서",
    "재직증명",
    "4대보험",
    "급여대장",
    "인사자료",
    "회의록",
    "단톡방",
)

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_RRN_RE = re.compile(r"\b\d{6}\s*[- ]?\s*[1-4]\d{6}\b")
_BIRTH_DATE_RE = re.compile(
    r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?82[- ]?)?0\d{1,2}[- )]?\d{3,4}[- ]?\d{4}(?!\d)")
_ACCOUNT_RE = re.compile(
    r"(?i)(계좌(?:번호)?|account)\s*[:：]?\s*[0-9][0-9 -]{8,20}[0-9]"
)
_BUSINESS_ID_RE = re.compile(r"(?<!\d)\d{3}[- ]?\d{2}[- ]?\d{5}(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[- ]?){3}\d{4}(?!\d)")
_PASSPORT_RE = re.compile(
    r"(?i)(여권(?:번호)?|passport)\s*[:：]?\s*[A-Z0-9][A-Z0-9 -]{5,15}"
)
_LABELED_NAME_RE = re.compile(
    r"(?i)(담당자|발명자|출원인|실무자|소개자|성명|이름)\s*[:：]?\s*"
    r"(?:[가-힣]{2,4}|[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    include_content: bool
    reason: str
    category: str = "격리·미분류 자료"
    authority: str = "reference"
    status: str = "unverified"
    security_classification: str = "internal_reference"


class WikiInclusionPolicy:
    """Fail-closed metadata policy for the `[상상] 업무분류` Wiki."""

    def __init__(self, config: WikiConfig) -> None:
        self.config = config
        self._include = self._root_map(config.include_roots, label="include")
        self._exclude = self._root_map(config.exclude_roots, label="exclude")
        if set(self._include).intersection(self._exclude):
            raise ValueError("Wiki include/exclude roots are duplicate or ambiguous")
        self._extensions = set(config.allowed_extensions)
        self._selective = self._root_map(config.selective_roots, label="selective")
        self._keywords = tuple(item.casefold() for item in config.selective_keywords)

    @staticmethod
    def _root_map(names: tuple[str, ...], *, label: str) -> dict[str, str]:
        roots: dict[str, str] = {}
        for raw_name in names:
            name = str(raw_name)
            normalized = unicodedata.normalize("NFC", name)
            if (
                not normalized
                or "/" in normalized
                or "\\" in normalized
                or normalized in {".", ".."}
            ):
                raise ValueError(f"Wiki {label} root name is not canonical")
            key = normalized.casefold()
            if key in roots:
                raise ValueError(f"Wiki {label} roots are duplicate or ambiguous")
            # Preserve exact configured spelling for category/display mappings.
            roots[key] = name
        return roots

    @staticmethod
    def top_root(relative_path: str) -> str:
        normalized = unicodedata.normalize(
            "NFC", relative_path.replace("\\", "/")
        ).strip("/")
        return normalized.split("/", 1)[0] if normalized else ""

    def decide(self, entry: DriveEntry) -> PolicyDecision:
        if entry.is_folder:
            return PolicyDecision(False, "folder")
        path = unicodedata.normalize("NFC", entry.relative_path).strip("/")
        root = self.top_root(path)
        root_key = root.casefold()
        folded = path.casefold()
        name = entry.name.casefold()
        suffix = PurePosixPath(name).suffix.casefold()
        if not root or root_key in self._exclude:
            return PolicyDecision(False, "excluded_root")
        if root_key not in self._include:
            return PolicyDecision(False, "root_not_allowlisted")
        if entry.size is None or entry.size <= 0:
            return PolicyDecision(False, "empty_or_unknown_size")
        if entry.size > self.config.max_file_bytes:
            return PolicyDecision(False, "file_too_large")
        if suffix not in self._extensions:
            return PolicyDecision(False, "unsupported_extension")
        if name.startswith("~$") or name.endswith((".tmp", ".bak", ".old")):
            return PolicyDecision(False, "temporary_or_backup")
        if any(token in folded for token in _UNSAFE_PATH_TOKENS):
            return PolicyDecision(False, "restricted_path_signal")
        if root_key in self._selective and not any(
            token in folded for token in self._keywords
        ):
            return PolicyDecision(False, "selective_keyword_required")
        canonical_root = self._include[root_key]
        category = _CATEGORY_BY_ROOT.get(canonical_root, "격리·미분류 자료")
        if canonical_root == "9. 법령":
            authority = "official_candidate"
        elif canonical_root == "3. 사무소규정관련":
            authority = "office_policy_candidate"
        elif canonical_root.startswith("4"):
            authority = "program_rule_candidate"
        else:
            authority = "reference"
        classification = (
            "internal_restricted"
            if canonical_root == "16. 당소 사무소 정보"
            else "internal_reference"
        )
        return PolicyDecision(
            True,
            "eligible",
            category=category,
            authority=authority,
            status="unverified",
            security_classification=classification,
        )


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_RE.sub(" ", value)
    value = "\n".join(" ".join(line.split()) for line in value.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def redact_sensitive_text(value: str) -> tuple[str, tuple[str, ...]]:
    """Remove contact/identity tokens that are unnecessary for rule retrieval."""

    normalized = normalized_text(value)
    findings: list[str] = []
    substitutions = (
        (_RRN_RE, "[REDACTED_ID]", "resident_id"),
        (_BIRTH_DATE_RE, "[REDACTED_BIRTH_DATE]", "birth_date"),
        (_ACCOUNT_RE, "[REDACTED_ACCOUNT]", "account"),
        (_BUSINESS_ID_RE, "[REDACTED_BUSINESS_ID]", "business_id"),
        (_CARD_RE, "[REDACTED_CARD]", "card"),
        (_PASSPORT_RE, "[REDACTED_PASSPORT]", "passport"),
        (_LABELED_NAME_RE, "[REDACTED_NAME]", "labeled_name"),
        (_EMAIL_RE, "[REDACTED_EMAIL]", "email"),
        (_PHONE_RE, "[REDACTED_PHONE]", "phone"),
    )
    for pattern, replacement, label in substitutions:
        normalized, count = pattern.subn(replacement, normalized)
        if count:
            findings.append(f"{label}:{count}")
    return normalized, tuple(findings)


def hashed_locator(relative_path: str) -> str:
    return hashlib.sha256(
        unicodedata.normalize("NFC", relative_path).encode("utf-8")
    ).hexdigest()


def policy_fingerprint(config: WikiConfig) -> str:
    digest = sha256_json(
        {
            "policy_version": config.policy_version,
            "include_roots": config.include_roots,
            "exclude_roots": config.exclude_roots,
            "allowed_extensions": config.allowed_extensions,
            "selective_roots": config.selective_roots,
            "selective_keywords": config.selective_keywords,
            "rollout_mode": config.rollout_mode,
            "max_file_bytes": config.max_file_bytes,
            "max_total_bytes": config.max_total_bytes,
            "max_documents": config.max_documents,
            "max_output_chars": config.max_output_chars,
            "max_generation_chars": config.max_generation_chars,
            "max_generation_chunks": config.max_generation_chunks,
            "extractor_timeout_seconds": config.extractor_timeout_seconds,
            "chunk_chars": config.chunk_chars,
            "chunk_overlap_chars": config.chunk_overlap_chars,
        }
    )
    return f"{config.policy_version}:{digest[:16]}"


TOPIC_CATEGORIES: dict[str, tuple[str, ...]] = {
    "actual-cost": ("비용·관납료·청구 기준", "사무소 규정·업무 매뉴얼"),
    "billing-policy": ("비용·관납료·청구 기준", "사무소 규정·업무 매뉴얼"),
    "government-support-evidence": ("정부지원사업·증빙",),
    "eligible-cost": ("정부지원사업·증빙", "비용·관납료·청구 기준"),
    "proof-documents": ("정부지원사업·증빙", "서식·템플릿"),
    "legal-deadline": ("법령·제도", "국내·해외 절차"),
    "procedure-rule": ("법령·제도", "국내·해외 절차"),
    "registration-followup": ("국내·해외 절차", "특허·상표·디자인·저작권"),
    "office-sop": ("사무소 규정·업무 매뉴얼",),
    "document-checklist": ("사무소 규정·업무 매뉴얼", "서식·템플릿"),
    "template": ("서식·템플릿",),
    "grouping-policy": ("정부지원사업·증빙", "사무소 규정·업무 매뉴얼"),
    "case-numbering": ("사무소 규정·업무 매뉴얼",),
    "data-quality": ("사무소 규정·업무 매뉴얼",),
}


def categories_for_topics(topics: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(topics) - TOPIC_CATEGORIES.keys())
    if unknown:
        raise ValueError("Unknown Wiki topic: " + ", ".join(unknown))
    values = {
        category
        for topic in topics
        for category in TOPIC_CATEGORIES.get(topic, ())
    }
    return tuple(sorted(values))
