from __future__ import annotations

import json
import os
import uuid
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from notion_excel_sync.security.windows_credentials import (
    WindowsCredentialError,
    credential_target,
    read_windows_generic_credential,
)


class ConfigError(ValueError):
    """Raised when runtime configuration is missing or unsafe."""


_WINDOWS_REPARSE_POINT = 0x400
_WIKI_RUNTIME_PARTS = ("notion-excel-sync", "wiki")
_AI_RUNTIME_PARTS = ("notion-excel-sync", "ai")


def _normalized_absolute_path(value: str | Path) -> Path:
    expanded = os.path.expandvars(os.fspath(value))
    return Path(os.path.abspath(os.path.expanduser(expanded)))


def _path_is_within(path: Path, root: Path, *, allow_root: bool = True) -> bool:
    try:
        common = os.path.commonpath((os.fspath(path), os.fspath(root)))
    except ValueError:
        return False
    same = os.path.normcase(common) == os.path.normcase(os.fspath(root))
    if not same:
        return False
    return allow_root or os.path.normcase(os.fspath(path)) != os.path.normcase(
        os.fspath(root)
    )


def _has_reparse_attribute(path: Path) -> bool:
    try:
        stat_result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & _WINDOWS_REPARSE_POINT)


def _assert_no_existing_reparse_component(path: Path) -> None:
    current = path
    existing: list[Path] = []
    while True:
        try:
            if current.exists() or current.is_symlink():
                existing.append(current)
        except OSError as exc:
            raise ConfigError("Wiki runtime path cannot be inspected safely") from exc
        if current.parent == current:
            break
        current = current.parent
    if any(_has_reparse_attribute(component) for component in existing):
        raise ConfigError("Wiki runtime path must not contain a reparse point")


def _local_wiki_runtime_root(*, project_root: Path) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise ConfigError("LOCALAPPDATA is required for the Wiki runtime")
    local_root = _normalized_absolute_path(local_app_data)
    if not local_root.is_dir():
        raise ConfigError("LOCALAPPDATA must be an existing local directory")
    runtime_root = local_root.joinpath(*_WIKI_RUNTIME_PARTS)
    project_root = _normalized_absolute_path(project_root)
    if _path_is_within(runtime_root, project_root) or _path_is_within(
        project_root, runtime_root
    ):
        raise ConfigError("Wiki runtime must be outside the project directory")

    cloud_roots: list[Path] = []
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        raw = os.environ.get(name, "").strip()
        if raw:
            cloud_roots.append(_normalized_absolute_path(raw))
    if any(_path_is_within(runtime_root, root) for root in cloud_roots):
        raise ConfigError("Wiki runtime must not be inside a cloud-synced directory")
    if any(
        part.casefold() == "onedrive"
        or part.casefold().startswith("onedrive - ")
        for part in runtime_root.parts
    ):
        raise ConfigError("Wiki runtime must not be inside a OneDrive directory")
    return runtime_root


def _local_ai_runtime_root(*, project_root: Path) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise ConfigError("LOCALAPPDATA is required for the AI runtime")
    local_root = _normalized_absolute_path(local_app_data)
    if not local_root.is_dir():
        raise ConfigError("LOCALAPPDATA must be an existing local directory")
    runtime_root = local_root.joinpath(*_AI_RUNTIME_PARTS)
    project_root = _normalized_absolute_path(project_root)
    if _path_is_within(runtime_root, project_root) or _path_is_within(
        project_root, runtime_root
    ):
        raise ConfigError("AI runtime must be outside the project directory")
    cloud_roots = [
        _normalized_absolute_path(raw)
        for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
        if (raw := os.environ.get(name, "").strip())
    ]
    if any(_path_is_within(runtime_root, root) for root in cloud_roots):
        raise ConfigError("AI runtime must not be inside a cloud-synced directory")
    return runtime_root


@dataclass(slots=True)
class OneDriveConfig:
    drive_id: str
    item_id: str
    client_id: str | None = None
    token_cache: Path = Path("data/msal-token-cache.bin")
    authority: str = "https://login.microsoftonline.com/consumers"
    username: str | None = None
    token_env: str = "ONEDRIVE_ACCESS_TOKEN"


@dataclass(slots=True)
class SourceConfig:
    """Authoritative workbook and data-folder source."""

    mode: str = "onedrive"
    root_dir: Path | None = None
    excel_path: Path | None = None
    immutable: bool = True
    initial_auto_hydrate: bool = False
    steady_state_auto_hydrate: bool = False
    snapshot_dir: Path = Path("data/local-source-snapshots")


@dataclass(slots=True)
class ExcelConfig:
    sheets: list[str]
    header_row: int = 1
    key_columns: list[str] = field(
        default_factory=lambda: ["당소 사건번호", "사건번호", "업무번호", "ID"]
    )
    key_groups: dict[str, list[list[str]]] = field(default_factory=dict)


@dataclass(slots=True)
class NotionConfig:
    read_token_env: str = "NOTION_READ_TOKEN"
    write_token_env: str = "GATEWAY_RELAY_NX_NOTION_TOKEN"
    api_version: str = "2026-03-11"
    schema_parent_page_id: str | None = None
    databases: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalConfig:
    secret_env: str = "GATEWAY_RELAY_NX_APPROVAL_SECRET"
    ttl_seconds: int = 1800
    allowed_telegram_users: set[str] = field(default_factory=set)


@dataclass(slots=True)
class EmailConfig:
    """Read-only Hiworks ingestion settings."""

    enabled: bool = False
    reference_project: Path | None = None
    host: str = "pop3s.hiworks.com"
    port: int = 995
    username: str | None = None
    password_secret: str = "HIWORKS_MAIL_PASSWORD"
    use_ssl: bool = True
    header_first: bool = True
    max_message_bytes: int = 10 * 1024 * 1024
    max_body_bytes: int = 2 * 1024 * 1024
    max_attachment_bytes: int = 20 * 1024 * 1024


@dataclass(slots=True)
class AIConfig:
    """Structured, advisory AI enrichment with a local output-only cache."""

    enabled: bool = False
    provider: str = "hermes"
    task_name: str = "web_extract"
    model: str | None = None
    rollout_mode: str = "shadow"
    privacy_mode: str = "local_only"
    cache_db: Path = Path("%LOCALAPPDATA%/notion-excel-sync/ai/analysis.db")
    timeout_seconds: int = 45
    max_calls_per_sync: int = 8
    max_mail_calls_per_sync: int = 4
    max_input_chars: int = 12_000
    assist_confidence: float = 0.80
    verified_confidence: float = 0.95


@dataclass(slots=True)
class WikiConfig:
    """Read-only OneDrive knowledge index configuration.

    The Wiki is deliberately independent from the Excel source item.  It may
    explain rules and methods, but it is never a source of case facts.
    """

    enabled: bool = False
    drive_id: str | None = None
    root_item_id: str | None = None
    source_root: Path | None = None
    output_root: Path | None = None
    root_path: str = "Desktop/[상상] 업무분류"
    index_db: Path = Path("%LOCALAPPDATA%/notion-excel-sync/wiki/index.db")
    rollout_mode: str = "shadow"
    initial_auto_hydrate: bool = False
    steady_state_auto_hydrate: bool = False
    include_roots: tuple[str, ...] = (
        "2. 해외총괄",
        "3. 사무소규정관련",
        "4. 사무소사업관련",
        "4-1. 기타 과제관련(4번 항목 제외 과제)",
        "4-2. 대리인 지원 자료 및 고객 관리",
        "4-3. 절세",
        "5. 사무소특허제도_특허,실시권,이전,감평",
        "5-1. 소송 및 권리행사",
        "6. 사무소상표제도",
        "7. 사무소디자인제도",
        "7-1. 저작권",
        "8. 기술분류",
        "9. 법령",
        "16. 당소 사무소 정보",
    )
    exclude_roots: tuple[str, ...] = (
        "0-1. 영업 관련 정보",
        "1. 업무분류_국내",
        "1. 업무분류_국내2",
        "1-1. 업무분류_인커밍",
        "1-2. 업무분류_아웃고잉",
        "1-3. 업무분류(사업계획서_제안서)",
        "1-4. 업무분류_과제",
        "11. 팀원실적정산_월간업무보고",
        "12. 파트너_회의록_단톡방자료",
        "20. 상상인력이력정보",
    )
    allowed_extensions: tuple[str, ...] = (
        ".txt",
        ".md",
        ".csv",
        ".html",
        ".htm",
        ".docx",
        ".pptx",
        ".xlsx",
        ".hwpx",
    )
    max_file_bytes: int = 20 * 1024 * 1024
    max_total_bytes: int = 250 * 1024 * 1024
    max_documents: int = 300
    max_output_chars: int = 2_000_000
    max_generation_chars: int = 30_000_000
    max_generation_chunks: int = 50_000
    extractor_timeout_seconds: int = 30
    chunk_chars: int = 1_200
    chunk_overlap_chars: int = 180
    retrieval_top_k: int = 5
    policy_version: str = "sangsang-wiki-policy-v2"
    selective_roots: tuple[str, ...] = (
        "2. 해외총괄",
        "4. 사무소사업관련",
        "4-1. 기타 과제관련(4번 항목 제외 과제)",
        "4-2. 대리인 지원 자료 및 고객 관리",
        "16. 당소 사무소 정보",
    )
    selective_keywords: tuple[str, ...] = (
        "공고",
        "규정",
        "지침",
        "기준",
        "요령",
        "안내",
        "가이드",
        "매뉴얼",
        "절차",
        "서식",
        "양식",
        "템플릿",
        "faq",
        "사업비",
        "지원사업",
        "바우처",
        "증빙",
        "정산",
        "수행기관",
        "관납료",
        "비용",
    )


@dataclass(slots=True)
class AppConfig:
    onedrive: OneDriveConfig
    excel: ExcelConfig
    notion: NotionConfig
    approval: ApprovalConfig
    wiki: WikiConfig
    state_db: Path
    work_dir: Path
    initial_cutoff: datetime
    source: SourceConfig = field(default_factory=SourceConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    ai: AIConfig = field(default_factory=AIConfig)

    def secret(self, env_name: str, *, required: bool = True) -> str | None:
        gateway_only = env_name.strip().upper().startswith("GATEWAY_RELAY_")
        if not gateway_only:
            try:
                credential_value = read_windows_generic_credential(env_name)
            except WindowsCredentialError as exc:
                raise ConfigError(
                    f"Cannot read protected credential {credential_target(env_name)!r}"
                ) from exc
            if credential_value is not None:
                if not credential_value:
                    raise ConfigError(
                        f"Protected credential is empty: {credential_target(env_name)}"
                    )
                return credential_value

        # Gateway-only values deliberately remain process-environment-only. A
        # Generic Credential can be read by arbitrary processes of the same Windows
        # user, so it cannot replace Hermes' child-environment stripping boundary.
        value = os.environ.get(env_name)
        if required and not value:
            if gateway_only:
                raise ConfigError(
                    f"Required gateway process environment secret is not set: {env_name}"
                )
            raise ConfigError(
                "Required secret is not available in Windows Credential Manager "
                f"or environment fallback: {env_name}"
            )
        return value


def _required(mapping: dict[str, Any], key: str, section: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"Missing config value: {section}.{key}")
    return mapping[key]


def _require_gateway_only_secret(name: str, *, suffix: str, field_name: str) -> str:
    normalized = name.strip().upper()
    if not normalized.startswith("GATEWAY_RELAY_") or not normalized.endswith(suffix):
        raise ConfigError(
            f"{field_name} must use a GATEWAY_RELAY_*{suffix} environment name "
            "so Hermes strips it from model subprocesses"
        )
    return name.strip()


def _key_groups(
    raw: object,
    sheets: list[str],
) -> dict[str, list[list[str]]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("excel.key_groups must be an object keyed by sheet name")
    unknown_sheets = sorted(set(str(name) for name in raw) - set(sheets))
    if unknown_sheets:
        raise ConfigError(f"excel.key_groups contains unknown sheets: {unknown_sheets}")
    result: dict[str, list[list[str]]] = {}
    for sheet, groups in raw.items():
        if not isinstance(groups, list) or not groups:
            raise ConfigError(f"excel.key_groups.{sheet} must be a non-empty list")
        normalized: list[list[str]] = []
        for group in groups:
            if not isinstance(group, list) or not group:
                raise ConfigError(
                    f"excel.key_groups.{sheet} entries must be non-empty lists"
                )
            columns = [str(column).strip() for column in group]
            if any(not column for column in columns) or len(set(columns)) != len(columns):
                raise ConfigError(
                    f"excel.key_groups.{sheet} has blank or duplicate columns"
                )
            normalized.append(columns)
        if len({tuple(group) for group in normalized}) != len(normalized):
            raise ConfigError(f"excel.key_groups.{sheet} has duplicate groups")
        result[str(sheet)] = normalized
    return result


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON config: {exc}") from exc

    onedrive_raw = raw.get("onedrive", {})
    source_raw = raw.get("source", {})
    excel_raw = raw.get("excel", {})
    notion_raw = raw.get("notion", {})
    approval_raw = raw.get("approval", {})
    wiki_raw = raw.get("wiki", {})
    email_raw = raw.get("email", {})
    ai_raw = raw.get("ai", {})
    if not isinstance(source_raw, dict):
        raise ConfigError("source must be an object")
    if not isinstance(wiki_raw, dict):
        raise ConfigError("wiki must be an object")
    if not isinstance(email_raw, dict):
        raise ConfigError("email must be an object")
    if not isinstance(ai_raw, dict):
        raise ConfigError("ai must be an object")

    source_mode = str(source_raw.get("mode", "onedrive")).strip().casefold()
    if source_mode not in {"onedrive", "local_filesystem"}:
        raise ConfigError("source.mode must be onedrive or local_filesystem")

    initial_cutoff_raw = raw.get("initial_cutoff")
    initial_cutoff = (
        datetime.now().astimezone()
        if initial_cutoff_raw is None
        else datetime.fromisoformat(str(initial_cutoff_raw))
    )
    if initial_cutoff.tzinfo is None:
        raise ConfigError("initial_cutoff must include an explicit timezone")

    base_dir = config_path.parent
    state_db = Path(raw.get("state_db", "../data/state.db"))
    work_dir = Path(raw.get("work_dir", "../data/snapshots"))
    if not state_db.is_absolute():
        state_db = (base_dir / state_db).resolve()
    if not work_dir.is_absolute():
        work_dir = (base_dir / work_dir).resolve()

    source_root: Path | None = None
    source_excel_path: Path | None = None
    source_snapshot_dir = Path(
        source_raw.get("snapshot_dir", work_dir / "local-source-snapshots")
    )
    if not source_snapshot_dir.is_absolute():
        source_snapshot_dir = _normalized_absolute_path(base_dir / source_snapshot_dir)
    else:
        source_snapshot_dir = _normalized_absolute_path(source_snapshot_dir)
    source_immutable = bool(source_raw.get("immutable", True))
    initial_auto_hydrate = bool(source_raw.get("initial_auto_hydrate", False))
    steady_state_auto_hydrate = bool(
        source_raw.get("steady_state_auto_hydrate", False)
    )
    if source_mode == "local_filesystem":
        source_root_value = str(
            _required(source_raw, "root_dir", "source")
        ).strip()
        source_excel_value = str(
            _required(source_raw, "excel_path", "source")
        ).strip()
        source_root = _normalized_absolute_path(source_root_value)
        source_excel_path = _normalized_absolute_path(source_excel_value)
        if not source_root.is_dir():
            raise ConfigError("source.root_dir must be an existing directory")
        if not source_excel_path.is_file():
            raise ConfigError("source.excel_path must be an existing file")
        if not _path_is_within(source_excel_path, source_root, allow_root=False):
            raise ConfigError("source.excel_path must be inside source.root_dir")
        if not source_immutable:
            raise ConfigError("local source must set source.immutable to true")
        if _path_is_within(source_snapshot_dir, source_root):
            raise ConfigError("source.snapshot_dir must be outside source.root_dir")
        if steady_state_auto_hydrate:
            raise ConfigError(
                "source.steady_state_auto_hydrate must be false; changed online-only "
                "files require a fresh Telegram approval"
            )

    token_cache = Path(onedrive_raw.get("token_cache", "../data/msal-token-cache.bin"))
    if not token_cache.is_absolute():
        token_cache = (base_dir / token_cache).resolve()
    client_id_value = str(onedrive_raw.get("client_id", "")).strip() or None
    if client_id_value:
        try:
            uuid.UUID(client_id_value)
        except ValueError as exc:
            raise ConfigError("onedrive.client_id must be a valid UUID") from exc
    authority = str(
        onedrive_raw.get(
            "authority",
            "https://login.microsoftonline.com/consumers",
        )
    ).rstrip("/")
    if authority != "https://login.microsoftonline.com/consumers":
        raise ConfigError(
            "onedrive.authority must be https://login.microsoftonline.com/consumers "
            "for the configured personal Microsoft account"
        )

    users = {str(item) for item in approval_raw.get("allowed_telegram_users", [])}
    if not users:
        raise ConfigError("approval.allowed_telegram_users must contain at least one user")

    sheets = [str(item) for item in _required(excel_raw, "sheets", "excel")]
    if not sheets:
        raise ConfigError("excel.sheets must not be empty")

    read_token_env = str(
        notion_raw.get(
            "read_token_env",
            notion_raw.get("token_env", "NOTION_READ_TOKEN"),
        )
    ).strip()
    if not read_token_env:
        raise ConfigError("notion.read_token_env must not be empty")
    write_token_env = _require_gateway_only_secret(
        str(
            notion_raw.get(
                "write_token_env",
                "GATEWAY_RELAY_NX_NOTION_TOKEN",
            )
        ),
        suffix="_TOKEN",
        field_name="notion.write_token_env",
    )
    if read_token_env.upper() == write_token_env.upper():
        raise ConfigError("Notion read and write tokens must be separate credentials")
    approval_secret_env = _require_gateway_only_secret(
        str(
            approval_raw.get(
                "secret_env",
                "GATEWAY_RELAY_NX_APPROVAL_SECRET",
            )
        ),
        suffix="_SECRET",
        field_name="approval.secret_env",
    )

    wiki_enabled = bool(wiki_raw.get("enabled", False))
    wiki_runtime_root = _local_wiki_runtime_root(project_root=base_dir.parent)
    wiki_index_value = str(
        wiki_raw.get("index_db", str(wiki_runtime_root / "index.db"))
    ).strip()
    if not wiki_index_value:
        raise ConfigError("wiki.index_db must not be empty")
    wiki_index_expanded = os.path.expandvars(os.path.expanduser(wiki_index_value))
    if not Path(wiki_index_expanded).is_absolute():
        raise ConfigError(
            "wiki.index_db must be an absolute path inside the local Wiki runtime"
        )
    wiki_index = _normalized_absolute_path(wiki_index_expanded)
    if not _path_is_within(wiki_index, wiki_runtime_root, allow_root=False):
        raise ConfigError(
            "wiki.index_db must be inside %LOCALAPPDATA%\\notion-excel-sync\\wiki"
        )
    if wiki_enabled:
        _assert_no_existing_reparse_component(wiki_index)
        try:
            if wiki_index.exists() and wiki_index.is_dir():
                raise ConfigError("wiki.index_db must be a file path")
        except OSError as exc:
            raise ConfigError("Wiki index path cannot be inspected safely") from exc
    rollout_mode = str(wiki_raw.get("rollout_mode", "shadow")).strip().casefold()
    if rollout_mode not in {"inventory", "shadow", "verified"}:
        raise ConfigError("wiki.rollout_mode must be inventory, shadow, or verified")
    root_path = str(
        wiki_raw.get("root_path", "Desktop/[상상] 업무분류")
    ).strip().strip("/")
    root_item_id = str(wiki_raw.get("root_item_id", "")).strip() or None
    if bool(wiki_raw.get("enabled", False)) and not (root_item_id or root_path):
        raise ConfigError("enabled Wiki requires wiki.root_item_id or wiki.root_path")

    def wiki_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = wiki_raw.get(name, default)
        if not isinstance(value, list | tuple):
            raise ConfigError(f"wiki.{name} must be a list")
        normalized = tuple(str(item).strip() for item in value if str(item).strip())
        if len(set(normalized)) != len(normalized):
            raise ConfigError(f"wiki.{name} contains duplicate values")
        return normalized

    wiki_defaults = WikiConfig()
    allowed_extensions = tuple(
        extension.casefold() if extension.startswith(".") else f".{extension.casefold()}"
        for extension in wiki_tuple("allowed_extensions", wiki_defaults.allowed_extensions)
    )
    max_file_bytes = int(wiki_raw.get("max_file_bytes", wiki_defaults.max_file_bytes))
    max_total_bytes = int(wiki_raw.get("max_total_bytes", wiki_defaults.max_total_bytes))
    max_documents = int(wiki_raw.get("max_documents", wiki_defaults.max_documents))
    max_output_chars = int(
        wiki_raw.get("max_output_chars", wiki_defaults.max_output_chars)
    )
    max_generation_chars = int(
        wiki_raw.get("max_generation_chars", wiki_defaults.max_generation_chars)
    )
    max_generation_chunks = int(
        wiki_raw.get("max_generation_chunks", wiki_defaults.max_generation_chunks)
    )
    extractor_timeout_seconds = int(
        wiki_raw.get(
            "extractor_timeout_seconds", wiki_defaults.extractor_timeout_seconds
        )
    )
    chunk_chars = int(wiki_raw.get("chunk_chars", wiki_defaults.chunk_chars))
    chunk_overlap_chars = int(
        wiki_raw.get("chunk_overlap_chars", wiki_defaults.chunk_overlap_chars)
    )
    retrieval_top_k = int(
        wiki_raw.get("retrieval_top_k", wiki_defaults.retrieval_top_k)
    )
    if min(
        max_file_bytes,
        max_total_bytes,
        max_documents,
        max_output_chars,
        max_generation_chars,
        max_generation_chunks,
        extractor_timeout_seconds,
        chunk_chars,
        retrieval_top_k,
    ) <= 0:
        raise ConfigError("Wiki numeric limits must be positive")
    if chunk_overlap_chars < 0 or chunk_overlap_chars >= chunk_chars:
        raise ConfigError("wiki.chunk_overlap_chars must be >= 0 and < chunk_chars")

    wiki_source_root: Path | None = source_root
    wiki_source_value = str(wiki_raw.get("source_root", "")).strip()
    if wiki_source_value:
        wiki_source_root = _normalized_absolute_path(wiki_source_value)
    if wiki_source_root is not None and not wiki_source_root.is_dir():
        raise ConfigError("wiki.source_root must be an existing directory")

    wiki_output_root: Path | None = None
    wiki_output_value = str(wiki_raw.get("output_root", "")).strip()
    if wiki_output_value:
        wiki_output_root = _normalized_absolute_path(wiki_output_value)
        if wiki_source_root is not None and (
            _path_is_within(wiki_output_root, wiki_source_root)
            or _path_is_within(wiki_source_root, wiki_output_root)
        ):
            raise ConfigError("wiki.output_root must not overlap wiki.source_root")
    if source_mode == "local_filesystem" and wiki_enabled:
        if wiki_source_root is None or wiki_output_root is None:
            raise ConfigError(
                "enabled local Wiki requires wiki.source_root and wiki.output_root"
            )
    wiki_initial_auto_hydrate = bool(
        wiki_raw.get("initial_auto_hydrate", initial_auto_hydrate)
    )
    wiki_steady_state_auto_hydrate = bool(
        wiki_raw.get("steady_state_auto_hydrate", steady_state_auto_hydrate)
    )
    if source_mode == "local_filesystem" and wiki_steady_state_auto_hydrate:
        raise ConfigError(
            "wiki.steady_state_auto_hydrate must be false; later online-only "
            "downloads require explicit Telegram approval"
        )

    email_enabled = bool(email_raw.get("enabled", False))
    email_reference_project: Path | None = None
    email_project_value = str(email_raw.get("reference_project", "")).strip()
    if email_project_value:
        email_reference_project = _normalized_absolute_path(email_project_value)
        if email_enabled and not email_reference_project.is_dir():
            raise ConfigError("email.reference_project must be an existing directory")
    email_host = str(email_raw.get("host", "pop3s.hiworks.com")).strip()
    email_port = int(email_raw.get("port", 995))
    email_password_secret = str(
        email_raw.get(
            "password_secret",
            email_raw.get("credential_target", "HIWORKS_MAIL_PASSWORD"),
        )
    ).strip()
    email_max_message_bytes = int(
        email_raw.get("max_message_bytes", 10 * 1024 * 1024)
    )
    email_max_body_bytes = int(email_raw.get("max_body_bytes", 2 * 1024 * 1024))
    email_max_attachment_bytes = int(
        email_raw.get("max_attachment_bytes", 20 * 1024 * 1024)
    )
    if (
        not email_host
        or not email_password_secret
        or email_port <= 0
        or min(
            email_max_message_bytes,
            email_max_body_bytes,
            email_max_attachment_bytes,
        )
        <= 0
    ):
        raise ConfigError("email connection names and size limits must be positive")
    try:
        credential_target(email_password_secret)
    except WindowsCredentialError as exc:
        raise ConfigError(
            "email.password_secret must be an environment-style credential name"
        ) from exc

    ai_defaults = AIConfig()
    ai_enabled = bool(ai_raw.get("enabled", False))
    ai_provider = str(ai_raw.get("provider", ai_defaults.provider)).strip().casefold()
    if ai_provider != "hermes":
        raise ConfigError("ai.provider currently supports hermes only")
    ai_task_name = str(ai_raw.get("task_name", ai_defaults.task_name)).strip()
    if not ai_task_name or not ai_task_name.replace("_", "").isalnum():
        raise ConfigError("ai.task_name must contain only letters, digits, and underscores")
    ai_rollout_mode = str(
        ai_raw.get("rollout_mode", ai_defaults.rollout_mode)
    ).strip().casefold()
    if ai_rollout_mode not in {"shadow", "assist", "verified"}:
        raise ConfigError("ai.rollout_mode must be shadow, assist, or verified")
    ai_privacy_mode = str(
        ai_raw.get("privacy_mode", ai_defaults.privacy_mode)
    ).strip().casefold()
    if ai_privacy_mode not in {"local_only", "redacted_remote"}:
        raise ConfigError(
            "ai.privacy_mode must be local_only or redacted_remote"
        )
    ai_runtime_root = _local_ai_runtime_root(project_root=base_dir.parent)
    ai_cache_value = str(
        ai_raw.get("cache_db", str(ai_runtime_root / "analysis.db"))
    ).strip()
    ai_cache_expanded = os.path.expandvars(os.path.expanduser(ai_cache_value))
    if not Path(ai_cache_expanded).is_absolute():
        raise ConfigError("ai.cache_db must be an absolute local runtime path")
    ai_cache_db = _normalized_absolute_path(ai_cache_expanded)
    if not _path_is_within(ai_cache_db, ai_runtime_root, allow_root=False):
        raise ConfigError(
            "ai.cache_db must be inside %LOCALAPPDATA%\\notion-excel-sync\\ai"
        )
    if ai_enabled:
        _assert_no_existing_reparse_component(ai_cache_db)
    ai_timeout_seconds = int(
        ai_raw.get("timeout_seconds", ai_defaults.timeout_seconds)
    )
    ai_max_calls = int(
        ai_raw.get("max_calls_per_sync", ai_defaults.max_calls_per_sync)
    )
    ai_max_mail_calls = int(
        ai_raw.get(
            "max_mail_calls_per_sync",
            ai_defaults.max_mail_calls_per_sync,
        )
    )
    ai_max_input_chars = int(
        ai_raw.get("max_input_chars", ai_defaults.max_input_chars)
    )
    ai_assist_confidence = float(
        ai_raw.get("assist_confidence", ai_defaults.assist_confidence)
    )
    ai_verified_confidence = float(
        ai_raw.get("verified_confidence", ai_defaults.verified_confidence)
    )
    if min(
        ai_timeout_seconds,
        ai_max_calls,
        ai_max_mail_calls,
        ai_max_input_chars,
    ) <= 0:
        raise ConfigError("AI numeric limits must be positive")
    if ai_max_mail_calls > ai_max_calls:
        raise ConfigError(
            "ai.max_mail_calls_per_sync must not exceed max_calls_per_sync"
        )
    if not 0 <= ai_assist_confidence <= ai_verified_confidence <= 1:
        raise ConfigError("AI confidence thresholds are invalid")

    if source_mode == "local_filesystem":
        assert source_excel_path is not None
        onedrive_drive_id = "local"
        onedrive_item_id = "local:" + hashlib.sha256(
            os.path.normcase(os.fspath(source_excel_path)).encode("utf-8")
        ).hexdigest()[:32]
    else:
        onedrive_drive_id = str(_required(onedrive_raw, "drive_id", "onedrive"))
        onedrive_item_id = str(_required(onedrive_raw, "item_id", "onedrive"))

    return AppConfig(
        onedrive=OneDriveConfig(
            drive_id=onedrive_drive_id,
            item_id=onedrive_item_id,
            client_id=client_id_value,
            token_cache=token_cache,
            authority=authority,
            username=str(onedrive_raw.get("username", "")).strip() or None,
            token_env=str(onedrive_raw.get("token_env", "ONEDRIVE_ACCESS_TOKEN")),
        ),
        excel=ExcelConfig(
            sheets=sheets,
            header_row=int(excel_raw.get("header_row", 1)),
            key_columns=[str(item) for item in excel_raw.get("key_columns", [])]
            or ExcelConfig(sheets=[]).key_columns,
            key_groups=_key_groups(excel_raw.get("key_groups"), sheets),
        ),
        notion=NotionConfig(
            read_token_env=read_token_env,
            write_token_env=write_token_env,
            api_version=str(notion_raw.get("api_version", "2026-03-11")),
            schema_parent_page_id=(
                str(notion_raw.get("schema_parent_page_id", "")).strip() or None
            ),
            databases={str(k): str(v) for k, v in notion_raw.get("databases", {}).items()},
        ),
        approval=ApprovalConfig(
            secret_env=approval_secret_env,
            ttl_seconds=int(approval_raw.get("ttl_seconds", 1800)),
            allowed_telegram_users=users,
        ),
        wiki=WikiConfig(
            enabled=wiki_enabled,
            drive_id=str(wiki_raw.get("drive_id", "")).strip() or None,
            root_item_id=root_item_id,
            source_root=wiki_source_root,
            output_root=wiki_output_root,
            root_path=root_path,
            index_db=wiki_index,
            rollout_mode=rollout_mode,
            initial_auto_hydrate=wiki_initial_auto_hydrate,
            steady_state_auto_hydrate=wiki_steady_state_auto_hydrate,
            include_roots=wiki_tuple("include_roots", wiki_defaults.include_roots),
            exclude_roots=wiki_tuple("exclude_roots", wiki_defaults.exclude_roots),
            allowed_extensions=allowed_extensions,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            max_documents=max_documents,
            max_output_chars=max_output_chars,
            max_generation_chars=max_generation_chars,
            max_generation_chunks=max_generation_chunks,
            extractor_timeout_seconds=extractor_timeout_seconds,
            chunk_chars=chunk_chars,
            chunk_overlap_chars=chunk_overlap_chars,
            retrieval_top_k=retrieval_top_k,
            policy_version=str(
                wiki_raw.get("policy_version", wiki_defaults.policy_version)
            ).strip()
            or wiki_defaults.policy_version,
            selective_roots=wiki_tuple(
                "selective_roots", wiki_defaults.selective_roots
            ),
            selective_keywords=wiki_tuple(
                "selective_keywords", wiki_defaults.selective_keywords
            ),
        ),
        state_db=state_db,
        work_dir=work_dir,
        initial_cutoff=initial_cutoff,
        source=SourceConfig(
            mode=source_mode,
            root_dir=source_root,
            excel_path=source_excel_path,
            immutable=source_immutable,
            initial_auto_hydrate=initial_auto_hydrate,
            steady_state_auto_hydrate=steady_state_auto_hydrate,
            snapshot_dir=source_snapshot_dir,
        ),
        email=EmailConfig(
            enabled=email_enabled,
            reference_project=email_reference_project,
            host=email_host,
            port=email_port,
            username=str(email_raw.get("username", "")).strip() or None,
            password_secret=email_password_secret,
            use_ssl=bool(email_raw.get("use_ssl", True)),
            header_first=bool(email_raw.get("header_first", True)),
            max_message_bytes=email_max_message_bytes,
            max_body_bytes=email_max_body_bytes,
            max_attachment_bytes=email_max_attachment_bytes,
        ),
        ai=AIConfig(
            enabled=ai_enabled,
            provider=ai_provider,
            task_name=ai_task_name,
            model=str(ai_raw.get("model", "")).strip() or None,
            rollout_mode=ai_rollout_mode,
            privacy_mode=ai_privacy_mode,
            cache_db=ai_cache_db,
            timeout_seconds=ai_timeout_seconds,
            max_calls_per_sync=ai_max_calls,
            max_mail_calls_per_sync=ai_max_mail_calls,
            max_input_chars=ai_max_input_chars,
            assist_confidence=ai_assist_confidence,
            verified_confidence=ai_verified_confidence,
        ),
    )
