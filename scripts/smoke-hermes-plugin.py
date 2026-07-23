from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path


VENDORED_MODULES = ("openpyxl", "et_xmlfile", "msal", "msal_extensions")
HOST_MODULES = (
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "jwt",
    "cryptography",
    "cffi",
    "_cffi_backend",
    "pycparser",
    "PIL",
    "defusedxml",
    "portalocker",
)


def _within(path: str | Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--hermes-runtime", type=Path, required=True)
    arguments = parser.parse_args()

    plugin_path = arguments.plugin.resolve(strict=True)
    project_root = arguments.project_root.resolve(strict=True)
    hermes_runtime = arguments.hermes_runtime.resolve(strict=True)
    hermes_home = hermes_runtime.parent
    hermes_venv = (hermes_runtime / "venv").resolve(strict=True)
    site_packages = (hermes_venv / "Lib" / "site-packages").resolve(strict=True)
    hermes_cli_init = (hermes_runtime / "hermes_cli" / "__init__.py").resolve(strict=True)

    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["VIRTUAL_ENV"] = str(hermes_venv)
    os.environ["NOTION_EXCEL_SYNC_PROJECT_ROOT"] = str(project_root)
    os.environ.pop("NOTION_EXCEL_SYNC_DEV_PLUGIN", None)
    sys.path[:0] = [str(site_packages), str(hermes_runtime), str(project_root)]

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__file__ = str(hermes_cli_init)
    sys.modules["hermes_cli"] = hermes_cli

    specification = importlib.util.spec_from_file_location(
        "nx_installed_plugin_smoke",
        plugin_path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Installed plugin specification is unavailable")
    plugin = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(plugin)

    from notion_excel_sync.adapters.onedrive_auth import AtomicFileLock
    from notion_excel_sync.security import hermes_gateway
    from msal_extensions import build_encrypted_persistence

    runtime_root = plugin._IMPORT_ROOT.resolve(strict=True)
    if plugin._RUNTIME_MODE != "vendored":
        raise RuntimeError("Installed plugin did not select its vendored runtime")
    if not _within(hermes_gateway.__file__, runtime_root):
        raise RuntimeError("Gateway module escaped the installed runtime")
    for module_name in VENDORED_MODULES:
        module = sys.modules[module_name]
        if not _within(module.__file__, runtime_root):
            raise RuntimeError(f"Vendored module escaped the installed runtime: {module_name}")
    for module_name in HOST_MODULES:
        module = sys.modules[module_name]
        if not _within(module.__file__, hermes_venv):
            raise RuntimeError(f"Host module escaped the Hermes venv: {module_name}")
    lock_module = sys.modules[AtomicFileLock.__module__]
    if not _within(lock_module.__file__, runtime_root):
        raise RuntimeError("MSAL cache lock did not come from the vendored runtime")
    if any(_within(entry, project_root) for entry in sys.path if entry):
        raise RuntimeError("Project checkout remained on the credentialed import path")

    with tempfile.TemporaryDirectory() as folder:
        cache_path = Path(folder) / "cache.bin"
        persistence = build_encrypted_persistence(str(cache_path))
        if not persistence.is_encrypted:
            raise RuntimeError("MSAL smoke persistence is not encrypted")
        persistence.save('{"smoke":true}')
        if persistence.load() != '{"smoke":true}':
            raise RuntimeError("MSAL DPAPI smoke round-trip failed")
        with AtomicFileLock(str(cache_path) + ".lockfile"):
            pass

    print(
        json.dumps(
            {
                "mode": plugin._RUNTIME_MODE,
                "runtime": str(runtime_root),
                "hermes_venv": str(hermes_venv),
                "openpyxl_lxml": sys.modules["openpyxl"].LXML,
                "openpyxl_defusedxml": sys.modules["openpyxl"].DEFUSEDXML,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
