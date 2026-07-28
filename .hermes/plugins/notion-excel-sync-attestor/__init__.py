from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any


_PROJECT_ROOT_ENV = "NOTION_EXCEL_SYNC_PROJECT_ROOT"
_DEVELOPMENT_ENV = "NOTION_EXCEL_SYNC_DEV_PLUGIN"
_PLUGIN_DIR = Path(__file__).resolve().parent
_VENDORED_ROOT = _PLUGIN_DIR / "runtime"
_MANIFEST_PATH = _PLUGIN_DIR / "runtime-manifest.json"
_configured_root = os.environ.get(_PROJECT_ROOT_ENV, "").strip()
_PROJECT_ROOT = (
    Path(_configured_root).expanduser().resolve()
    if _configured_root
    else Path(__file__).resolve().parents[3]
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VENDORED_DISTRIBUTIONS = {
    "openpyxl": "openpyxl",
    "et-xmlfile": "et_xmlfile",
    "msal": "msal",
    "msal-extensions": "msal_extensions",
}
_VENDORED_VERSIONS = {
    "openpyxl": "3.1.5",
    "et-xmlfile": "2.0.0",
    "msal": "1.37.0",
    "msal-extensions": "1.3.1",
}
_HOST_REQUIREMENTS = {
    "hermes-agent": "==0.18.2",
    "requests": "==2.33.0",
    "urllib3": "==2.7.0",
    "certifi": "==2026.5.20",
    "charset-normalizer": "==3.4.4",
    "idna": "==3.15",
    "PyJWT": "==2.13.0",
    "cryptography": "==46.0.7",
    "cffi": "==2.0.0",
    "pycparser": "==3.0",
    "Pillow": "==12.2.0",
    "defusedxml": "==0.7.1",
    "portalocker": "==3.2.0",
}
_HOST_DEPENDENCIES = {
    "requests": ("requests", "2.33.0"),
    "urllib3": ("urllib3", "2.7.0"),
    "certifi": ("certifi", "2026.5.20"),
    "charset-normalizer": ("charset_normalizer", "3.4.4"),
    "idna": ("idna", "3.15"),
    "PyJWT": ("jwt", "2.13.0"),
    "cryptography": ("cryptography", "46.0.7"),
    "cffi": ("cffi", "2.0.0"),
    "pycparser": ("pycparser", "3.0"),
    "Pillow": ("PIL", "12.2.0"),
    "defusedxml": ("defusedxml", "0.7.1"),
    "portalocker": ("portalocker", "3.2.0"),
}
_HOST_AUXILIARY_MODULES = ("_cffi_backend",)
_HERMES_AGENT_VERSION = "0.18.2"
_FORBIDDEN_OPTIONAL_MODULES = ("lxml", "numpy", "redis")


def _runtime_files(root: Path) -> dict[str, Path]:
    """Return regular runtime files and reject links or path escapes."""

    resolved_root = root.resolve(strict=True)
    files: dict[str, Path] = {}
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeError(f"Vendored runtime contains a link: {candidate}")
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(resolved_root).as_posix()
        except ValueError as exc:
            raise RuntimeError(f"Vendored runtime escapes its root: {candidate}") from exc
        files[relative] = resolved
    return files


def _verify_vendored_runtime(
    *,
    expected_manifest_digest: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Verify the complete vendored tree and return its bound manifest."""

    if not _VENDORED_ROOT.is_dir() or not _MANIFEST_PATH.is_file():
        raise RuntimeError("Installed Hermes plugin is missing its vendored runtime")
    manifest_bytes = _MANIFEST_PATH.read_bytes()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_digest is not None and not hmac.compare_digest(
        manifest_digest,
        expected_manifest_digest,
    ):
        raise RuntimeError("Vendored runtime manifest changed after plugin load")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Vendored runtime manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise RuntimeError("Unsupported vendored runtime manifest format")
    if manifest.get("algorithm") != "sha256":
        raise RuntimeError("Vendored runtime must use SHA-256")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError("Vendored runtime manifest has no files")
    if "notion_excel_sync/security/hermes_gateway.py" not in expected:
        raise RuntimeError("Vendored runtime is missing the trusted gateway module")
    distributions = manifest.get("distributions")
    if (
        not isinstance(distributions, dict)
        or distributions != _VENDORED_VERSIONS
        or any(
            not isinstance(version, str)
            or re.fullmatch(r"\d+(?:\.\d+)*(?:[A-Za-z0-9._+-]*)", version) is None
            for version in distributions.values()
        )
    ):
        raise RuntimeError("Vendored runtime distribution contract is invalid")
    if manifest.get("host_requirements") != _HOST_REQUIREMENTS:
        raise RuntimeError("Vendored runtime host dependency contract is invalid")

    actual = _runtime_files(_VENDORED_ROOT)
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise RuntimeError(
            "Vendored runtime file set differs from manifest "
            f"(missing={missing[:3]}, unexpected={unexpected[:3]})"
        )
    for relative, expected_digest in expected.items():
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(expected_digest, str)
            or _SHA256_RE.fullmatch(expected_digest) is None
        ):
            raise RuntimeError("Vendored runtime manifest contains an unsafe entry")
        digest = hashlib.sha256(actual[relative].read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, expected_digest):
            raise RuntimeError(f"Vendored runtime hash mismatch: {relative}")
    return manifest_digest, manifest


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _verified_hermes_venv_root() -> Path | None:
    hermes_home_text = os.environ.get("HERMES_HOME", "").strip()
    virtual_env_text = os.environ.get("VIRTUAL_ENV", "").strip()
    if not hermes_home_text and not virtual_env_text:
        return None
    if not hermes_home_text or not virtual_env_text:
        raise RuntimeError("Hermes host environment is incomplete")

    hermes_home = Path(hermes_home_text).expanduser().resolve(strict=True)
    agent_root = (hermes_home / "hermes-agent").resolve(strict=True)
    virtual_env = Path(virtual_env_text).expanduser().resolve(strict=True)
    expected_virtual_env = (agent_root / "venv").resolve(strict=True)
    if virtual_env != expected_virtual_env:
        raise RuntimeError("Hermes VIRTUAL_ENV does not belong to HERMES_HOME")
    if _path_is_within(virtual_env, _PROJECT_ROOT):
        raise RuntimeError("Hermes host dependencies cannot come from the project checkout")

    hermes_cli = sys.modules.get("hermes_cli.main") or sys.modules.get("hermes_cli")
    cli_origin = getattr(hermes_cli, "__file__", None)
    if not cli_origin or not _path_is_within(Path(cli_origin), agent_root / "hermes_cli"):
        raise RuntimeError("Hermes CLI has no trusted origin")

    site_packages = virtual_env / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise RuntimeError("Hermes VIRTUAL_ENV has no site-packages directory")
    metadata_matches = tuple(site_packages.glob("hermes_agent-*.dist-info/METADATA"))
    if len(metadata_matches) != 1:
        raise RuntimeError("Hermes agent distribution metadata is missing or ambiguous")
    metadata_text = metadata_matches[0].read_text(encoding="utf-8")
    name_match = re.search(r"(?m)^Name:\s*([^\r\n]+)\s*$", metadata_text)
    version_match = re.search(r"(?m)^Version:\s*([^\r\n]+)\s*$", metadata_text)
    if (
        name_match is None
        or _normalized_distribution_name(name_match.group(1).strip()) != "hermes-agent"
        or version_match is None
        or version_match.group(1).strip() != _HERMES_AGENT_VERSION
    ):
        raise RuntimeError("Hermes agent distribution identity is unsupported")
    return virtual_env


def _trusted_interpreter_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in (sys.prefix, sys.base_prefix):
        try:
            root = Path(value).resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if _path_is_within(root, _PROJECT_ROOT):
            continue
        if root not in roots:
            roots.append(root)
    hermes_venv = _verified_hermes_venv_root()
    if hermes_venv is None:
        raise RuntimeError("Installed plugin requires the verified Hermes host environment")
    if hermes_venv not in roots:
        roots.append(hermes_venv)
    if not roots:
        raise RuntimeError("Cannot determine the trusted Hermes interpreter roots")
    return tuple(roots)


def _assert_origin_within(
    origin: str | os.PathLike[str],
    roots: tuple[Path, ...],
    label: str,
) -> None:
    candidate = Path(origin)
    if not any(_path_is_within(candidate, root) for root in roots):
        raise RuntimeError(f"{label} was loaded outside the trusted runtime: {candidate}")


def _assert_vendored_module_origins(import_root: Path, *, require_all: bool) -> None:
    loaded_top_levels: set[str] = set()
    for name, module in tuple(sys.modules.items()):
        top_level = name.split(".", 1)[0]
        if top_level not in _VENDORED_DISTRIBUTIONS.values():
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            raise RuntimeError(f"Vendored module has no verifiable origin: {name}")
        if not _path_is_within(Path(origin), import_root):
            raise RuntimeError(f"Vendored module was preloaded outside the runtime: {name}")
        loaded_top_levels.add(top_level)
    if require_all:
        missing = sorted(set(_VENDORED_DISTRIBUTIONS.values()) - loaded_top_levels)
        if missing:
            raise RuntimeError(f"Vendored modules did not load: {missing}")


def _assert_module_spec_within(module_name: str, trusted_root: Path) -> None:
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        origin = getattr(loaded, "__file__", None)
        if not origin:
            raise RuntimeError(f"Hermes host module has no verifiable origin: {module_name}")
        _assert_origin_within(origin, (trusted_root,), f"Hermes module {module_name}")
        return

    try:
        specification = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError) as exc:
        raise RuntimeError(f"Cannot inspect Hermes host module: {module_name}") from exc
    if specification is None:
        raise RuntimeError(f"Hermes host module is missing: {module_name}")
    origins: list[str | os.PathLike[str]] = []
    if specification.origin not in {None, "built-in", "frozen"}:
        origins.append(specification.origin)
    if specification.submodule_search_locations is not None:
        origins.extend(specification.submodule_search_locations)
    if not origins:
        raise RuntimeError(f"Hermes host module has no inspectable origin: {module_name}")
    for origin in origins:
        _assert_origin_within(origin, (trusted_root,), f"Hermes module {module_name}")


def _assert_loaded_host_module_origins(trusted_root: Path) -> None:
    top_levels = {module_name for module_name, _ in _HOST_DEPENDENCIES.values()}
    top_levels.update(_HOST_AUXILIARY_MODULES)
    for name, module in tuple(sys.modules.items()):
        if name.split(".", 1)[0] not in top_levels:
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            raise RuntimeError(f"Hermes host module has no verifiable origin: {name}")
        _assert_origin_within(origin, (trusted_root,), f"Hermes module {name}")


def _verify_host_dependencies() -> None:
    trusted_root = _verified_hermes_venv_root()
    if trusted_root is None:
        raise RuntimeError("Hermes host dependency verification is unavailable")
    _reject_unbound_openpyxl_accelerators()
    for distribution_name, contract in _HOST_DEPENDENCIES.items():
        module_name, expected_version = contract
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"Hermes host dependency is missing: {distribution_name}"
            ) from exc
        version_text = distribution.version
        if version_text != expected_version:
            raise RuntimeError(
                "Hermes host dependency has an unsupported version: "
                f"{distribution_name} {version_text}"
            )
        _assert_origin_within(
            distribution.locate_file(""),
            (trusted_root,),
            f"Hermes distribution {distribution_name}",
        )
        _assert_module_spec_within(module_name, trusted_root)
    for module_name in _HOST_AUXILIARY_MODULES:
        _assert_module_spec_within(module_name, trusted_root)

    for module_name, _ in _HOST_DEPENDENCIES.values():
        module = importlib.import_module(module_name)
        origin = getattr(module, "__file__", None)
        if not origin:
            raise RuntimeError(f"Hermes host module has no verifiable origin: {module_name}")
        _assert_origin_within(origin, (trusted_root,), f"Hermes module {module_name}")
    for module_name in _HOST_AUXILIARY_MODULES:
        module = importlib.import_module(module_name)
        origin = getattr(module, "__file__", None)
        if not origin:
            raise RuntimeError(f"Hermes host module has no verifiable origin: {module_name}")
        _assert_origin_within(origin, (trusted_root,), f"Hermes module {module_name}")
    _assert_loaded_host_module_origins(trusted_root)


def _reject_unbound_openpyxl_accelerators() -> None:
    for module_name in _FORBIDDEN_OPTIONAL_MODULES:
        if module_name in sys.modules:
            raise RuntimeError(f"Unsupported optional Excel module was preloaded: {module_name}")
        try:
            specification = importlib.util.find_spec(module_name)
        except (ImportError, AttributeError, ValueError) as exc:
            raise RuntimeError(f"Cannot inspect optional Excel module: {module_name}") from exc
        if specification is not None:
            raise RuntimeError(f"Unsupported optional Excel module is installed: {module_name}")
    os.environ["OPENPYXL_LXML"] = "False"
    os.environ["OPENPYXL_DEFUSEDXML"] = "True"


def _verify_vendored_dependencies(import_root: Path, manifest: dict[str, Any]) -> None:
    expected = manifest["distributions"]
    discovered: dict[str, str] = {}
    for distribution in importlib.metadata.distributions(path=[str(import_root)]):
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        normalized_name = _normalized_distribution_name(raw_name)
        for expected_name in _VENDORED_DISTRIBUTIONS:
            if normalized_name == _normalized_distribution_name(expected_name):
                if expected_name in discovered:
                    raise RuntimeError(
                        f"Duplicate vendored distribution metadata: {expected_name}"
                    )
                discovered[expected_name] = distribution.version
    if discovered != expected:
        raise RuntimeError(
            "Vendored distribution metadata differs from the manifest "
            f"(expected={expected}, actual={discovered})"
        )

    _reject_unbound_openpyxl_accelerators()
    _assert_vendored_module_origins(import_root, require_all=False)
    for module_name in _VENDORED_DISTRIBUTIONS.values():
        importlib.import_module(module_name)
    _assert_vendored_module_origins(import_root, require_all=True)
    openpyxl = sys.modules["openpyxl"]
    if getattr(openpyxl, "LXML", None) is not False:
        raise RuntimeError("Vendored openpyxl did not disable lxml")
    if getattr(openpyxl, "DEFUSEDXML", None) is not True:
        raise RuntimeError("Vendored openpyxl did not enable defusedxml")
    trusted_host = _verified_hermes_venv_root()
    if trusted_host is None:
        raise RuntimeError("Hermes host dependency verification is unavailable")
    _assert_loaded_host_module_origins(trusted_host)


def _assert_package_origin(import_root: Path) -> None:
    """Fail if the trusted package was preloaded from another location."""

    resolved_root = import_root.resolve(strict=True)
    for name, module in tuple(sys.modules.items()):
        if name != "notion_excel_sync" and not name.startswith("notion_excel_sync."):
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            raise RuntimeError(f"Trusted module has no verifiable origin: {name}")
        try:
            Path(origin).resolve(strict=True).relative_to(resolved_root)
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError(
                f"Trusted module was loaded outside the verified runtime: {name}"
            ) from exc


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return True


def _sanitize_vendored_import_path(import_root: Path) -> None:
    """Keep only verified runtime, Hermes, and standard-library import roots."""

    interpreter_roots = _trusted_interpreter_roots()
    hermes_venv = _verified_hermes_venv_root()
    if hermes_venv is None:
        raise RuntimeError("Installed plugin requires the verified Hermes host environment")
    allowed_roots = interpreter_roots + (hermes_venv.parent, import_root)
    retained: list[str] = []
    for entry in sys.path:
        candidate = Path.cwd() if not entry else Path(entry).expanduser()
        if not entry or not any(_path_is_within(candidate, root) for root in allowed_roots):
            continue
        retained.append(entry)
    sys.path[:] = retained


def _assert_no_project_modules(import_root: Path) -> None:
    """Reject package modules already imported from the mutable source checkout."""

    project_package = (_PROJECT_ROOT / "src" / "notion_excel_sync").resolve(strict=True)
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        candidate = Path(origin)
        if _path_is_within(candidate, project_package) and not _path_is_within(
            candidate,
            import_root,
        ):
            raise RuntimeError(
                f"Credentialed plugin refused project-checkout module: {name}"
            )


if _VENDORED_ROOT.is_dir() or _MANIFEST_PATH.exists():
    _RUNTIME_MODE = "vendored"
    _MANIFEST_DIGEST, _RUNTIME_MANIFEST = _verify_vendored_runtime()
    _IMPORT_ROOT = _VENDORED_ROOT.resolve(strict=True)
else:
    if os.environ.get(_DEVELOPMENT_ENV, "").strip() != "1":
        raise RuntimeError(
            "Uninstalled Hermes plugin refused to load. Run install-hermes-plugin.ps1; "
            f"set {_DEVELOPMENT_ENV}=1 only for repository tests."
        )
    _RUNTIME_MODE = "development"
    _MANIFEST_DIGEST = None
    _RUNTIME_MANIFEST = None
    _DEVELOPMENT_PROJECT_ROOT = _PLUGIN_DIR.parents[2]
    _IMPORT_ROOT = (_DEVELOPMENT_PROJECT_ROOT / "src").resolve(strict=True)
    if not (_IMPORT_ROOT / "notion_excel_sync" / "security" / "hermes_gateway.py").is_file():
        raise RuntimeError("Development runtime does not contain the trusted gateway module")

if _RUNTIME_MODE == "vendored":
    _sanitize_vendored_import_path(_IMPORT_ROOT)
    _assert_no_project_modules(_IMPORT_ROOT)
_assert_package_origin(_IMPORT_ROOT)
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

# Trusted source trees must not acquire unmanifested bytecode after verification.
# Set both controls: ``sys.dont_write_bytecode`` protects this interpreter and
# the environment variable protects isolated extractor subprocesses.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
if _RUNTIME_MODE == "vendored":
    _verify_host_dependencies()
    assert _RUNTIME_MANIFEST is not None
    _verify_vendored_dependencies(_IMPORT_ROOT, _RUNTIME_MANIFEST)

_COMMANDS = frozenset(
    {
        "/nx_sync",
        "/nx_show",
        "/nx_set",
        "/nx_reject",
        "/nx_recover",
        "/nx_correct",
        "/nx_approve",
        "/nx_schema_plan",
        "/nx_schema_show",
        "/nx_schema_reject",
        "/nx_schema_approve",
        "/nx_schema_recover",
    }
)
_ACTIVE: set[str] = set()
_ACTIVE_STARTED: dict[str, float] = {}
_ACTIVE_STAGE: dict[str, str] = {}
_ACTIVE_PROGRESS: dict[str, asyncio.Task[None]] = {}
_ACTIVE_LOCK = threading.RLock()
logger = logging.getLogger(__name__)
_SYNC_ACCEPTED_MARKER = (
    "[NX_SYNC_ACCEPTED] stage=preparing elapsed_seconds=0. "
    "인증된 동기화 요청을 접수했습니다. 준비 중에는 Notion을 변경하지 않으며, "
    "모든 Notion 변경에는 별도의 정확한 Telegram 승인이 필요합니다."
)


def _release_active(key: str) -> None:
    """Release one command only after its trusted worker has really stopped."""

    with _ACTIVE_LOCK:
        _ACTIVE.discard(key)
        _ACTIVE_STARTED.pop(key, None)
        _ACTIVE_STAGE.pop(key, None)


async def _await_shielded_completion(task: asyncio.Task[Any]) -> Any:
    """Ignore wrapper cancellation while a trusted worker or delivery is active."""

    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            if task.done():
                return task.result()


def _recognized(event: Any) -> bool:
    text = getattr(event, "text", None)
    if not isinstance(text, str):
        return False
    tokens = text.strip().split(maxsplit=1)
    return bool(tokens and tokens[0] in _COMMANDS)


def _active_key(event: Any) -> str:
    """Serialize all state transitions for one proposal inside this gateway."""

    text = str(getattr(event, "text", "") or "").strip()
    parts = text.split()
    if parts and parts[0] == "/nx_sync":
        return "source-sync"
    if parts and parts[0] == "/nx_correct":
        return "correction\n" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    if parts and parts[0] == "/nx_schema_plan":
        template = parts[1] if len(parts) >= 2 else "malformed"
        return "schema-plan\n" + template
    if len(parts) >= 2:
        return "proposal\n" + parts[1]
    source = getattr(event, "source", None)
    return "malformed\n" + str(getattr(source, "chat_id", "") or "") + "\n" + text


async def _send_result(event: Any, gateway: Any, marker: str) -> bool:
    source = getattr(event, "source", None)
    adapter_for_source = getattr(gateway, "_adapter_for_source", None)
    adapter = adapter_for_source(source) if callable(adapter_for_source) else None
    if adapter is None:
        logger.error("Cannot deliver notion-excel-sync result: no Telegram adapter")
        return False
    metadata: dict[str, str] = {}
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if thread_id:
        metadata["thread_id"] = thread_id
    result = await adapter.send(
        str(getattr(source, "chat_id", "")),
        marker,
        reply_to=str(getattr(event, "message_id", "") or "") or None,
        metadata=metadata or None,
    )
    if getattr(result, "success", True) is not True:
        logger.error("Telegram delivery failed for notion-excel-sync result")
        return False
    return True


def _verified_gateway_module() -> Any:
    """Load the trusted gateway only after validating the installed runtime."""

    if _RUNTIME_MODE == "vendored":
        _verify_vendored_runtime(expected_manifest_digest=_MANIFEST_DIGEST)
        _assert_no_project_modules(_IMPORT_ROOT)
        _verify_host_dependencies()
        assert _RUNTIME_MANIFEST is not None
        _verify_vendored_dependencies(_IMPORT_ROOT, _RUNTIME_MANIFEST)
    _assert_package_origin(_IMPORT_ROOT)
    from notion_excel_sync.security import hermes_gateway

    return hermes_gateway


async def _run_guarded(event: Any, gateway: Any, key: str) -> None:
    loop = asyncio.get_running_loop()

    def notify_authenticated_sync_start() -> None:
        with _ACTIVE_LOCK:
            if key in _ACTIVE:
                _ACTIVE_STAGE[key] = "preparing"
        delivery = asyncio.run_coroutine_threadsafe(
            _send_result(event, gateway, _SYNC_ACCEPTED_MARKER),
            loop,
        )
        try:
            if not delivery.result(timeout=10.0):
                logger.error(
                    "Could not deliver authenticated synchronization start marker"
                )
        except Exception:
            delivery.cancel()
            logger.error(
                "Could not deliver authenticated synchronization start marker"
            )

    def execute_trusted_command() -> str | None:
        try:
            hermes_gateway = _verified_gateway_module()
            return hermes_gateway.handle_pre_gateway_dispatch(
                event=event,
                gateway=gateway,
                project_root=_PROJECT_ROOT,
                on_authenticated_sync_start=notify_authenticated_sync_start,
            )
        finally:
            # asyncio cancellation cannot stop a thread started by to_thread.
            # Keep the command scope owned until that thread actually exits.
            _release_active(key)

    worker: asyncio.Task[str | None] | None = None
    try:
        worker = asyncio.create_task(asyncio.to_thread(execute_trusted_command))
        marker = await _await_shielded_completion(worker)
        if not marker:
            raise RuntimeError("Recognized workflow command produced no decision")
    except Exception:
        logger.exception("Trusted notion-excel-sync worker failed closed")
        marker = (
            "[NX_SYNC_ERROR] 신뢰된 Hermes worker가 안전하게 중단되었습니다. "
            "검증되지 않은 Notion 쓰기는 허용되지 않았습니다."
        )
    finally:
        if worker is None:
            _release_active(key)
    try:
        delivery = asyncio.create_task(_send_result(event, gateway, marker))
        await _await_shielded_completion(delivery)
    except Exception:
        logger.exception("Could not deliver trusted notion-excel-sync result")


async def _report_sync_in_progress(
    event: Any,
    gateway: Any,
    key: str,
    active_started: float,
) -> None:
    with _ACTIVE_LOCK:
        if _ACTIVE_STARTED.get(key) != active_started:
            return
        stage = _ACTIVE_STAGE.get(key, "authenticating")
    elapsed_seconds = min(
        max(int(time.monotonic() - active_started), 0),
        86_400,
    )

    def authenticated_progress_marker() -> str:
        hermes_gateway = _verified_gateway_module()
        return hermes_gateway.authenticated_sync_progress_marker(
            event=event,
            gateway=gateway,
            project_root=_PROJECT_ROOT,
            stage=stage,
            elapsed_seconds=elapsed_seconds,
        )

    try:
        worker = asyncio.create_task(
            asyncio.to_thread(authenticated_progress_marker)
        )
        marker = await _await_shielded_completion(worker)
        with _ACTIVE_LOCK:
            if _ACTIVE_STARTED.get(key) != active_started:
                return
        delivery = asyncio.create_task(_send_result(event, gateway, marker))
        await _await_shielded_completion(delivery)
    except Exception:
        logger.error("Could not deliver synchronization progress marker")


def _forget_progress_task(
    key: str,
    task: asyncio.Task[None],
) -> None:
    with _ACTIVE_LOCK:
        if _ACTIVE_PROGRESS.get(key) is task:
            _ACTIVE_PROGRESS.pop(key, None)


def _schedule_sync_progress(
    event: Any,
    gateway: Any,
    key: str,
) -> None:
    """Coalesce a burst of duplicate sync requests into one authenticated read."""

    loop = asyncio.get_running_loop()
    with _ACTIVE_LOCK:
        active_started = _ACTIVE_STARTED.get(key)
        if active_started is None:
            return
        existing = _ACTIVE_PROGRESS.get(key)
        if existing is not None and not existing.done():
            return
        task = loop.create_task(
            _report_sync_in_progress(
                event,
                gateway,
                key,
                active_started,
            )
        )
        _ACTIVE_PROGRESS[key] = task
    task.add_done_callback(
        lambda finished, active_key=key: _forget_progress_task(
            active_key,
            finished,
        )
    )


def _pre_gateway_dispatch(*, event: Any, gateway: Any, **_: Any) -> dict[str, str] | None:
    """Consume workflow commands and run trusted work off the event loop."""

    if not _recognized(event):
        return None
    try:
        source = getattr(event, "source", None)
        platform = getattr(source, "platform", "")
        platform_name = str(getattr(platform, "value", platform) or "").lower()
        if platform_name != "telegram":
            return {"action": "skip", "reason": "nx-command-non-telegram"}
        key = _active_key(event)
        with _ACTIVE_LOCK:
            if key in _ACTIVE:
                if str(getattr(event, "text", "") or "").strip().split(
                    maxsplit=1
                )[0] == "/nx_sync":
                    try:
                        _schedule_sync_progress(event, gateway, key)
                    except Exception:
                        logger.error(
                            "Could not schedule synchronization progress marker"
                        )
                return {"action": "skip", "reason": "nx-command-in-progress"}
            _ACTIVE.add(key)
            _ACTIVE_STARTED[key] = time.monotonic()
            _ACTIVE_STAGE[key] = "authenticating"
        guarded = asyncio.get_running_loop().create_task(
            _run_guarded(event, gateway, key)
        )
        guarded.add_done_callback(
            lambda task, active_key=key: (
                _release_active(active_key) if task.cancelled() else None
            )
        )
    except Exception:
        logger.exception("Could not schedule trusted notion-excel-sync worker")
        _release_active(locals().get("key", ""))
    return {"action": "skip", "reason": "nx-command-consumed"}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
