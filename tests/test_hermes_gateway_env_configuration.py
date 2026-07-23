from __future__ import annotations

import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class HermesGatewayEnvironmentConfigurationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.script = cls.project_root / "scripts" / "configure-hermes-gateway-env.ps1"

    def _run(self, project: Path, hermes_home: Path) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("HERMES_HOME", None)
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script),
                "-ProjectRoot",
                str(project),
                "-HermesHome",
                str(hermes_home),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )

    @staticmethod
    def _acl_fingerprint(path: Path) -> str:
        environment = os.environ.copy()
        environment["NX_ACL_PATH"] = str(path)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$a=Get-Acl -LiteralPath $env:NX_ACL_PATH; "
                'Write-Output ($a.Sddl + "|Protected=" + '
                "$a.AreAccessRulesProtected.ToString())",
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
    def _make_layout(root: Path, env_bytes: bytes) -> tuple[Path, Path]:
        project = root / "project with spaces"
        config = project / "config" / "sync.local.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        hermes_home = root / "hermes"
        hermes_home.mkdir()
        env_path = hermes_home / ".env"
        env_path.write_bytes(env_bytes)
        environment = os.environ.copy()
        environment["NX_ACL_PATH"] = str(env_path)
        normalized = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$a=Get-Acl -LiteralPath $env:NX_ACL_PATH; "
                "Set-Acl -LiteralPath $env:NX_ACL_PATH -AclObject $a",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        if normalized.returncode != 0:
            raise AssertionError(normalized.stderr)
        return project, hermes_home

    def test_preserves_existing_content_and_is_byte_idempotent(self) -> None:
        secret = "secret-canary-never-print"
        original = f"# existing settings\r\nUNRELATED_SECRET={secret}\r\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home = self._make_layout(Path(temporary), original)
            env_path = hermes_home / ".env"
            original_acl = self._acl_fingerprint(env_path)
            first = self._run(project, hermes_home)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotIn(secret, first.stdout + first.stderr)
            configured = env_path.read_bytes()
            self.assertTrue(configured.startswith(original))
            self.assertEqual(
                configured.count(b"NOTION_EXCEL_SYNC_PROJECT_ROOT="),
                1,
            )
            self.assertEqual(configured.count(b"NOTION_EXCEL_SYNC_CONFIG="), 1)
            self.assertNotIn(b"\n", configured.replace(b"\r\n", b""))
            self.assertRegex(first.stdout, r"SettingsChanged\s+: True")
            self.assertEqual(self._acl_fingerprint(env_path), original_acl)
            self.assertEqual(list(hermes_home.glob(".env.notion-excel-sync.*")), [])

            second = self._run(project, hermes_home)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(env_path.read_bytes(), configured)
            self.assertRegex(second.stdout, r"SettingsChanged\s+: False")

    def test_removes_duplicate_variants_but_keeps_commented_examples(self) -> None:
        existing = (
            b"# NOTION_EXCEL_SYNC_PROJECT_ROOT=commented\n"
            b"export NOTION_EXCEL_SYNC_PROJECT_ROOT = 'old'\n"
            b"notion_excel_sync_project_root=older\n"
            b"NOTION_EXCEL_SYNC_CONFIG=old-config\n"
            b"export 'NOTION_EXCEL_SYNC_CONFIG' = 'quoted-old-config'\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home = self._make_layout(Path(temporary), existing)
            result = self._run(project, hermes_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            configured = (hermes_home / ".env").read_text(encoding="utf-8")
            self.assertIn("# NOTION_EXCEL_SYNC_PROJECT_ROOT=commented\n", configured)
            active = [
                line
                for line in configured.splitlines()
                if line and not line.lstrip().startswith("#")
            ]
            self.assertEqual(
                sum(line.startswith("NOTION_EXCEL_SYNC_PROJECT_ROOT=") for line in active),
                1,
            )
            self.assertEqual(
                sum(line.startswith("NOTION_EXCEL_SYNC_CONFIG=") for line in active),
                1,
            )
            self.assertNotIn("quoted-old-config", configured)

    def test_preserves_utf8_bom_and_quotes_dotenv_special_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project # O'Brien"
            config = project / "config" / "sync.local.json"
            config.parent.mkdir(parents=True)
            config.write_text("{}\n", encoding="utf-8")
            hermes_home = root / "hermes"
            hermes_home.mkdir()
            (hermes_home / ".env").write_bytes(b"\xef\xbb\xbf# keep\n")

            result = self._run(project, hermes_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            configured = (hermes_home / ".env").read_bytes()
            self.assertTrue(configured.startswith(b"\xef\xbb\xbf# keep\n"))
            text = configured[3:].decode("utf-8")
            expected_project = str(project).replace("\\", "\\\\").replace('"', '\\"')
            expected_config = str(config).replace("\\", "\\\\").replace('"', '\\"')
            self.assertIn(
                f'NOTION_EXCEL_SYNC_PROJECT_ROOT="{expected_project}"\n',
                text,
            )
            self.assertIn(f'NOTION_EXCEL_SYNC_CONFIG="{expected_config}"\n', text)

            hermes_runtime = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent"
            pyvenv = (hermes_runtime / "venv" / "pyvenv.cfg").read_text(
                encoding="utf-8"
            )
            python_home = next(
                line.split("=", 1)[1].strip()
                for line in pyvenv.splitlines()
                if line.startswith("home = ")
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "NX_DOTENV_PATH": str(hermes_home / ".env"),
                    "NX_EXPECT_PROJECT": str(project),
                    "NX_EXPECT_CONFIG": str(config),
                    "NX_HERMES_SITE": str(hermes_runtime / "venv" / "Lib" / "site-packages"),
                }
            )
            loaded = subprocess.run(
                [
                    str(Path(python_home) / "python.exe"),
                    "-I",
                    "-c",
                    "import json,os,sys; "
                    "sys.path.insert(0,os.environ['NX_HERMES_SITE']); "
                    "from dotenv import dotenv_values; "
                    "v=dotenv_values(os.environ['NX_DOTENV_PATH']); "
                    "print(json.dumps({"
                    "'project':v.get('NOTION_EXCEL_SYNC_PROJECT_ROOT')=="
                    "os.environ['NX_EXPECT_PROJECT'],"
                    "'config':v.get('NOTION_EXCEL_SYNC_CONFIG')=="
                    "os.environ['NX_EXPECT_CONFIG']}))",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                env=environment,
            )
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertEqual(json.loads(loaded.stdout), {"project": True, "config": True})

    def test_rejects_invalid_encoding_without_changing_file(self) -> None:
        original = "SECRET=preserve\r\n".encode("utf-16")
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home = self._make_layout(Path(temporary), original)
            result = self._run(project, hermes_home)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("preserve", result.stdout + result.stderr)
            self.assertEqual((hermes_home / ".env").read_bytes(), original)

    def test_rejects_ambiguous_target_without_changing_file(self) -> None:
        original = b"OTHER=x NOTION_EXCEL_SYNC_CONFIG=combined\n"
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home = self._make_layout(Path(temporary), original)
            result = self._run(project, hermes_home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((hermes_home / ".env").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
