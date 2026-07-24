"""Internal worker for the secure, one-shot Windows Hermes restart.

This module is intentionally launched by ``restart-hermes-gateway-secure.ps1``
with the uv-managed *base* Python.  Paths and mode are non-secret environment
settings; the Notion write token is accepted only as a single line on the
worker's anonymous standard-input pipe. A second, fixed ``COMMIT`` line is sent
only after the worker reports ``STATUS=PREPARED``. The approval signing secret
is made inside this short-lived process and is never returned to the caller.

The two gateway-only values necessarily become part of the detached gateway
process environment.  They are never accepted from, or persisted to, the
launcher's environment, command line, a file, or Windows Credential Manager.
"""

from __future__ import annotations

import base64
import contextlib
import gc
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any


PLUGIN_NAME = "notion-excel-sync-attestor"
WRITE_TOKEN_ENV = "GATEWAY_RELAY_NX_NOTION_TOKEN"
APPROVAL_SECRET_ENV = "GATEWAY_RELAY_NX_APPROVAL_SECRET"
REQUIRED_GATEWAY_FILE = "notion_excel_sync/security/hermes_gateway.py"
EXPECTED_DISTRIBUTIONS = {
    "openpyxl": "3.1.5",
    "et-xmlfile": "2.0.0",
    "msal": "1.37.0",
    "msal-extensions": "1.3.1",
}
EXPECTED_HOST_REQUIREMENTS = {
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
EXPECTED_PLUGIN_FILES = {
    "plugin.yaml": "fafa9a353a821fa34d88c306515766eb14059c6108172eea3d3f5a90ad5e7ddd",
    "__init__.py": "308f1633c76ad9252a99b0cdd826d84c1a8d0fa12fd6f354bf9688db846c74f0",
}
MAX_TOKEN_BYTES = 512
MIN_TOKEN_BYTES = 20

_MODE_ENV = "NX_SECURE_GATEWAY_MODE"
_HOME_ENV = "NX_SECURE_GATEWAY_HERMES_HOME"
_RUNTIME_ENV = "NX_SECURE_GATEWAY_RUNTIME"
_PROJECT_ENV = "NX_SECURE_GATEWAY_PROJECT_ROOT"
_CONFIG_ENV = "NX_SECURE_GATEWAY_CONFIG"
_INTERNAL_ENV_NAMES = (_MODE_ENV, _HOME_ENV, _RUNTIME_ENV, _PROJECT_ENV, _CONFIG_ENV)
_TRUSTED_YAML: Any | None = None


class PreflightError(RuntimeError):
    """A fail-closed, deliberately non-secret preflight failure."""


class SecretInputError(RuntimeError):
    """The pipe payload was absent or did not meet the token contract."""


def _full_path(raw: str, *, kind: str) -> Path:
    if not raw or "\x00" in raw:
        raise PreflightError("invalid path")
    path = Path(raw).absolute()
    try:
        info = path.lstat()
    except OSError as exc:
        raise PreflightError("required path unavailable") from exc
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise PreflightError("required file unavailable")
    if kind == "directory" and not stat.S_ISDIR(info.st_mode):
        raise PreflightError("required directory unavailable")
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PreflightError("reparse points are not trusted")
    return path


def _assert_child(root: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PreflightError("path escaped its trusted root") from exc

    current = candidate
    while current != root:
        try:
            info = current.lstat()
        except OSError as exc:
            raise PreflightError("required path unavailable") from exc
        if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PreflightError("reparse points are not trusted")
        current = current.parent


def _read_utf8(path: Path) -> str:
    try:
        payload = path.read_bytes()
        if payload.startswith(b"\xef\xbb\xbf"):
            payload = payload[3:]
        text = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise PreflightError("invalid UTF-8 input") from exc
    if "\x00" in text:
        raise PreflightError("NUL data is not supported")
    return text


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_utf8(path))
    except (json.JSONDecodeError, TypeError) as exc:
        raise PreflightError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise PreflightError("JSON root must be an object")
    return value


def _configure_trusted_imports(runtime: Path) -> None:
    global _TRUSTED_YAML

    site_packages = _full_path(
        str(runtime / "venv" / "Lib" / "site-packages"),
        kind="directory",
    )
    # Isolated/no-site Python omits PYTHONPATH and startup customizations. Load
    # YAML with the venv dependency directory first and validate its origin
    # before the Hermes source root is added, so a same-named checkout file can
    # never execute as a dependency shadow.
    inherited = [entry for entry in sys.path if entry]
    sys.path[:] = [str(site_packages), *inherited]
    try:
        import yaml
    except Exception as exc:
        raise PreflightError("Hermes YAML dependency unavailable") from exc
    module_path = _full_path(str(Path(yaml.__file__)), kind="file")
    _assert_child(site_packages, module_path)
    _TRUSTED_YAML = yaml
    # Hermes itself is imported only after its exact installer-owned source
    # root takes precedence over dependencies and the worker directory.
    sys.path.insert(0, str(runtime))


def _safe_load_yaml(path: Path) -> dict[str, Any]:
    if _TRUSTED_YAML is None:
        raise PreflightError("Hermes YAML dependency unavailable")
    try:
        value = _TRUSTED_YAML.safe_load(_read_utf8(path))
    except Exception as exc:
        raise PreflightError("invalid YAML") from exc
    if not isinstance(value, dict):
        raise PreflightError("YAML root must be a mapping")
    return value


def _validate_uv_runtime(runtime: Path) -> None:
    pyvenv = _full_path(str(runtime / "venv" / "pyvenv.cfg"), kind="file")
    text = _read_utf8(pyvenv)
    if not re.search(r"(?im)^\s*uv\s*=\s*\S+\s*$", text):
        raise PreflightError("Hermes environment is not uv-managed")
    if not re.search(r"(?im)^\s*home\s*=\s*\S.*$", text):
        raise PreflightError("uv base Python home is missing")


def _validate_sync_config(config_path: Path) -> None:
    config = _read_json(config_path)
    notion = config.get("notion")
    approval = config.get("approval")
    excel = config.get("excel")
    if not isinstance(notion, dict) or not isinstance(approval, dict):
        raise PreflightError("sync secret routing is incomplete")
    if notion.get("write_token_env") != WRITE_TOKEN_ENV:
        raise PreflightError("write-token routing differs from the gateway contract")
    if approval.get("secret_env") != APPROVAL_SECRET_ENV:
        raise PreflightError("approval-secret routing differs from the gateway contract")
    if notion.get("read_token_env") == WRITE_TOKEN_ENV:
        raise PreflightError("read and write integrations must remain separated")
    if not isinstance(excel, dict):
        raise PreflightError("Excel configuration is missing")
    sheets = excel.get("sheets")
    header_row = excel.get("header_row")
    if (
        not isinstance(sheets, list)
        or not sheets
        or any(not isinstance(item, str) or not item.strip() for item in sheets)
        or isinstance(header_row, bool)
        or not isinstance(header_row, int)
        or header_row < 1
    ):
        raise PreflightError("Excel source configuration is invalid")


def _validate_no_persisted_gateway_secrets(home: Path, project: Path) -> None:
    key_pattern = re.compile(
        r"^\s*(?:export\s+)?['\"]?(?:"
        + re.escape(WRITE_TOKEN_ENV)
        + "|"
        + re.escape(APPROVAL_SECRET_ENV)
        + r")[\"']?\s*=",
        re.IGNORECASE,
    )
    candidates = (home / ".env", project / ".env")
    for candidate in candidates:
        if not candidate.exists():
            continue
        checked = _full_path(str(candidate), kind="file")
        if any(key_pattern.match(line) for line in _read_utf8(checked).splitlines()):
            raise PreflightError("gateway-only secrets must not be persisted")


def _validate_manifest(plugin: Path) -> None:
    manifest_path = _full_path(str(plugin / "runtime-manifest.json"), kind="file")
    manifest = _read_json(manifest_path)
    if (
        manifest.get("format") != 1
        or manifest.get("algorithm") != "sha256"
        or manifest.get("package") != "notion_excel_sync"
        or manifest.get("distributions") != EXPECTED_DISTRIBUTIONS
        or manifest.get("host_requirements") != EXPECTED_HOST_REQUIREMENTS
    ):
        raise PreflightError("installed runtime manifest contract differs")
    files = manifest.get("files")
    if not isinstance(files, dict) or REQUIRED_GATEWAY_FILE not in files or not files:
        raise PreflightError("installed runtime manifest is incomplete")

    runtime = _full_path(str(plugin / "runtime"), kind="directory")
    expected_paths: set[str] = set()
    for relative, expected_digest in files.items():
        if (
            not isinstance(relative, str)
            or not isinstance(expected_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        ):
            raise PreflightError("installed runtime manifest entry is invalid")
        relative_path = Path(relative.replace("/", os.sep))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise PreflightError("installed runtime manifest path is unsafe")
        installed = _full_path(str(runtime / relative_path), kind="file")
        _assert_child(runtime, installed)
        expected_paths.add(relative)
        try:
            actual = hashlib.sha256(installed.read_bytes()).hexdigest()
        except OSError as exc:
            raise PreflightError("installed runtime file unavailable") from exc
        if not secrets.compare_digest(actual, expected_digest):
            raise PreflightError("installed runtime integrity check failed")

    actual_paths: set[str] = set()
    try:
        for installed in runtime.rglob("*"):
            info = installed.lstat()
            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise PreflightError("installed runtime contains a reparse point")
            if stat.S_ISREG(info.st_mode):
                actual_paths.add(installed.relative_to(runtime).as_posix())
    except OSError as exc:
        raise PreflightError("installed runtime tree is unavailable") from exc
    if actual_paths != expected_paths:
        raise PreflightError("installed runtime file set differs from its manifest")


def _validate_plugin(home: Path) -> None:
    plugin = _full_path(str(home / "plugins" / PLUGIN_NAME), kind="directory")
    installed_yaml = _full_path(str(plugin / "plugin.yaml"), kind="file")
    installed_shim = _full_path(str(plugin / "__init__.py"), kind="file")
    for installed, name in (
        (installed_yaml, "plugin.yaml"),
        (installed_shim, "__init__.py"),
    ):
        installed_digest = hashlib.sha256(installed.read_bytes()).hexdigest()
        if not secrets.compare_digest(installed_digest, EXPECTED_PLUGIN_FILES[name]):
            raise PreflightError("installed plugin shim integrity check failed")

    plugin_yaml = _safe_load_yaml(installed_yaml)
    if plugin_yaml.get("name") != PLUGIN_NAME:
        raise PreflightError("installed plugin identity differs")
    hooks = plugin_yaml.get("provides_hooks")
    if not isinstance(hooks, list) or "pre_gateway_dispatch" not in hooks:
        raise PreflightError("installed plugin hook contract differs")
    _validate_manifest(plugin)

    hermes_config = _safe_load_yaml(
        _full_path(str(home / "config.yaml"), kind="file")
    )
    plugins = hermes_config.get("plugins")
    if not isinstance(plugins, dict):
        raise PreflightError("Hermes plugin configuration is missing")
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled", [])
    entries = plugins.get("entries")
    if not isinstance(enabled, list) or PLUGIN_NAME not in enabled:
        raise PreflightError("installed plugin is not enabled")
    if isinstance(disabled, list) and PLUGIN_NAME in disabled:
        raise PreflightError("installed plugin is disabled")
    if not isinstance(entries, dict):
        raise PreflightError("installed plugin entry is missing")
    entry = entries.get(PLUGIN_NAME)
    if not isinstance(entry, dict) or entry.get("allow_tool_override") is not False:
        raise PreflightError("plugin tool override must be denied")


def _paths_from_environment() -> tuple[str, Path, Path, Path, Path]:
    mode = os.environ.get(_MODE_ENV, "")
    if mode not in {"preflight", "restart"}:
        raise PreflightError("invalid mode")
    home = _full_path(os.environ.get(_HOME_ENV, ""), kind="directory")
    runtime = _full_path(os.environ.get(_RUNTIME_ENV, ""), kind="directory")
    project = _full_path(os.environ.get(_PROJECT_ENV, ""), kind="directory")
    config = _full_path(os.environ.get(_CONFIG_ENV, ""), kind="file")
    _assert_child(project, config)
    expected_runtime = (home / "hermes-agent").absolute()
    if os.path.normcase(str(runtime)) != os.path.normcase(str(expected_runtime)):
        raise PreflightError("Hermes runtime is outside Hermes home")
    return mode, home, runtime, project, config


def _preflight(home: Path, runtime: Path, project: Path, config: Path) -> None:
    _validate_uv_runtime(runtime)
    _configure_trusted_imports(runtime)
    _validate_sync_config(config)
    _validate_no_persisted_gateway_secrets(home, project)
    _validate_plugin(home)


def _read_token_from_pipe() -> bytearray:
    # The PowerShell wrapper verifies that its input is either a masked console
    # prompt or an anonymous pipe. This worker receives only the wrapper's own
    # redirected StandardInput pipe and accepts exactly one bounded ASCII line.
    raw = bytearray(sys.stdin.buffer.readline(MAX_TOKEN_BYTES + 2))
    terminated = raw.endswith(b"\n")
    while raw.endswith(b"\n") or raw.endswith(b"\r"):
        raw.pop()
    if not terminated or not MIN_TOKEN_BYTES <= len(raw) <= MAX_TOKEN_BYTES:
        for index in range(len(raw)):
            raw[index] = 0
        raise SecretInputError("invalid secret input")
    if any(value < 0x21 or value > 0x7E for value in raw):
        for index in range(len(raw)):
            raw[index] = 0
        raise SecretInputError("invalid secret input")
    return raw


def _read_commit_from_pipe() -> None:
    commit = bytearray(sys.stdin.buffer.readline(9))
    extra = sys.stdin.buffer.read(1)
    try:
        while commit.endswith(b"\n") or commit.endswith(b"\r"):
            commit.pop()
        if extra or commit != b"COMMIT":
            raise SecretInputError("invalid commit input")
    finally:
        for index in range(len(commit)):
            commit[index] = 0


def _trusted_gateway_module(runtime: Path) -> Any:
    try:
        from hermes_cli import gateway_windows
    except Exception as exc:
        raise PreflightError("Hermes gateway module unavailable") from exc
    module_path = _full_path(str(Path(gateway_windows.__file__)), kind="file")
    _assert_child(runtime, module_path)
    return gateway_windows


def _restart(home: Path, runtime: Path, token_bytes: bytearray) -> int:
    approval_bytes = bytearray()
    token_text: str | None = None
    approval_text: str | None = None
    spawned_pid: int | None = None
    try:
        # Import and drain the previous gateway before materializing the CSPRNG
        # approval secret or creating immutable environment strings.
        os.environ.pop(WRITE_TOKEN_ENV, None)
        os.environ.pop(APPROVAL_SECRET_ENV, None)
        os.environ["HERMES_HOME"] = str(home)
        gateway_windows = _trusted_gateway_module(runtime)
        # Do not relay library output: it is unnecessary for automation and an
        # unexpected dependency diagnostic must never become a secret oracle.
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                gateway_windows.stop()
                if not gateway_windows._wait_for_gateway_absent(timeout_s=8.0):
                    raise RuntimeError("old gateway remained active")

                approval_bytes.extend(secrets.token_bytes(48))
                token_text = token_bytes.decode("ascii", errors="strict")
                approval_text = base64.urlsafe_b64encode(approval_bytes).decode("ascii")
                os.environ[WRITE_TOKEN_ENV] = token_text
                os.environ[APPROVAL_SECRET_ENV] = approval_text
                spawned_pid = int(gateway_windows._spawn_detached())
                os.environ.pop(WRITE_TOKEN_ENV, None)
                os.environ.pop(APPROVAL_SECRET_ENV, None)
                for index in range(len(token_bytes)):
                    token_bytes[index] = 0
                for index in range(len(approval_bytes)):
                    approval_bytes[index] = 0
                token_text = None
                approval_text = None
                ready = [
                    int(item)
                    for item in gateway_windows._wait_for_gateway_ready(timeout_s=10.0)
                ]
                if spawned_pid <= 0 or spawned_pid not in ready:
                    try:
                        gateway_windows.stop()
                    except Exception:
                        pass
                    raise RuntimeError("new gateway failed readiness")
        return spawned_pid
    finally:
        os.environ.pop(WRITE_TOKEN_ENV, None)
        os.environ.pop(APPROVAL_SECRET_ENV, None)
        for index in range(len(token_bytes)):
            token_bytes[index] = 0
        for index in range(len(approval_bytes)):
            approval_bytes[index] = 0
        token_text = None
        approval_text = None
        gc.collect()


def main() -> int:
    try:
        mode, home, runtime, project, config = _paths_from_environment()
        for name in _INTERNAL_ENV_NAMES:
            os.environ.pop(name, None)
        os.environ.pop("NOTION_EXCEL_SYNC_DEV_PLUGIN", None)
        _preflight(home, runtime, project, config)
    except Exception:
        print("STATUS=PREFLIGHT_REJECTED")
        return 10

    if mode == "preflight":
        print("STATUS=PREFLIGHT_OK")
        return 0

    try:
        token_bytes = _read_token_from_pipe()
    except Exception:
        print("STATUS=INPUT_REJECTED")
        return 11

    # Two-way commit prevents the acknowledgement race: PREPARED guarantees that
    # the old Gateway is untouched, then the parent marks the worker no-kill
    # before it sends COMMIT. Only a valid commit allows _restart to begin.
    print("STATUS=PREPARED", flush=True)
    try:
        _read_commit_from_pipe()
    except Exception:
        for index in range(len(token_bytes)):
            token_bytes[index] = 0
        print("STATUS=COMMIT_REJECTED")
        return 13

    try:
        pid = _restart(home, runtime, token_bytes)
    except Exception:
        # _restart clears both mutable buffers and its temporary environment.
        print("STATUS=RESTART_REJECTED")
        return 12
    print("STATUS=READY")
    print(f"PID={pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
