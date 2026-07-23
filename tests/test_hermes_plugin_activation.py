from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class HermesPluginActivationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.script = cls.project_root / "scripts" / "activate-hermes-plugin.ps1"
        cls.plugin_source = (
            cls.project_root / ".hermes" / "plugins" / "notion-excel-sync-attestor"
        )
        cls.hermes_home = Path(os.environ["LOCALAPPDATA"]) / "hermes"
        cls.hermes_runtime = cls.hermes_home / "hermes-agent"
        pyvenv = (cls.hermes_runtime / "venv" / "pyvenv.cfg").read_text(
            encoding="utf-8"
        )
        python_home = next(
            line.split("=", 1)[1].strip()
            for line in pyvenv.splitlines()
            if line.startswith("home = ")
        )
        cls.hermes_python = Path(python_home) / "python.exe"
        cls.hermes_site = cls.hermes_runtime / "venv" / "Lib" / "site-packages"

    def _load_config(self, path: Path) -> dict:
        environment = os.environ.copy()
        environment["NX_CONFIG_PATH"] = str(path)
        environment["NX_HERMES_SITE"] = str(self.hermes_site)
        result = subprocess.run(
            [
                str(self.hermes_python),
                "-I",
                "-c",
                "import json,os,sys; "
                "sys.path.insert(0,os.environ['NX_HERMES_SITE']); "
                "import yaml; "
                "d=yaml.safe_load(open(os.environ['NX_CONFIG_PATH'],"
                "encoding='utf-8-sig')) or {}; "
                "print(json.dumps(d))",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def _normalize_file_acl(path: Path) -> str:
        environment = os.environ.copy()
        environment["NX_ACL_PATH"] = str(path)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$a=Get-Acl -LiteralPath $env:NX_ACL_PATH; "
                "Set-Acl -LiteralPath $env:NX_ACL_PATH -AclObject $a; "
                "$b=Get-Acl -LiteralPath $env:NX_ACL_PATH; "
                'Write-Output ($b.Sddl + "|Protected=" + '
                "$b.AreAccessRulesProtected.ToString())",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    @staticmethod
    def _protect_directory(path: Path) -> None:
        environment = os.environ.copy()
        environment["NX_ACL_PATH"] = str(path)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$u=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
                "$a=New-Object Security.AccessControl.DirectorySecurity; "
                "$a.SetOwner($u); $a.SetAccessRuleProtection($true,$false); "
                "$i=[Security.AccessControl.InheritanceFlags]::ContainerInherit "
                "-bor [Security.AccessControl.InheritanceFlags]::ObjectInherit; "
                "$p=[Security.AccessControl.PropagationFlags]::None; "
                "$t=[Security.AccessControl.AccessControlType]::Allow; "
                "$f=[Security.AccessControl.FileSystemRights]::FullControl; "
                "$s=@($u,(New-Object Security.Principal.SecurityIdentifier"
                "('S-1-5-18')),(New-Object Security.Principal.SecurityIdentifier"
                "('S-1-5-32-544'))); foreach($x in $s){$r=New-Object "
                "Security.AccessControl.FileSystemAccessRule($x,$f,$i,$p,$t); "
                "[void]$a.AddAccessRule($r)}; "
                "Set-Acl -LiteralPath $env:NX_ACL_PATH -AclObject $a; "
                "if (-not (Get-Acl -LiteralPath $env:NX_ACL_PATH)."
                "AreAccessRulesProtected) { exit 9 }",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)

    def _layout(
        self,
        root: Path,
        config_text: str,
        *,
        protect_plugin: bool = True,
    ) -> tuple[Path, Path]:
        home = root / "hermes"
        plugin = home / "plugins" / "notion-excel-sync-attestor"
        plugin.mkdir(parents=True)
        shutil.copy2(self.plugin_source / "plugin.yaml", plugin / "plugin.yaml")
        shutil.copy2(self.plugin_source / "__init__.py", plugin / "__init__.py")
        (plugin / "runtime-manifest.json").write_text("{}\n", encoding="utf-8")
        if protect_plugin:
            self._protect_directory(plugin)
        config = home / "config.yaml"
        config.write_text(config_text, encoding="utf-8")
        return home, config

    @staticmethod
    def _write_valid_runtime(plugin: Path) -> Path:
        gateway = plugin / "runtime" / "notion_excel_sync" / "security" / "hermes_gateway.py"
        gateway.parent.mkdir(parents=True)
        gateway.write_text("# trusted test gateway\n", encoding="utf-8")
        digest = hashlib.sha256(gateway.read_bytes()).hexdigest()
        manifest = {
            "format": 1,
            "algorithm": "sha256",
            "package": "notion_excel_sync",
            "distributions": {
                "openpyxl": "3.1.5",
                "et-xmlfile": "2.0.0",
                "msal": "1.37.0",
                "msal-extensions": "1.3.1",
            },
            "host_requirements": {
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
            "files": {"notion_excel_sync/security/hermes_gateway.py": digest},
        }
        (plugin / "runtime-manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        return gateway

    def _run(
        self,
        home: Path,
        *,
        skip_integrity: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("HERMES_HOME", None)
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            "-HermesHome",
            str(home),
            "-HermesRuntimeRoot",
            str(self.hermes_runtime),
            "-AllowExternalHermesRuntimeForTest",
        ]
        if skip_integrity:
            command.append("-SkipInstalledIntegrityCheckForTest")
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            env=environment,
            cwd=cwd,
        )

    def test_activates_once_preserves_other_values_and_denies_override(self) -> None:
        canary = "private-config-canary"
        original = (
            "custom:\n"
            f"  retained: {canary}\n"
            "plugins:\n"
            "  enabled:\n"
            "    - other-plugin\n"
            "  disabled:\n"
            "    - notion-excel-sync-attestor\n"
            "    - other-disabled\n"
            "  entries:\n"
            "    notion-excel-sync-attestor:\n"
            "      allow_tool_override: true\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            home, config = self._layout(Path(temporary), original)
            original_acl = self._normalize_file_acl(config)
            first = self._run(home)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotIn(canary, first.stdout + first.stderr)
            configured = self._load_config(config)
            self.assertEqual(configured["custom"]["retained"], canary)
            plugins = configured["plugins"]
            self.assertIn("other-plugin", plugins["enabled"])
            self.assertIn("notion-excel-sync-attestor", plugins["enabled"])
            self.assertNotIn("notion-excel-sync-attestor", plugins["disabled"])
            self.assertIn("other-disabled", plugins["disabled"])
            self.assertIs(
                plugins["entries"]["notion-excel-sync-attestor"][
                    "allow_tool_override"
                ],
                False,
            )
            self.assertEqual(self._normalize_file_acl(config), original_acl)
            self.assertEqual(list(home.glob(".config.notion-excel-sync.*")), [])
            self.assertRegex(first.stdout, r"Enabled\s+: True")
            self.assertRegex(first.stdout, r"AllowToolOverride\s+: False")

            first_bytes = config.read_bytes()
            second = self._run(home)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(config.read_bytes(), first_bytes)
            self.assertRegex(second.stdout, r"ConfigChanged\s+: False")

    def test_missing_plugin_fails_without_changing_config(self) -> None:
        canary = "failure-canary-private"
        with tempfile.TemporaryDirectory() as temporary:
            home, config = self._layout(
                Path(temporary),
                f"custom:\n  retained: {canary}\n",
            )
            shutil.rmtree(home / "plugins" / "notion-excel-sync-attestor")
            original = config.read_bytes()
            result = self._run(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), original)
            self.assertNotIn(canary, result.stdout + result.stderr)

    def test_invalid_configs_fail_without_changing_bytes(self) -> None:
        cases = (
            b"plugins: [unterminated\n",
            b"- root-is-a-list\n",
            b"plugins:\n  enabled: not-a-list\n",
            b"custom: \xff\n",
        )
        for original in cases:
            with self.subTest(original=original[:20]):
                with tempfile.TemporaryDirectory() as temporary:
                    home, config = self._layout(Path(temporary), "custom: safe\n")
                    config.write_bytes(original)
                    self._normalize_file_acl(config)
                    result = self._run(home)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(config.read_bytes(), original)
                    self.assertEqual(
                        list(home.glob(".config.notion-excel-sync.*")),
                        [],
                    )

    def test_runtime_integrity_preflight_accepts_valid_and_rejects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, config = self._layout(Path(temporary), "custom: retained\n")
            plugin = home / "plugins" / "notion-excel-sync-attestor"
            self._write_valid_runtime(plugin)
            valid = self._run(home, skip_integrity=False)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertTrue(
                self._load_config(config)["plugins"]["entries"]
                ["notion-excel-sync-attestor"]["allow_tool_override"]
                is False
            )

        with tempfile.TemporaryDirectory() as temporary:
            home, config = self._layout(Path(temporary), "custom: retained\n")
            plugin = home / "plugins" / "notion-excel-sync-attestor"
            gateway = self._write_valid_runtime(plugin)
            gateway.write_text("# tampered\n", encoding="utf-8")
            original = config.read_bytes()
            rejected = self._run(home, skip_integrity=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(config.read_bytes(), original)

    def test_rejects_unprotected_acl_and_ignores_cwd_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, config = self._layout(root, "custom: retained\n")
            marker = root / "shadow-executed"
            shadow = root / "hermes_cli"
            shadow.mkdir()
            (shadow / "__init__.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            result = self._run(home, cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, config = self._layout(
                root,
                "custom: retained\n",
                protect_plugin=False,
            )
            original = config.read_bytes()
            rejected = self._run(home)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(config.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
