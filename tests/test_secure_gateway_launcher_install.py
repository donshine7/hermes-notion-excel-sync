from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Secure launcher installation is Windows-only")
class SecureGatewayLauncherInstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = Path(__file__).resolve().parents[1]
        cls.installer = (
            cls.repository / "scripts" / "install-secure-gateway-launcher.ps1"
        )
        cls.launcher_names = (
            "restart-hermes-gateway-secure.ps1",
            "restart-hermes-gateway-secure.py",
        )
        cls.manifest_name = "secure-launcher-manifest.json"
        cls.plugin_payloads = {
            "plugin.yaml": b"name: notion-excel-sync-attestor\n",
            "__init__.py": b"# protected plugin shim\n",
            "runtime-manifest.json": b'{"format":1,"files":{}}\n',
        }

    @classmethod
    def _create_protected_plugin(cls, hermes_home: Path) -> Path:
        plugin = hermes_home / "plugins" / "notion-excel-sync-attestor"
        plugin.mkdir(parents=True)
        for name, payload in cls.plugin_payloads.items():
            (plugin / name).write_bytes(payload)
        environment = os.environ.copy()
        environment["NX_PLUGIN_ROOT"] = str(plugin)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$root=$env:NX_PLUGIN_ROOT;"
                "$owner=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;"
                "$sids=@($owner.Value,'S-1-5-18','S-1-5-32-544');"
                "function Set-NxExactAcl{param([string]$Path,[bool]$Directory);"
                "$acl=Get-Acl -LiteralPath $Path;"
                "$acl.SetAccessRuleProtection($true,$false);"
                "@($acl.GetAccessRules($true,$true,"
                "[System.Security.Principal.SecurityIdentifier]))|ForEach-Object{"
                "[void]$acl.RemoveAccessRuleSpecific($_)};"
                "$inheritance=if($Directory){"
                "[System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor "
                "[System.Security.AccessControl.InheritanceFlags]::ObjectInherit}else{"
                "[System.Security.AccessControl.InheritanceFlags]::None};"
                "foreach($sidValue in $sids){"
                "$sid=New-Object System.Security.Principal.SecurityIdentifier($sidValue);"
                "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule("
                "$sid,[System.Security.AccessControl.FileSystemRights]::FullControl,"
                "$inheritance,[System.Security.AccessControl.PropagationFlags]::None,"
                "[System.Security.AccessControl.AccessControlType]::Allow);"
                "[void]$acl.AddAccessRule($rule)};"
                "$acl.SetOwner($owner);Set-Acl -LiteralPath $Path -AclObject $acl};"
                "Set-NxExactAcl -Path $root -Directory $true;"
                "@('plugin.yaml','__init__.py','runtime-manifest.json')|ForEach-Object{"
                "$path=Join-Path $root $_;$acl=Get-Acl -LiteralPath $path;"
                "$acl.SetAccessRuleProtection($true,$false);"
                "@($acl.GetAccessRules($true,$true,"
                "[System.Security.Principal.SecurityIdentifier]))|ForEach-Object{"
                "[void]$acl.RemoveAccessRuleSpecific($_)};"
                "$acl.SetAccessRuleProtection($false,$false);"
                "$acl.SetOwner($owner);Set-Acl -LiteralPath $path -AclObject $acl}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return plugin

    def _make_layout(self, root: Path) -> tuple[Path, Path, dict[str, bytes]]:
        project = root / "project with spaces"
        scripts = project / "scripts"
        scripts.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        payloads = {
            self.launcher_names[0]: (
                b"# secure launcher fixture\r\n"
                b"Write-Output 'source-content-canary-must-not-be-printed'\r\n"
            ),
            self.launcher_names[1]: (
                b"# secure worker fixture\n"
                b"print('source-content-canary-must-not-be-printed')\n"
            ),
        }
        for name, payload in payloads.items():
            (scripts / name).write_bytes(payload)
        hermes_home = root / "fake hermes"
        hermes_home.mkdir()
        self._create_protected_plugin(hermes_home)
        return project, hermes_home, payloads

    def _run(
        self,
        project: Path,
        hermes_home: Path,
        *arguments: str,
        installer: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer or self.installer),
            "-ProjectRoot",
            str(project),
            "-HermesHome",
            str(hermes_home),
            *arguments,
        ]
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            env=environment,
        )

    @staticmethod
    def _output_field(result: subprocess.CompletedProcess[str], name: str) -> str:
        match = re.search(rf"(?m)^\s*{re.escape(name)}\s*:\s*(\S+)", result.stdout)
        if match is None:
            raise AssertionError(f"Missing output field {name!r}: {result.stdout!r}")
        return match.group(1)

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
    def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
        return {
            str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
            for path in sorted(root.rglob("*"))
        }

    def _assert_exact_acl(self, path: Path, *, directory: bool) -> None:
        report = self._acl_report(path)
        expected_sids = {
            str(report["CurrentSid"]),
            "S-1-5-18",
            "S-1-5-32-544",
        }
        self.assertEqual(report["Owner"], report["CurrentSid"])
        self.assertTrue(report["Protected"])
        rules = report["Rules"]
        self.assertIsInstance(rules, list)
        self.assertEqual({str(rule["Sid"]) for rule in rules}, expected_sids)
        self.assertEqual(len(rules), len(expected_sids))
        for rule in rules:
            self.assertEqual(rule["Rights"], report["FullControl"])
            self.assertEqual(rule["Inheritance"], 3 if directory else 0)
            self.assertEqual(rule["Propagation"], 0)
            self.assertEqual(rule["Type"], 0)
            self.assertFalse(rule["Inherited"])

    def _assert_inherited_plugin_file_acl(self, path: Path) -> None:
        report = self._acl_report(path)
        expected_sids = {
            str(report["CurrentSid"]),
            "S-1-5-18",
            "S-1-5-32-544",
        }
        self.assertEqual(report["Owner"], report["CurrentSid"])
        self.assertFalse(report["Protected"])
        rules = report["Rules"]
        self.assertIsInstance(rules, list)
        self.assertEqual({str(rule["Sid"]) for rule in rules}, expected_sids)
        self.assertEqual(len(rules), len(expected_sids))
        for rule in rules:
            self.assertEqual(rule["Rights"], report["FullControl"])
            self.assertEqual(rule["Inheritance"], 0)
            self.assertEqual(rule["Propagation"], 0)
            self.assertEqual(rule["Type"], 0)
            self.assertTrue(rule["Inherited"])

    def test_default_and_whatif_are_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home, _ = self._make_layout(Path(temporary))
            before = self._tree_snapshot(Path(temporary))

            default = self._run(project, hermes_home)
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(self._output_field(default, "Mode"), "DryRun")
            self.assertEqual(self._output_field(default, "ChangesRequired"), "1")
            self.assertEqual(self._output_field(default, "ChangesApplied"), "0")
            self.assertEqual(self._output_field(default, "Compliant"), "False")
            self.assertNotIn(
                "source-content-canary-must-not-be-printed",
                default.stdout + default.stderr,
            )
            self.assertEqual(self._tree_snapshot(Path(temporary)), before)

            what_if = self._run(project, hermes_home, "-Apply", "-WhatIf")
            self.assertEqual(what_if.returncode, 0, what_if.stderr)
            self.assertEqual(self._output_field(what_if, "Mode"), "WhatIf")
            self.assertEqual(self._output_field(what_if, "ChangesApplied"), "0")
            self.assertEqual(self._tree_snapshot(Path(temporary)), before)

    def test_apply_installs_fixed_manifest_and_exact_acl_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home, payloads = self._make_layout(Path(temporary))
            install = hermes_home / "secure-gateway-launcher"

            first = self._run(project, hermes_home, "-Apply")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(self._output_field(first, "Mode"), "Apply")
            self.assertEqual(self._output_field(first, "ChangesRequired"), "1")
            self.assertEqual(self._output_field(first, "ChangesApplied"), "1")
            self.assertEqual(self._output_field(first, "Compliant"), "True")
            self.assertEqual(self._output_field(first, "BackupRetained"), "False")

            self.assertEqual(
                {path.name for path in install.iterdir()},
                {*self.launcher_names, self.manifest_name},
            )
            for name, payload in payloads.items():
                self.assertEqual((install / name).read_bytes(), payload)
            plugin = hermes_home / "plugins" / "notion-excel-sync-attestor"
            self._assert_exact_acl(plugin, directory=True)
            for name, payload in self.plugin_payloads.items():
                self.assertEqual((plugin / name).read_bytes(), payload)
                self._assert_inherited_plugin_file_acl(plugin / name)
            manifest = json.loads((install / self.manifest_name).read_text("utf-8"))
            self.assertEqual(
                manifest,
                {
                    "format": 1,
                    "algorithm": "sha256",
                    "files": {
                        name: hashlib.sha256(payload).hexdigest()
                        for name, payload in payloads.items()
                    },
                    "plugin_files": {
                        name: hashlib.sha256(payload).hexdigest()
                        for name, payload in self.plugin_payloads.items()
                    },
                },
            )
            self._assert_exact_acl(install, directory=True)
            for name in (*self.launcher_names, self.manifest_name):
                self._assert_exact_acl(install / name, directory=False)
            self.assertEqual(list(hermes_home.glob(".secure-gateway-launcher.*")), [])

            second = self._run(project, hermes_home, "-Apply")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(self._output_field(second, "ChangesRequired"), "0")
            self.assertEqual(self._output_field(second, "ChangesApplied"), "0")
            self.assertEqual(self._output_field(second, "Compliant"), "True")

    def test_apply_transactionally_replaces_a_tampered_expected_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home, payloads = self._make_layout(Path(temporary))
            install = hermes_home / "secure-gateway-launcher"
            initial = self._run(project, hermes_home, "-Apply")
            self.assertEqual(initial.returncode, 0, initial.stderr)
            (install / self.launcher_names[1]).write_bytes(b"tampered fixture\n")

            replaced = self._run(project, hermes_home, "-Apply")
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertEqual(self._output_field(replaced, "ChangesRequired"), "1")
            self.assertEqual(self._output_field(replaced, "ChangesApplied"), "1")
            self.assertEqual(
                (install / self.launcher_names[1]).read_bytes(),
                payloads[self.launcher_names[1]],
            )
            self.assertEqual(list(hermes_home.glob(".secure-gateway-launcher.*")), [])

    def test_backup_is_restored_and_verified_when_swap_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, hermes_home, _ = self._make_layout(root)
            install = hermes_home / "secure-gateway-launcher"
            initial = self._run(project, hermes_home, "-Apply")
            self.assertEqual(initial.returncode, 0, initial.stderr)
            old_payload = b"previous-known-bundle\n"
            (install / self.launcher_names[1]).write_bytes(old_payload)
            before = {
                path.name: path.read_bytes()
                for path in install.iterdir()
                if path.is_file()
            }

            injected = root / "installer-with-forced-failure.ps1"
            source = self.installer.read_text(encoding="utf-8")
            marker = "            $backupCreated = $true\n"
            self.assertEqual(source.count(marker), 1)
            injected.write_text(
                source.replace(
                    marker,
                    marker + '            throw "Forced test failure after backup."\n',
                ),
                encoding="utf-8",
            )
            failed = self._run(
                project,
                hermes_home,
                "-Apply",
                installer=injected,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("previous bundle was restored and verified", failed.stderr)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in install.iterdir()
                    if path.is_file()
                },
                before,
            )
            self.assertEqual(
                (install / self.launcher_names[1]).read_bytes(),
                old_payload,
            )
            self.assertEqual(list(hermes_home.glob(".secure-gateway-launcher.*")), [])

    def test_unexpected_bundle_entry_fails_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home, _ = self._make_layout(Path(temporary))
            install = hermes_home / "secure-gateway-launcher"
            initial = self._run(project, hermes_home, "-Apply")
            self.assertEqual(initial.returncode, 0, initial.stderr)
            unexpected = install / "unexpected.txt"
            unexpected.write_bytes(b"opaque-unexpected-entry\n")
            before = self._tree_snapshot(install)

            result = self._run(project, hermes_home, "-Apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected entry", result.stderr)
            self.assertNotIn("opaque-unexpected-entry", result.stdout + result.stderr)
            self.assertEqual(self._tree_snapshot(install), before)
            self.assertEqual(list(hermes_home.glob(".secure-gateway-launcher.*")), [])

    def test_reparse_install_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, hermes_home, _ = self._make_layout(root)
            external = root / "external target"
            external.mkdir()
            marker = external / "marker.txt"
            marker.write_bytes(b"outside-marker\n")
            install = hermes_home / "secure-gateway-launcher"
            environment = os.environ.copy()
            environment["NX_JUNCTION_PATH"] = str(install)
            environment["NX_JUNCTION_TARGET"] = str(external)
            junction = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:NX_JUNCTION_PATH "
                    "-Target $env:NX_JUNCTION_TARGET -ErrorAction Stop | Out-Null",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                env=environment,
            )
            if junction.returncode != 0:
                self.skipTest(f"Junction creation unavailable: {junction.stderr}")

            result = self._run(project, hermes_home, "-Apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reparse point", result.stderr)
            self.assertEqual(marker.read_bytes(), b"outside-marker\n")

    def test_non_inherited_plugin_binding_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, hermes_home, _ = self._make_layout(Path(temporary))
            plugin_file = (
                hermes_home
                / "plugins"
                / "notion-excel-sync-attestor"
                / "plugin.yaml"
            )
            before = {
                name: (
                    hermes_home / "plugins" / "notion-excel-sync-attestor" / name
                ).read_bytes()
                for name in self.plugin_payloads
            }
            environment = os.environ.copy()
            environment["NX_PLUGIN_FILE"] = str(plugin_file)
            weakened = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "$a=Get-Acl -LiteralPath $env:NX_PLUGIN_FILE;"
                    "$a.SetAccessRuleProtection($true,$true);"
                    "Set-Acl -LiteralPath $env:NX_PLUGIN_FILE -AclObject $a",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                env=environment,
            )
            self.assertEqual(weakened.returncode, 0, weakened.stderr)

            result = self._run(project, hermes_home, "-Apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("binding file ACL is not protected", result.stderr)
            self.assertFalse((hermes_home / "secure-gateway-launcher").exists())
            self.assertEqual(
                {
                    name: (
                        hermes_home
                        / "plugins"
                        / "notion-excel-sync-attestor"
                        / name
                    ).read_bytes()
                    for name in self.plugin_payloads
                },
                before,
            )

    def test_source_contract_has_no_secret_input_or_recursive_deletion(self) -> None:
        source = self.installer.read_text(encoding="utf-8")
        self.assertIn('"secure-gateway-launcher"', source)
        self.assertIn('"secure-launcher-manifest.json"', source)
        self.assertIn("Get-Sha256Hex", source)
        self.assertIn("Remove-BundleDirectorySafely", source)
        self.assertIn("previous bundle was restored and verified", source)
        self.assertNotIn("GATEWAY_RELAY_NX_NOTION_TOKEN", source)
        self.assertNotIn("GATEWAY_RELAY_NX_APPROVAL_SECRET", source)
        self.assertNotIn("Read-Host", source)
        self.assertNotIn("Get-Clipboard", source)
        self.assertNotRegex(source, r"(?i)Remove-Item[^\n]*-Recurse")

    def test_production_source_install_does_not_capture_environment_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hermes_home = Path(temporary) / "fake-hermes"
            hermes_home.mkdir()
            self._create_protected_plugin(hermes_home)
            token_canary = "secret-notion-token-canary-should-never-be-copied"
            approval_canary = "secret-approval-canary-should-never-be-copied"
            environment = os.environ.copy()
            environment["GATEWAY_RELAY_NX_NOTION_TOKEN"] = token_canary
            environment["GATEWAY_RELAY_NX_APPROVAL_SECRET"] = approval_canary

            result = self._run(
                self.repository,
                hermes_home,
                "-Apply",
                environment=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            combined_output = result.stdout + result.stderr
            self.assertNotIn(token_canary, combined_output)
            self.assertNotIn(approval_canary, combined_output)
            install = hermes_home / "secure-gateway-launcher"
            for path in install.iterdir():
                payload = path.read_bytes()
                self.assertNotIn(token_canary.encode("ascii"), payload)
                self.assertNotIn(approval_canary.encode("ascii"), payload)


if __name__ == "__main__":
    unittest.main()
