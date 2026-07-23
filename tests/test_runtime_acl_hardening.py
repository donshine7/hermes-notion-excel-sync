from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows ACL hardening is Windows-only")
class RuntimeAclHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = Path(__file__).resolve().parents[1]
        cls.script = cls.repository / "scripts" / "harden-runtime-acl.ps1"

    @staticmethod
    def _make_layout(root: Path, *, escaped_state: bool = False) -> Path:
        project = root / "project with spaces"
        (project / "config").mkdir(parents=True)
        (project / "data" / "work" / "receipts").mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (project / "data" / "msal-token-cache.bin").write_bytes(b"cache-canary")
        (project / "data" / "work" / "receipts" / "receipt.json").write_text(
            "receipt-canary",
            encoding="utf-8",
        )
        hermes_home = root / "fake-hermes"
        hermes_home.mkdir()
        (hermes_home / ".env").write_bytes(
            b"TELEGRAM_TOKEN=fake-home-secret-canary\r\n"
        )
        state_db = "../../outside-state.db" if escaped_state else "../data/state.db"
        wiki_runtime = root / "fake-localappdata" / "notion-excel-sync" / "wiki"
        (wiki_runtime / "work").mkdir(parents=True)
        (wiki_runtime / "index.db").write_bytes(b"wiki-index-canary")
        configuration = {
            "state_db": state_db,
            "work_dir": "../data/work",
            "onedrive": {"token_cache": "../data/msal-token-cache.bin"},
            "wiki": {"index_db": str(wiki_runtime / "index.db")},
            "secret_canary": "must-never-appear-in-output",
        }
        (project / "config" / "sync.local.json").write_text(
            json.dumps(configuration, ensure_ascii=False),
            encoding="utf-8",
        )
        return project

    def _run(
        self,
        project: Path,
        *arguments: str,
        config: Path | None = None,
        environment_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            "-ProjectRoot",
            str(project),
            "-HermesHome",
            str(project.parent / "fake-hermes"),
        ]
        if config is not None:
            command.extend(["-ConfigPath", str(config)])
        command.extend(arguments)
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(project.parent / "fake-localappdata")
        for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            environment.pop(name, None)
        if environment_overrides:
            environment.update(environment_overrides)
        return subprocess.run(
            command,
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
                "$s=[System.Security.AccessControl.AccessControlSections]::Owner -bor "
                "[System.Security.AccessControl.AccessControlSections]::Group -bor "
                "[System.Security.AccessControl.AccessControlSections]::Access; "
                "$a=Get-Acl -LiteralPath $env:NX_ACL_PATH; "
                'Write-Output ($a.GetSecurityDescriptorSddlForm($s) + "|" + '
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
    def _acl_report(path: Path) -> dict[str, object]:
        environment = os.environ.copy()
        environment["NX_ACL_PATH"] = str(path)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$a=Get-Acl -LiteralPath $env:NX_ACL_PATH; "
                "$rules=@($a.GetAccessRules($true,$true,"
                "[System.Security.Principal.SecurityIdentifier])|ForEach-Object{"
                "[pscustomobject]@{Sid=$_.IdentityReference.Value;"
                "Rights=[int]$_.FileSystemRights;"
                "Inheritance=[int]$_.InheritanceFlags;"
                "Propagation=[int]$_.PropagationFlags;"
                "Type=[int]$_.AccessControlType;Inherited=$_.IsInherited}}); "
                "[pscustomobject]@{"
                "Owner=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;"
                "Protected=$a.AreAccessRulesProtected;"
                "CurrentSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
                "FullControl=[int][System.Security.AccessControl.FileSystemRights]::FullControl;"
                "Rules=$rules}|ConvertTo-Json -Compress -Depth 5",
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
    def _output_field(result: subprocess.CompletedProcess[str], name: str) -> str:
        match = re.search(rf"(?m)^\s*{re.escape(name)}\s*:\s*(\S+)", result.stdout)
        if match is None:
            raise AssertionError(f"Missing output field {name!r}: {result.stdout!r}")
        return match.group(1)

    def test_default_and_explicit_whatif_are_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._make_layout(Path(temporary))
            config = project / "config" / "sync.local.json"
            data = project / "data"
            wiki = project.parent / "fake-localappdata" / "notion-excel-sync" / "wiki"
            wiki_application = wiki.parent
            hermes_env = project.parent / "fake-hermes" / ".env"
            targets = [
                config,
                data,
                *data.rglob("*"),
                wiki,
                *wiki.rglob("*"),
                wiki_application,
                hermes_env,
            ]
            original = {
                str(path): self._acl_fingerprint(path)
                for path in targets
            }
            env_bytes = hermes_env.read_bytes()

            default = self._run(project)
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(self._output_field(default, "Mode"), "DryRun")
            self.assertEqual(self._output_field(default, "ChangesApplied"), "0")
            self.assertNotIn("must-never-appear-in-output", default.stdout + default.stderr)
            self.assertNotIn("fake-home-secret-canary", default.stdout + default.stderr)
            for path in targets:
                self.assertEqual(self._acl_fingerprint(path), original[str(path)])
            self.assertEqual(hermes_env.read_bytes(), env_bytes)

            what_if = self._run(project, "-Apply", "-WhatIf")
            self.assertEqual(what_if.returncode, 0, what_if.stderr)
            self.assertEqual(self._output_field(what_if, "Mode"), "WhatIf")
            self.assertEqual(self._output_field(what_if, "ChangesApplied"), "0")
            for path in targets:
                self.assertEqual(self._acl_fingerprint(path), original[str(path)])
            self.assertEqual(hermes_env.read_bytes(), env_bytes)

    def test_apply_uses_exact_policy_owns_every_target_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._make_layout(Path(temporary))
            config = project / "config" / "sync.local.json"
            data = project / "data"
            wiki = project.parent / "fake-localappdata" / "notion-excel-sync" / "wiki"
            wiki_application = wiki.parent
            hermes_env = project.parent / "fake-hermes" / ".env"
            env_bytes = hermes_env.read_bytes()
            targets = [
                config,
                *data.rglob("*"),
                data,
                wiki,
                *wiki.rglob("*"),
                wiki_application,
                hermes_env,
            ]

            first = self._run(project, "-Apply")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(self._output_field(first, "Mode"), "Apply")
            self.assertEqual(
                int(self._output_field(first, "ChangesRequired")),
                len(targets),
            )
            self.assertEqual(
                int(self._output_field(first, "ChangesApplied")),
                len(targets),
            )
            self.assertEqual(
                int(self._output_field(first, "TargetsEvaluated")),
                len(targets),
            )
            self.assertEqual(self._output_field(first, "Compliant"), "True")
            self.assertEqual(hermes_env.read_bytes(), env_bytes)
            self.assertNotIn("fake-home-secret-canary", first.stdout + first.stderr)

            config_acl = self._acl_report(config)
            expected_sids = {
                str(config_acl["CurrentSid"]),
                "S-1-5-18",
                "S-1-5-32-544",
            }
            for path in targets:
                report = self._acl_report(path)
                expected_inheritance = 3 if path.is_dir() else 0
                self.assertEqual(report["Owner"], report["CurrentSid"])
                self.assertTrue(report["Protected"])
                rules = report["Rules"]
                self.assertIsInstance(rules, list)
                self.assertEqual({str(rule["Sid"]) for rule in rules}, expected_sids)
                self.assertEqual(len(rules), len(expected_sids))
                for rule in rules:
                    self.assertEqual(rule["Rights"], report["FullControl"])
                    self.assertEqual(rule["Inheritance"], expected_inheritance)
                    self.assertEqual(rule["Propagation"], 0)
                    self.assertEqual(rule["Type"], 0)
                    self.assertFalse(rule["Inherited"])

            hardened = {
                str(path): self._acl_fingerprint(path)
                for path in targets
            }
            second = self._run(project, "-Apply")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(self._output_field(second, "ChangesRequired"), "0")
            self.assertEqual(self._output_field(second, "ChangesApplied"), "0")
            self.assertEqual(self._output_field(second, "Compliant"), "True")
            for path in targets:
                self.assertEqual(self._acl_fingerprint(path), hardened[str(path)])
            self.assertEqual(hermes_env.read_bytes(), env_bytes)

    def test_path_escape_fails_closed_before_acl_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._make_layout(Path(temporary), escaped_state=True)
            config = project / "config" / "sync.local.json"
            data = project / "data"
            hermes_env = project.parent / "fake-hermes" / ".env"
            before = (
                self._acl_fingerprint(config),
                self._acl_fingerprint(data),
                self._acl_fingerprint(hermes_env),
            )

            result = self._run(project, "-Apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("escapes its required project directory", result.stderr)
            self.assertNotIn("must-never-appear-in-output", result.stdout + result.stderr)
            self.assertEqual(
                (
                    self._acl_fingerprint(config),
                    self._acl_fingerprint(data),
                    self._acl_fingerprint(hermes_env),
                ),
                before,
            )

    def test_external_config_is_rejected_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._make_layout(root)
            external = root / "external-config.json"
            canary = "external-secret-canary"
            external.write_text(json.dumps({"value": canary}), encoding="utf-8")
            config = project / "config" / "sync.local.json"
            data = project / "data"
            hermes_env = project.parent / "fake-hermes" / ".env"
            before = (
                self._acl_fingerprint(config),
                self._acl_fingerprint(data),
                self._acl_fingerprint(hermes_env),
            )

            result = self._run(project, "-Apply", config=external)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(canary, result.stdout + result.stderr)
            self.assertEqual(
                (
                    self._acl_fingerprint(config),
                    self._acl_fingerprint(data),
                    self._acl_fingerprint(hermes_env),
                ),
                before,
            )

    def test_wiki_index_escape_and_cloud_runtime_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._make_layout(Path(temporary))
            config = project / "config" / "sync.local.json"
            data = project / "data"
            wiki = project.parent / "fake-localappdata" / "notion-excel-sync" / "wiki"
            hermes_env = project.parent / "fake-hermes" / ".env"
            before = {
                str(path): self._acl_fingerprint(path)
                for path in (config, data, wiki, hermes_env)
            }

            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["wiki"]["index_db"] = str(project / "data" / "wiki.db")
            config.write_text(json.dumps(payload), encoding="utf-8")
            before[str(config)] = self._acl_fingerprint(config)
            escaped = self._run(project, "-Apply")
            self.assertNotEqual(escaped.returncode, 0)
            self.assertIn("wiki.index_db must be inside", escaped.stderr)
            for path in (config, data, wiki, hermes_env):
                self.assertEqual(self._acl_fingerprint(path), before[str(path)])

            payload["wiki"]["index_db"] = str(wiki / "index.db")
            config.write_text(json.dumps(payload), encoding="utf-8")
            before[str(config)] = self._acl_fingerprint(config)
            cloud = self._run(
                project,
                "-Apply",
                environment_overrides={"OneDrive": str(project.parent / "fake-localappdata")},
            )
            self.assertNotEqual(cloud.returncode, 0)
            self.assertIn("cloud-synced", cloud.stderr)
            for path in (config, data, wiki, hermes_env):
                self.assertEqual(self._acl_fingerprint(path), before[str(path)])

    def test_fake_hermes_env_bytes_are_never_parsed_or_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._make_layout(Path(temporary))
            hermes_env = project.parent / "fake-hermes" / ".env"
            opaque = b"\xff\xfe\x00\x81GATEWAY_RELAY_TOKEN=opaque-secret\x00"
            hermes_env.write_bytes(opaque)

            preview = self._run(project)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(hermes_env.read_bytes(), opaque)
            self.assertNotIn("opaque-secret", preview.stdout + preview.stderr)

            applied = self._run(project, "-Apply")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(hermes_env.read_bytes(), opaque)
            self.assertNotIn("opaque-secret", applied.stdout + applied.stderr)
            report = self._acl_report(hermes_env)
            self.assertEqual(report["Owner"], report["CurrentSid"])
            self.assertTrue(report["Protected"])

    def test_script_contains_verified_transactional_rollback(self) -> None:
        source = self.script.read_text(encoding="utf-8")
        self.assertIn("Restore-AclSnapshots", source)
        self.assertIn("rollback verification also failed", source)
        self.assertIn("GetSecurityDescriptorSddlForm", source)
        self.assertIn("SetSecurityDescriptorSddlForm", source)
        self.assertRegex(
            source,
            r"catch\s*\{[\s\S]*Restore-AclSnapshots[\s\S]*restored and verified",
        )


if __name__ == "__main__":
    unittest.main()
