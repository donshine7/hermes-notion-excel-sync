from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SecureGatewayRestartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.worker = cls.project_root / "scripts" / "restart-hermes-gateway-secure.py"
        cls.wrapper = cls.project_root / "scripts" / "restart-hermes-gateway-secure.ps1"

    @staticmethod
    def _write_fake_yaml_package(site_packages: Path) -> None:
        package = site_packages / "yaml"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "import json\n"
            "def safe_load(value):\n"
            "    try:\n"
            "        return json.loads(value)\n"
            "    except json.JSONDecodeError:\n"
            "        if 'name: notion-excel-sync-attestor' in value:\n"
            "            return {\n"
            "                'name': 'notion-excel-sync-attestor',\n"
            "                'provides_hooks': ['pre_gateway_dispatch'],\n"
            "            }\n"
            "        raise\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_fake_gateway_package(runtime: Path) -> None:
        package = runtime / "hermes_cli"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "gateway_windows.py").write_text(
            "import os\n"
            "WRITE = 'GATEWAY_RELAY_NX_NOTION_TOKEN'\n"
            "APPROVAL = 'GATEWAY_RELAY_NX_APPROVAL_SECRET'\n"
            "def stop():\n"
            "    assert WRITE not in os.environ\n"
            "    assert APPROVAL not in os.environ\n"
            "def _wait_for_gateway_absent(timeout_s):\n"
            "    return True\n"
            "def _spawn_detached():\n"
            "    assert len(os.environ[WRITE]) >= 20\n"
            "    assert len(os.environ[APPROVAL]) >= 48\n"
            "    return 4242\n"
            "def _wait_for_gateway_ready(timeout_s):\n"
            "    return [4242]\n",
            encoding="utf-8",
        )

    def _make_layout(self, root: Path) -> tuple[Path, Path, Path, Path]:
        home = root / "hermes"
        runtime = home / "hermes-agent"
        site_packages = runtime / "venv" / "Lib" / "site-packages"
        site_packages.mkdir(parents=True)
        self._write_fake_yaml_package(site_packages)
        (runtime / "venv" / "pyvenv.cfg").write_text(
            "home = C:\\trusted-uv-python\n"
            "implementation = CPython\n"
            "uv = 0.11.28\n"
            "version_info = 3.11\n",
            encoding="utf-8",
        )

        project = root / "project"
        config = project / "config" / "sync.local.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "excel": {"sheets": ["2026", "등록결정"], "header_row": 1},
                    "notion": {
                        "read_token_env": "NOTION_READ_TOKEN",
                        "write_token_env": "GATEWAY_RELAY_NX_NOTION_TOKEN",
                    },
                    "approval": {
                        "secret_env": "GATEWAY_RELAY_NX_APPROVAL_SECRET"
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        plugin = home / "plugins" / "notion-excel-sync-attestor"
        gateway = (
            plugin
            / "runtime"
            / "notion_excel_sync"
            / "security"
            / "hermes_gateway.py"
        )
        gateway.parent.mkdir(parents=True)
        gateway.write_text("# trusted gateway fixture\n", encoding="utf-8")
        trusted_plugin = (
            self.project_root
            / ".hermes"
            / "plugins"
            / "notion-excel-sync-attestor"
        )
        plugin_payload = (trusted_plugin / "plugin.yaml").read_bytes()
        shim_payload = (trusted_plugin / "__init__.py").read_bytes()
        (plugin / "plugin.yaml").write_bytes(plugin_payload)
        (plugin / "__init__.py").write_bytes(shim_payload)
        (plugin / "runtime-manifest.json").write_text(
            json.dumps(
                {
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
                    "files": {
                        "notion_excel_sync/security/hermes_gateway.py": hashlib.sha256(
                            gateway.read_bytes()
                        ).hexdigest()
                    },
                }
            ),
            encoding="utf-8",
        )
        (home / "config.yaml").write_text(
            json.dumps(
                {
                    "plugins": {
                        "enabled": ["notion-excel-sync-attestor"],
                        "disabled": [],
                        "entries": {
                            "notion-excel-sync-attestor": {
                                "allow_tool_override": False
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        (home / ".env").write_text(
            "NOTION_EXCEL_SYNC_PROJECT_ROOT=non-secret\n",
            encoding="utf-8",
        )
        return home, runtime, project, config

    def _run_worker(
        self,
        home: Path,
        runtime: Path,
        project: Path,
        config: Path,
        *,
        mode: str = "preflight",
        input_value: str = "",
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("GATEWAY_RELAY_NX_NOTION_TOKEN", None)
        environment.pop("GATEWAY_RELAY_NX_APPROVAL_SECRET", None)
        environment.update(
            {
                "NX_SECURE_GATEWAY_MODE": mode,
                "NX_SECURE_GATEWAY_HERMES_HOME": str(home),
                "NX_SECURE_GATEWAY_RUNTIME": str(runtime),
                "NX_SECURE_GATEWAY_PROJECT_ROOT": str(project),
                "NX_SECURE_GATEWAY_CONFIG": str(config),
            }
        )
        return subprocess.run(
            [sys.executable, "-I", "-S", str(self.worker)],
            input=input_value,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
            env=environment,
        )

    def test_preflight_is_read_only_and_does_not_consume_or_echo_stdin(self) -> None:
        canary = "secret-pipe-canary-that-must-not-appear"
        with tempfile.TemporaryDirectory() as temporary:
            home, runtime, project, config = self._make_layout(Path(temporary))
            before = {
                path: path.read_bytes()
                for path in (config, home / "config.yaml", home / ".env")
            }
            result = self._run_worker(
                home,
                runtime,
                project,
                config,
                input_value=canary + "\n",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "STATUS=PREFLIGHT_OK\n")
            self.assertNotIn(canary, result.stdout + result.stderr)
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in before},
            )

    def test_preflight_rejects_manifest_tamper_without_disclosing_input(self) -> None:
        canary = "private-input-canary"
        with tempfile.TemporaryDirectory() as temporary:
            home, runtime, project, config = self._make_layout(Path(temporary))
            gateway = (
                home
                / "plugins"
                / "notion-excel-sync-attestor"
                / "runtime"
                / "notion_excel_sync"
                / "security"
                / "hermes_gateway.py"
            )
            gateway.write_text("# tampered\n", encoding="utf-8")
            result = self._run_worker(
                home,
                runtime,
                project,
                config,
                input_value=canary + "\n",
            )
            self.assertEqual(result.returncode, 10)
            self.assertEqual(result.stdout, "STATUS=PREFLIGHT_REJECTED\n")
            self.assertNotIn(canary, result.stdout + result.stderr)

    def test_preflight_rejects_runtime_file_not_listed_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, runtime, project, config = self._make_layout(Path(temporary))
            extra = (
                home
                / "plugins"
                / "notion-excel-sync-attestor"
                / "runtime"
                / "unlisted.py"
            )
            extra.write_text("# not in manifest\n", encoding="utf-8")
            result = self._run_worker(home, runtime, project, config)
            self.assertEqual(result.returncode, 10)
            self.assertEqual(result.stdout, "STATUS=PREFLIGHT_REJECTED\n")

    def test_preflight_rejects_gateway_secrets_persisted_in_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, runtime, project, config = self._make_layout(Path(temporary))
            (home / ".env").write_text(
                "GATEWAY_RELAY_NX_NOTION_TOKEN=must-not-be-stored\n",
                encoding="utf-8",
            )
            result = self._run_worker(home, runtime, project, config)
            self.assertEqual(result.returncode, 10)
            self.assertEqual(result.stdout, "STATUS=PREFLIGHT_REJECTED\n")

    def test_invalid_pipe_token_fails_before_any_gateway_import_or_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, runtime, project, config = self._make_layout(Path(temporary))
            # The fixture deliberately has no hermes_cli package. A short input
            # must therefore fail at input validation, before gateway import.
            result = self._run_worker(
                home,
                runtime,
                project,
                config,
                mode="restart",
                input_value="too-short\n",
            )
            self.assertEqual(result.returncode, 11)
            self.assertEqual(result.stdout, "STATUS=INPUT_REJECTED\n")

    def test_restart_emits_commit_boundary_before_fixed_ready_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, runtime, project, config = self._make_layout(Path(temporary))
            self._write_fake_gateway_package(runtime)
            result = self._run_worker(
                home,
                runtime,
                project,
                config,
                mode="restart",
                input_value="secret-token-fixture-value-123456\nCOMMIT\n",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout,
                "STATUS=PREPARED\nSTATUS=READY\nPID=4242\n",
            )

    def test_missing_commit_rejects_before_gateway_import_or_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, runtime, project, config = self._make_layout(Path(temporary))
            # No hermes_cli fixture exists. If the worker attempted a stop before
            # receiving COMMIT this would fail through the restart path instead.
            result = self._run_worker(
                home,
                runtime,
                project,
                config,
                mode="restart",
                input_value="secret-token-fixture-value-123456\n",
            )
            self.assertEqual(result.returncode, 13)
            self.assertEqual(
                result.stdout,
                "STATUS=PREPARED\nSTATUS=COMMIT_REJECTED\n",
            )

    def test_source_contract_keeps_secrets_off_args_files_and_output(self) -> None:
        wrapper = self.wrapper.read_text(encoding="utf-8")
        worker = self.worker.read_text(encoding="utf-8")

        self.assertIn("Read-Host \"Notion 쓰기 통합 토큰\" -AsSecureString", wrapper)
        self.assertIn("SecureStringToBSTR", wrapper)
        self.assertIn("ZeroFreeBSTR", wrapper)
        self.assertIn("FILE_TYPE_PIPE", wrapper)
        self.assertIn("Assert-SensitiveFileAclContract", wrapper)
        self.assertIn("protected Hermes configuration file", wrapper)
        self.assertIn("Assert-LauncherBundleContract", wrapper)
        self.assertIn("Assert-PluginChildAclContract", wrapper)
        self.assertIn("secure-gateway-launcher", wrapper)
        self.assertIn("secure-launcher-manifest.json", wrapper)
        self.assertIn("$manifest.plugin_files", wrapper)
        self.assertIn("Microsoft.PowerShell.Utility\\Get-FileHash", wrapper)
        self.assertIn('Description "The sync configuration file"', wrapper)
        self.assertIn("RedirectStandardInput = $true", wrapper)
        self.assertIn("venv\\pyvenv.cfg", wrapper)
        self.assertIn("uv\\python", wrapper)
        self.assertIn("ReceiptContinuity      : RotatedOnRestart", wrapper)
        self.assertIn("cannot continue a previously signed but unused approval", wrapper)
        self.assertIn('$preparedLine -cne "STATUS=PREPARED"', wrapper)
        self.assertIn("$process.StandardInput.Flush()", wrapper)
        self.assertIn('$process.StandardInput.Write("COMMIT`n")', wrapper)
        self.assertIn("-not $committed -and -not $process.HasExited", wrapper)
        self.assertIn("$process.WaitForExit()", wrapper)
        self.assertNotRegex(wrapper, r"(?im)^\s*\[string\]\$(?:Write)?Token\b")
        self.assertNotIn("CredWrite", wrapper)
        self.assertNotIn("cmdkey", wrapper.lower())
        self.assertNotIn("Set-Content", wrapper)
        self.assertNotIn("Add-Content", wrapper)

        self.assertIn("secrets.token_bytes(48)", worker)
        self.assertIn('print("STATUS=PREPARED", flush=True)', worker)
        self.assertIn("_read_commit_from_pipe()", worker)
        self.assertIn("EXPECTED_PLUGIN_FILES", worker)
        self.assertNotIn('project / ".hermes"', worker)
        self.assertIn("gateway_windows.stop()", worker)
        self.assertIn("gateway_windows._spawn_detached()", worker)
        self.assertIn("os.environ.pop(WRITE_TOKEN_ENV, None)", worker)
        self.assertIn("os.environ.pop(APPROVAL_SECRET_ENV, None)", worker)
        self.assertNotIn("argparse", worker)
        self.assertNotIn("write_text(", worker)
        self.assertNotIn("write_bytes(", worker)

    def test_pinned_plugin_hashes_match_the_lf_repository_files(self) -> None:
        tree = ast.parse(self.worker.read_text(encoding="utf-8"))
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "EXPECTED_PLUGIN_FILES"
                for target in node.targets
            )
        )
        expected = ast.literal_eval(assignment.value)
        plugin_root = (
            self.project_root
            / ".hermes"
            / "plugins"
            / "notion-excel-sync-attestor"
        )
        observed = {
            name: hashlib.sha256((plugin_root / name).read_bytes()).hexdigest()
            for name in expected
        }
        self.assertEqual(observed, expected)

    def test_operations_docs_invoke_only_the_protected_launcher_copy(self) -> None:
        for relative in ("README.md", "docs/operations.md", "docs/configuration.md"):
            text = (self.project_root / relative).read_text(encoding="utf-8")
            self.assertNotIn(r".\scripts\restart-hermes-gateway-secure.ps1", text)
            self.assertIn(r"hermes\secure-gateway-launcher", text)


if __name__ == "__main__":
    unittest.main()
