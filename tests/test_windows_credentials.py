from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from notion_excel_sync.config import ConfigError, load_config
from notion_excel_sync.security.windows_credentials import (
    WindowsCredentialError,
    credential_target,
    read_windows_generic_credential,
)


def _config() -> dict[str, object]:
    return {
        "onedrive": {"drive_id": "drive", "item_id": "item"},
        "excel": {"sheets": ["Cases"]},
        "notion": {
            "read_token_env": "NOTION_READ_TOKEN",
            "write_token_env": "GATEWAY_RELAY_NX_NOTION_TOKEN",
        },
        "approval": {
            "secret_env": "GATEWAY_RELAY_NX_APPROVAL_SECRET",
            "allowed_telegram_users": ["123"],
        },
    }


class WindowsCredentialResolverTest(unittest.TestCase):
    def load(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "sync.json"
        path.write_text(json.dumps(_config()), encoding="utf-8")
        return load_config(path)

    def test_target_is_stable_and_rejects_unsafe_names(self) -> None:
        self.assertEqual(
            credential_target("NOTION_READ_TOKEN"),
            "NotionExcelSync:NOTION_READ_TOKEN",
        )
        for invalid in ("", "with space", "../secret", "name:other"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WindowsCredentialError):
                    credential_target(invalid)

    def test_non_windows_reader_is_a_noop(self) -> None:
        with patch(
            "notion_excel_sync.security.windows_credentials.os.name",
            "posix",
        ):
            self.assertIsNone(read_windows_generic_credential("NOTION_READ_TOKEN"))

    @unittest.skipUnless(os.name == "nt", "Credential Manager test requires Windows")
    def test_missing_windows_target_returns_none(self) -> None:
        name = "NX_TEST_MISSING_" + uuid.uuid4().hex.upper()
        self.assertIsNone(read_windows_generic_credential(name))

    def test_credential_manager_precedes_environment_fallback(self) -> None:
        config = self.load()
        with (
            patch(
                "notion_excel_sync.config.read_windows_generic_credential",
                return_value="credential-value",
            ),
            patch.dict(os.environ, {"NOTION_READ_TOKEN": "environment-value"}),
        ):
            self.assertEqual(
                config.secret("NOTION_READ_TOKEN"),
                "credential-value",
            )

    def test_environment_remains_a_missing_credential_fallback(self) -> None:
        config = self.load()
        with (
            patch(
                "notion_excel_sync.config.read_windows_generic_credential",
                return_value=None,
            ),
            patch.dict(os.environ, {"NOTION_READ_TOKEN": "environment-value"}),
        ):
            self.assertEqual(
                config.secret("NOTION_READ_TOKEN"),
                "environment-value",
            )

    def test_gateway_only_secrets_never_use_credential_manager(self) -> None:
        config = self.load()
        secret_name = "GATEWAY_RELAY_NX_NOTION_TOKEN"
        with (
            patch(
                "notion_excel_sync.config.read_windows_generic_credential",
                return_value="unsafe-stored-value",
            ) as reader,
            patch.dict(os.environ, {secret_name: "gateway-environment-value"}),
        ):
            self.assertEqual(
                config.secret(secret_name),
                "gateway-environment-value",
            )
            reader.assert_not_called()

    def test_empty_or_unreadable_stored_credential_fails_closed(self) -> None:
        config = self.load()
        with patch.dict(os.environ, {"NOTION_READ_TOKEN": "environment-value"}):
            with patch(
                "notion_excel_sync.config.read_windows_generic_credential",
                return_value="",
            ):
                with self.assertRaises(ConfigError):
                    config.secret("NOTION_READ_TOKEN")
            with patch(
                "notion_excel_sync.config.read_windows_generic_credential",
                side_effect=WindowsCredentialError("read failed"),
            ):
                with self.assertRaises(ConfigError):
                    config.secret("NOTION_READ_TOKEN")


class WindowsCredentialScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "store-windows-credentials.ps1"
        )

    def test_script_accepts_secrets_only_through_masked_prompt(self) -> None:
        source = self.script.read_text(encoding="utf-8")
        self.assertIn("Read-Host", source)
        self.assertIn("-AsSecureString", source)
        self.assertIn("SecureStringToBSTR", source)
        self.assertIn("ZeroFreeBSTR", source)
        self.assertIn('"NOTION_READ_TOKEN"', source)
        self.assertNotIn("GATEWAY_RELAY_NX_NOTION_TOKEN", source)
        self.assertNotIn("GATEWAY_RELAY_NX_APPROVAL_SECRET", source)
        self.assertNotIn("ConvertFrom-SecureString", source)
        self.assertNotIn("cmdkey", source.lower())
        self.assertNotRegex(source, r"\[string\]\s*\$(Secret|Token|Value)\b")

    @unittest.skipUnless(os.name == "nt", "PowerShell parser test requires Windows")
    def test_script_parses_in_windows_powershell(self) -> None:
        environment = os.environ.copy()
        environment["NX_CREDENTIAL_SCRIPT"] = str(self.script)
        command = (
            "$errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:NX_CREDENTIAL_SCRIPT, [ref]$null, [ref]$errors); "
            "if ($errors.Count) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    @unittest.skipUnless(os.name == "nt", "PowerShell test requires Windows")
    def test_script_rejects_gateway_only_secret_before_prompt(self) -> None:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script),
                "-Names",
                "GATEWAY_RELAY_NX_NOTION_TOKEN",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Only NOTION_READ_TOKEN is allowed", result.stderr)


if __name__ == "__main__":
    unittest.main()
