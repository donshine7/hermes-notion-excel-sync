from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


PLUGIN_NAME = "notion-excel-sync-attestor"
HERMES_AGENT_VERSION = "0.18.2"
EXPECTED_KIND = "standalone"
EXPECTED_HOOKS = ["pre_gateway_dispatch"]


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _require_regular_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("required_regular_file_missing")
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    reparse_flag = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse_flag:
        raise RuntimeError("reparse_point_rejected")
    return path.resolve(strict=True)


def _verify_hermes_distribution(site_packages: Path) -> None:
    matches = tuple(site_packages.glob("hermes_agent-*.dist-info/METADATA"))
    if len(matches) != 1:
        raise RuntimeError("hermes_distribution_ambiguous")
    metadata = matches[0].read_text(encoding="utf-8")
    name = re.search(r"(?m)^Name:\s*([^\r\n]+)\s*$", metadata)
    version = re.search(r"(?m)^Version:\s*([^\r\n]+)\s*$", metadata)
    if (
        name is None
        or re.sub(r"[-_.]+", "-", name.group(1).strip()).lower() != "hermes-agent"
        or version is None
        or version.group(1).strip() != HERMES_AGENT_VERSION
    ):
        raise RuntimeError("unsupported_hermes_distribution")


def _read_plugin_manifest(path: Path, yaml_module: Any) -> dict[str, Any]:
    manifest_path = _require_regular_file(path / "plugin.yaml")
    manifest = yaml_module.safe_load(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise RuntimeError("invalid_plugin_manifest")
    if (
        manifest.get("name") != PLUGIN_NAME
        or manifest.get("kind") != EXPECTED_KIND
        or manifest.get("provides_hooks") != EXPECTED_HOOKS
    ):
        raise RuntimeError("unexpected_plugin_manifest")
    return manifest


def _plugins_mapping(config: dict[str, Any]) -> dict[str, Any]:
    current = config.get("plugins")
    if current is None:
        current = {}
        config["plugins"] = current
    if not isinstance(current, dict):
        raise RuntimeError("plugins_config_not_mapping")
    return current


def _string_set(value: Any, field: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"{field}_not_string_list")
    return set(value)


def _verified_state(config: dict[str, Any], aliases: set[str]) -> bool:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return False
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    entries = plugins.get("entries")
    if not isinstance(enabled, list) or PLUGIN_NAME not in enabled:
        return False
    if not isinstance(disabled, list) or any(alias in disabled for alias in aliases):
        return False
    if not isinstance(entries, dict):
        return False
    entry = entries.get(PLUGIN_NAME)
    return isinstance(entry, dict) and entry.get("allow_tool_override") is False


def _validate_raw_config(config: dict[str, Any]) -> None:
    plugins = config.get("plugins")
    if plugins is None:
        return
    if not isinstance(plugins, dict):
        raise RuntimeError("plugins_config_not_mapping")
    _string_set(plugins.get("enabled"), "enabled")
    _string_set(plugins.get("disabled"), "disabled")
    entries = plugins.get("entries")
    if entries is None:
        return
    if not isinstance(entries, dict):
        raise RuntimeError("plugin_entries_not_mapping")
    entry = entries.get(PLUGIN_NAME)
    if entry is not None and not isinstance(entry, dict):
        raise RuntimeError("plugin_entry_not_mapping")


def _activate(args: argparse.Namespace) -> dict[str, Any]:
    hermes_home = Path(args.hermes_home).expanduser().resolve(strict=True)
    hermes_runtime = Path(args.hermes_runtime).expanduser().resolve(strict=True)
    if not args.allow_external_runtime_for_test:
        expected_runtime = (hermes_home / "hermes-agent").resolve(strict=True)
        if hermes_runtime != expected_runtime:
            raise RuntimeError("hermes_runtime_mismatch")

    venv = (hermes_runtime / "venv").resolve(strict=True)
    site_packages = (venv / "Lib" / "site-packages").resolve(strict=True)
    if not site_packages.is_dir() or not _within(site_packages, venv):
        raise RuntimeError("hermes_site_packages_missing")
    _verify_hermes_distribution(site_packages)
    for trusted_module in (
        hermes_runtime / "hermes_cli" / "__init__.py",
        hermes_runtime / "hermes_cli" / "config.py",
        hermes_runtime / "hermes_cli" / "plugins_cmd.py",
    ):
        _require_regular_file(trusted_module)

    plugin_root = (hermes_home / "plugins").resolve(strict=True)
    plugin_dir = (plugin_root / PLUGIN_NAME).resolve(strict=True)
    if not _within(plugin_dir, plugin_root):
        raise RuntimeError("plugin_path_escape")

    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["VIRTUAL_ENV"] = str(venv)
    for inherited_name in (
        "HERMES_CONFIG",
        "HERMES_ENV",
        "HERMES_PROFILE",
        "NOTION_EXCEL_SYNC_DEV_PLUGIN",
        "PYTHONPATH",
    ):
        os.environ.pop(inherited_name, None)
    sys.path[:0] = [str(hermes_runtime), str(site_packages)]

    import yaml
    from hermes_cli import config as hermes_config
    from hermes_cli import plugins_cmd

    config_origin = Path(hermes_config.__file__).resolve(strict=True)
    command_origin = Path(plugins_cmd.__file__).resolve(strict=True)
    if not _within(config_origin, hermes_runtime) or not _within(
        command_origin, hermes_runtime
    ):
        raise RuntimeError("untrusted_hermes_module_origin")

    manifest = _read_plugin_manifest(plugin_dir, yaml)
    resolved = plugins_cmd._resolve_plugin_key_and_source(PLUGIN_NAME)
    if resolved != (PLUGIN_NAME, "user"):
        raise RuntimeError("plugin_identity_resolution_failed")

    config_path = _require_regular_file(hermes_home / "config.yaml")
    # Hermes load_config() intentionally falls back to defaults on malformed
    # YAML. Activation is a write path, so independently reject any unreadable,
    # undecodable, malformed, or non-mapping user config before calling it.
    raw_text = config_path.read_bytes().decode("utf-8-sig")
    raw_config = yaml.safe_load(raw_text)
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise RuntimeError("config_root_not_mapping")
    _validate_raw_config(raw_config)

    config = hermes_config.load_config()
    if not isinstance(config, dict):
        raise RuntimeError("config_root_not_mapping")

    manifest_name = str(manifest["name"])
    aliases = {PLUGIN_NAME, PLUGIN_NAME.split("/")[-1], manifest_name}
    if _verified_state(config, aliases):
        return {
            "ok": True,
            "plugin": PLUGIN_NAME,
            "enabled": True,
            "override_allowed": False,
            "changed": False,
            "restart_required": True,
        }

    plugins = _plugins_mapping(config)
    enabled = _string_set(plugins.get("enabled"), "enabled")
    disabled = _string_set(plugins.get("disabled"), "disabled")
    enabled.add(PLUGIN_NAME)
    disabled.difference_update(aliases)
    plugins["enabled"] = sorted(enabled)
    plugins["disabled"] = sorted(disabled)

    entries = plugins.get("entries")
    if entries is None:
        entries = {}
        plugins["entries"] = entries
    if not isinstance(entries, dict):
        raise RuntimeError("plugin_entries_not_mapping")
    entry = entries.get(PLUGIN_NAME)
    if entry is None:
        entry = {}
        entries[PLUGIN_NAME] = entry
    if not isinstance(entry, dict):
        raise RuntimeError("plugin_entry_not_mapping")
    entry["allow_tool_override"] = False

    hermes_config.save_config(
        config,
        preserve_keys={
            ("plugins", "enabled"),
            ("plugins", "disabled"),
            ("plugins", "entries", PLUGIN_NAME, "allow_tool_override"),
        },
    )

    verified = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(verified, dict) or not _verified_state(verified, aliases):
        raise RuntimeError("activation_verification_failed")
    return {
        "ok": True,
        "plugin": PLUGIN_NAME,
        "enabled": True,
        "override_allowed": False,
        "changed": True,
        "restart_required": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--hermes-runtime", required=True)
    parser.add_argument("--allow-external-runtime-for-test", action="store_true")
    args = parser.parse_args()

    hidden_stdout = io.StringIO()
    hidden_stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(hidden_stdout), contextlib.redirect_stderr(
            hidden_stderr
        ):
            result = _activate(args)
    except BaseException as exc:  # keep config contents and library diagnostics private
        detail = str(exc)
        safe_detail = (
            detail
            if isinstance(exc, RuntimeError) and re.fullmatch(r"[a-z0-9_]+", detail)
            else "internal_error"
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "activation_failed",
                    "detail": safe_detail,
                    "error_type": type(exc).__name__,
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
