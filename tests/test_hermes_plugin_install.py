from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class HermesPluginInstallationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.installer = cls.project_root / "scripts" / "install-hermes-plugin.ps1"
        cls.hermes_home = Path(os.environ["LOCALAPPDATA"]) / "hermes"
        cls.hermes_runtime = cls.hermes_home / "hermes-agent"
        config = (cls.hermes_runtime / "venv" / "pyvenv.cfg").read_text(
            encoding="utf-8"
        )
        home_line = next(line for line in config.splitlines() if line.startswith("home = "))
        cls.hermes_python = Path(home_line.split("=", 1)[1].strip()) / "python.exe"

    @staticmethod
    def _load_plugin(
        plugin_path: Path,
        project_root: Path,
        *,
        fake_vendored_path: Path | None = None,
        untrusted_import_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        code = """
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

path = Path(os.environ["NX_TEST_PLUGIN"])
project_root = os.environ["NOTION_EXCEL_SYNC_PROJECT_ROOT"]
sys.path.insert(0, project_root)
hermes_runtime = Path(os.environ["NX_TEST_HERMES_RUNTIME"])
hermes_venv = hermes_runtime / "venv"
sys.path[:0] = [
    str(hermes_venv / "Lib" / "site-packages"),
    str(hermes_runtime),
]
untrusted_import_root = os.environ.get("NX_TEST_UNTRUSTED_IMPORT_ROOT")
if untrusted_import_root:
    sys.path.insert(0, untrusted_import_root)
hermes_cli = types.ModuleType("hermes_cli")
hermes_cli.__file__ = str(hermes_runtime / "hermes_cli" / "__init__.py")
sys.modules["hermes_cli"] = hermes_cli
fake_vendored_path = os.environ.get("NX_TEST_FAKE_VENDORED_PATH")
if fake_vendored_path:
    fake_msal = types.ModuleType("msal")
    fake_msal.__file__ = fake_vendored_path
    sys.modules["msal"] = fake_msal
spec = importlib.util.spec_from_file_location("nx_installed_plugin_test", path)
if spec is None or spec.loader is None:
    raise RuntimeError("plugin spec unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from notion_excel_sync.security import hermes_gateway

vendored_modules = ("openpyxl", "et_xmlfile", "msal", "msal_extensions")
print(json.dumps({
    "mode": module._RUNTIME_MODE,
    "root": str(module._IMPORT_ROOT),
    "project_on_path": project_root in sys.path,
    "gateway": str(Path(hermes_gateway.__file__).resolve()),
    "vendored": {
        name: str(Path(sys.modules[name].__file__).resolve())
        for name in vendored_modules
    },
}))
"""
        environment = os.environ.copy()
        environment.pop("NOTION_EXCEL_SYNC_DEV_PLUGIN", None)
        environment["NOTION_EXCEL_SYNC_PROJECT_ROOT"] = str(project_root)
        environment["NX_TEST_PLUGIN"] = str(plugin_path)
        environment["NX_TEST_HERMES_RUNTIME"] = str(
            HermesPluginInstallationTest.hermes_runtime
        )
        environment["HERMES_HOME"] = str(HermesPluginInstallationTest.hermes_home)
        environment["VIRTUAL_ENV"] = str(
            HermesPluginInstallationTest.hermes_runtime / "venv"
        )
        if fake_vendored_path is not None:
            environment["NX_TEST_FAKE_VENDORED_PATH"] = str(fake_vendored_path)
        else:
            environment.pop("NX_TEST_FAKE_VENDORED_PATH", None)
        if untrusted_import_root is not None:
            environment["NX_TEST_UNTRUSTED_IMPORT_ROOT"] = str(untrusted_import_root)
        else:
            environment.pop("NX_TEST_UNTRUSTED_IMPORT_ROOT", None)
        return subprocess.run(
            [str(HermesPluginInstallationTest.hermes_python), "-I", "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )

    def test_installer_vendors_verifies_and_acl_protects_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hermes_home = Path(temporary) / "hermes"
            stale_skill = hermes_home / "skills" / "notion-excel-sync" / "stale.txt"
            stale_skill.parent.mkdir(parents=True)
            stale_skill.write_text("stale", encoding="utf-8")
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.installer),
                    "-HermesHome",
                    str(hermes_home),
                    "-HermesRuntimeRoot",
                    str(self.hermes_runtime),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            plugin_dir = hermes_home / "plugins" / "notion-excel-sync-attestor"
            plugin_path = plugin_dir / "__init__.py"
            runtime_root = plugin_dir / "runtime"
            manifest_path = plugin_dir / "runtime-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            actual = {
                path.relative_to(runtime_root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in runtime_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(manifest["files"], actual)
            self.assertIn(
                "notion_excel_sync/security/hermes_gateway.py",
                actual,
            )
            for schema_runtime_file in (
                "notion_excel_sync/security/hermes_schema_gateway.py",
                "notion_excel_sync/security/schema_approval.py",
                "notion_excel_sync/security/schema_recovery.py",
                "notion_excel_sync/workflow/notion_schema.py",
                "notion_excel_sync/workflow/data_sources.py",
            ):
                self.assertIn(schema_runtime_file, actual)
            self.assertIn(
                'version: "0.5.0"',
                (plugin_dir / "plugin.yaml").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                set(manifest["distributions"]),
                {"openpyxl", "et-xmlfile", "msal", "msal-extensions"},
            )
            self.assertEqual(
                manifest["host_requirements"],
                {
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
                },
            )
            for distribution_name in manifest["distributions"]:
                normalized = distribution_name.replace("-", "_")
                self.assertTrue(
                    any(
                        path.startswith(normalized + "-")
                        and ".dist-info/" in path
                        for path in actual
                    ),
                    distribution_name,
                )
            self.assertFalse(stale_skill.exists())

            loaded = self._load_plugin(plugin_path, self.project_root)
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            payload = json.loads(loaded.stdout.strip())
            self.assertEqual(payload["mode"], "vendored")
            self.assertEqual(Path(payload["root"]), runtime_root.resolve())
            self.assertFalse(payload["project_on_path"])
            self.assertTrue(
                Path(payload["gateway"]).is_relative_to(runtime_root.resolve())
            )
            for origin in payload["vendored"].values():
                self.assertTrue(Path(origin).is_relative_to(runtime_root.resolve()))

            untrusted_root = Path(temporary) / "untrusted"
            fake_requests = untrusted_root / "requests"
            fake_requests.mkdir(parents=True)
            execution_marker = Path(temporary) / "fake-requests-executed"
            (fake_requests / "__init__.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(execution_marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            protected = self._load_plugin(
                plugin_path,
                self.project_root,
                untrusted_import_root=untrusted_root,
            )
            self.assertEqual(protected.returncode, 0, protected.stderr)
            self.assertFalse(execution_marker.exists())

            fake_msal = Path(temporary) / "untrusted-msal.py"
            fake_msal.write_text("# fake\n", encoding="utf-8")
            preloaded = self._load_plugin(
                plugin_path,
                self.project_root,
                fake_vendored_path=fake_msal,
            )
            self.assertNotEqual(preloaded.returncode, 0)
            self.assertIn("preloaded outside the runtime", preloaded.stderr)

            acl_environment = os.environ.copy()
            acl_environment["NX_TEST_PLUGIN_DIR"] = str(plugin_dir)
            acl = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "(Get-Acl -LiteralPath $env:NX_TEST_PLUGIN_DIR).AreAccessRulesProtected",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                env=acl_environment,
            )
            self.assertEqual(acl.returncode, 0, acl.stderr)
            self.assertEqual(acl.stdout.strip(), "True")

            gateway = runtime_root / "notion_excel_sync" / "security" / "hermes_gateway.py"
            gateway.write_bytes(gateway.read_bytes() + b"\n# tampered\n")
            rejected = self._load_plugin(plugin_path, self.project_root)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("hash mismatch", rejected.stderr)

    def test_repository_shim_requires_explicit_development_mode(self) -> None:
        source_plugin = (
            self.project_root
            / ".hermes"
            / "plugins"
            / "notion-excel-sync-attestor"
            / "__init__.py"
        )
        rejected = self._load_plugin(source_plugin, self.project_root)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("Uninstalled Hermes plugin refused to load", rejected.stderr)

    def test_enable_failure_restores_previous_plugin_and_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hermes_home = Path(temporary) / "hermes"
            old_plugin = hermes_home / "plugins" / "notion-excel-sync-attestor"
            old_skill = hermes_home / "skills" / "notion-excel-sync"
            old_plugin.mkdir(parents=True)
            old_skill.mkdir(parents=True)
            (old_plugin / "old.txt").write_text("old-plugin", encoding="utf-8")
            (old_skill / "old.txt").write_text("old-skill", encoding="utf-8")

            fake_bin = Path(temporary) / "bin"
            fake_bin.mkdir()
            (fake_bin / "hermes.cmd").write_text("@exit /b 9\n", encoding="ascii")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.installer),
                    "-ProjectRoot",
                    str(self.project_root),
                    "-HermesHome",
                    str(hermes_home),
                    "-HermesRuntimeRoot",
                    str(self.hermes_runtime),
                    "-Enable",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                (old_plugin / "old.txt").read_text(encoding="utf-8"),
                "old-plugin",
            )
            self.assertEqual(
                (old_skill / "old.txt").read_text(encoding="utf-8"),
                "old-skill",
            )
            self.assertFalse((old_plugin / "runtime").exists())
            self.assertEqual(
                list((hermes_home / "plugins").glob(".*.staging-*")),
                [],
            )
            self.assertEqual(
                list((hermes_home / "skills").glob(".*.staging-*")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
