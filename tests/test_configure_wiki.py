from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Wiki configuration is Windows-only")
class ConfigureWikiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = Path(__file__).resolve().parents[1]
        cls.script = cls.repository / "scripts" / "configure-wiki.py"

    def test_creates_protected_local_runtime_before_config_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            config_dir = project / "config"
            config_dir.mkdir(parents=True)
            local_app_data = root / "local"
            local_app_data.mkdir()
            config_path = config_dir / "sync.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "onedrive": {"drive_id": "drive", "item_id": "excel"},
                        "excel": {"sheets": ["2026"]},
                        "notion": {},
                        "approval": {"allowed_telegram_users": ["1"]},
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_app_data)
            for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
                environment.pop(name, None)
            command = [
                sys.executable,
                str(self.script),
                "--config",
                str(config_path),
                "--root-item-id",
                "root-item",
            ]
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            runtime = local_app_data / "notion-excel-sync" / "wiki"
            self.assertTrue(runtime.is_dir())
            self.assertEqual(payload["wiki"]["index_db"], str(runtime / "index.db"))
            self.assertEqual(payload["wiki"]["max_generation_chars"], 30_000_000)
            self.assertEqual(payload["wiki"]["max_generation_chunks"], 50_000)

            # A second run exercises ACL verification on an existing runtime.
            second = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

    def test_cloud_localappdata_fails_without_replacing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            (project / "config").mkdir(parents=True)
            config_path = project / "config" / "sync.local.json"
            original = json.dumps(
                {"onedrive": {"drive_id": "drive", "item_id": "excel"}}
            )
            config_path.write_text(original, encoding="utf-8")
            cloud = root / "cloud"
            local_app_data = cloud / "local"
            local_app_data.mkdir(parents=True)
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_app_data)
            environment["OneDrive"] = str(cloud)
            environment.pop("OneDriveConsumer", None)
            environment.pop("OneDriveCommercial", None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.script),
                    "--config",
                    str(config_path),
                    "--root-item-id",
                    "root-item",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
