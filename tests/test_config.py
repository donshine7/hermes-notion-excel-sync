from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from notion_excel_sync.config import ConfigError, load_config


def _base_config() -> dict[str, object]:
    return {
        "onedrive": {"drive_id": "drive", "item_id": "item"},
        "excel": {"sheets": ["Cases"]},
        "notion": {
            "read_token_env": "NOTION_READ_TOKEN",
            "write_token_env": "GATEWAY_RELAY_NX_NOTION_TOKEN",
            "databases": {"cases": "data-source"},
        },
        "approval": {
            "secret_env": "GATEWAY_RELAY_NX_APPROVAL_SECRET",
            "allowed_telegram_users": ["123"],
        },
    }


class ConfigSecurityTest(unittest.TestCase):
    def load(self, raw: dict[str, object]):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            config_dir = project / "config"
            config_dir.mkdir(parents=True)
            local_app_data = root / "local"
            local_app_data.mkdir()
            path = config_dir / "sync.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "OneDrive": "",
                "OneDriveConsumer": "",
                "OneDriveCommercial": "",
            }
            with mock.patch.dict("os.environ", environment, clear=False):
                return load_config(path)

    def test_gateway_only_secret_names_are_accepted(self) -> None:
        before = datetime.now().astimezone()
        config = self.load(_base_config())
        after = datetime.now().astimezone()
        self.assertEqual(
            config.notion.write_token_env,
            "GATEWAY_RELAY_NX_NOTION_TOKEN",
        )
        self.assertIsNone(config.notion.schema_parent_page_id)
        self.assertLessEqual(before, config.initial_cutoff)
        self.assertLessEqual(config.initial_cutoff, after)

    def test_schema_parent_page_id_is_loaded_from_local_config(self) -> None:
        raw = _base_config()
        raw["notion"]["schema_parent_page_id"] = (
            "f0000000-0000-0000-0000-000000000001"
        )

        config = self.load(raw)

        self.assertEqual(
            config.notion.schema_parent_page_id,
            "f0000000-0000-0000-0000-000000000001",
        )

    def test_model_visible_write_or_signing_secret_names_are_rejected(self) -> None:
        unsafe_write = _base_config()
        unsafe_write["notion"] = {
            "read_token_env": "NOTION_READ_TOKEN",
            "write_token_env": "NOTION_TOKEN",
        }
        with self.assertRaises(ConfigError):
            self.load(unsafe_write)

        unsafe_approval = _base_config()
        unsafe_approval["approval"] = {
            "secret_env": "SYNC_APPROVAL_SECRET",
            "allowed_telegram_users": ["123"],
        }
        with self.assertRaises(ConfigError):
            self.load(unsafe_approval)

    def test_read_and_write_integrations_cannot_share_an_environment(self) -> None:
        raw = _base_config()
        raw["notion"] = {
            "read_token_env": "GATEWAY_RELAY_NX_NOTION_TOKEN",
            "write_token_env": "GATEWAY_RELAY_NX_NOTION_TOKEN",
        }
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_personal_msal_configuration_is_resolved_safely(self) -> None:
        raw = _base_config()
        raw["onedrive"] = {
            "drive_id": "drive",
            "item_id": "item",
            "client_id": "11111111-2222-4333-8444-555555555555",
            "username": "test.user@example.com",
            "token_cache": "../data/msal-token-cache.bin",
        }
        config = self.load(raw)
        self.assertEqual(
            config.onedrive.client_id,
            "11111111-2222-4333-8444-555555555555",
        )
        self.assertEqual(config.onedrive.username, "test.user@example.com")
        self.assertEqual(
            config.onedrive.authority,
            "https://login.microsoftonline.com/consumers",
        )
        self.assertEqual(config.onedrive.token_cache.name, "msal-token-cache.bin")

    def test_personal_msal_configuration_rejects_unsafe_authority(self) -> None:
        raw = _base_config()
        raw["onedrive"] = {
            "drive_id": "drive",
            "item_id": "item",
            "client_id": "11111111-2222-4333-8444-555555555555",
            "authority": "https://login.microsoftonline.com/common",
        }
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_local_source_does_not_require_onedrive_and_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            config_dir = project / "config"
            config_dir.mkdir(parents=True)
            local_app_data = root / "local"
            local_app_data.mkdir()
            source_root = root / "authoritative"
            source_root.mkdir()
            workbook = source_root / "workbook.xlsx"
            workbook.write_bytes(b"xlsx")
            wiki_output = root / "wiki-output"
            raw = _base_config()
            raw.pop("onedrive")
            raw["source"] = {
                "mode": "local_filesystem",
                "root_dir": str(source_root),
                "excel_path": str(workbook),
                "immutable": True,
                "initial_auto_hydrate": True,
                "steady_state_auto_hydrate": False,
            }
            raw["wiki"] = {
                "source_root": str(source_root),
                "output_root": str(wiki_output),
            }
            path = config_dir / "sync.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "OneDrive": "",
                "OneDriveConsumer": "",
                "OneDriveCommercial": "",
            }

            with mock.patch.dict("os.environ", environment, clear=False):
                config = load_config(path)

            self.assertEqual(config.source.mode, "local_filesystem")
            self.assertEqual(config.source.root_dir, source_root)
            self.assertEqual(config.source.excel_path, workbook)
            self.assertTrue(config.source.immutable)
            self.assertTrue(config.source.initial_auto_hydrate)
            self.assertFalse(config.source.steady_state_auto_hydrate)
            self.assertEqual(config.onedrive.drive_id, "local")
            self.assertTrue(config.onedrive.item_id.startswith("local:"))
            self.assertEqual(config.wiki.output_root, wiki_output)
            self.assertFalse(config.wiki.steady_state_auto_hydrate)

    def test_local_source_rejects_writable_or_overlapping_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            config_dir = project / "config"
            config_dir.mkdir(parents=True)
            local_app_data = root / "local"
            local_app_data.mkdir()
            source_root = root / "authoritative"
            source_root.mkdir()
            workbook = source_root / "workbook.xlsx"
            workbook.write_bytes(b"xlsx")
            raw = _base_config()
            raw.pop("onedrive")
            raw["source"] = {
                "mode": "local_filesystem",
                "root_dir": str(source_root),
                "excel_path": str(workbook),
                "immutable": False,
            }
            path = config_dir / "sync.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "OneDrive": "",
                "OneDriveConsumer": "",
                "OneDriveCommercial": "",
            }

            with mock.patch.dict("os.environ", environment, clear=False):
                with self.assertRaisesRegex(ConfigError, "immutable"):
                    load_config(path)

            raw["source"]["immutable"] = True
            raw["wiki"] = {"output_root": str(source_root / "generated")}
            path.write_text(json.dumps(raw), encoding="utf-8")
            with mock.patch.dict("os.environ", environment, clear=False):
                with self.assertRaisesRegex(ConfigError, "must not overlap"):
                    load_config(path)

    def test_sheet_specific_composite_keys_are_validated(self) -> None:
        raw = _base_config()
        raw["excel"] = {
            "sheets": ["2026", "등록결정"],
            "key_groups": {
                "2026": [
                    ["사건번호", "종류", "작업설명", "받은날짜"],
                    ["사건번호", "종류", "작업설명"],
                    ["사건번호"],
                ],
                "등록결정": [
                    ["사건번호", "등록 마감일"],
                    ["사건번호"],
                    ["의뢰인"],
                ],
            },
        }
        config = self.load(raw)
        self.assertEqual(
            config.excel.key_groups["2026"][0],
            ["사건번호", "종류", "작업설명", "받은날짜"],
        )

        malformed = _base_config()
        malformed["excel"] = {
            "sheets": ["2026"],
            "key_groups": {"typo": [["사건번호"]]},
        }
        with self.assertRaises(ConfigError):
            self.load(malformed)

    def test_wiki_defaults_to_disabled_and_protected_data_path(self) -> None:
        config = self.load(_base_config())
        self.assertFalse(config.wiki.enabled)
        self.assertEqual(config.wiki.rollout_mode, "shadow")
        self.assertEqual(config.wiki.index_db.name, "index.db")
        self.assertEqual(config.wiki.index_db.parent.name, "wiki")
        self.assertIn("notion-excel-sync", config.wiki.index_db.parts)
        self.assertEqual(config.wiki.max_generation_chars, 30_000_000)
        self.assertEqual(config.wiki.max_generation_chunks, 50_000)
        self.assertFalse(config.ai.enabled)
        self.assertEqual(config.ai.rollout_mode, "shadow")
        self.assertEqual(config.ai.privacy_mode, "local_only")
        self.assertEqual(config.ai.cache_db.name, "analysis.db")

    def test_structured_ai_configuration_is_bounded_and_local(self) -> None:
        raw = _base_config()
        raw["ai"] = {
            "enabled": True,
            "provider": "hermes",
            "task_name": "web_extract",
            "rollout_mode": "assist",
            "privacy_mode": "local_only",
            "max_calls_per_sync": 6,
            "max_mail_calls_per_sync": 2,
            "max_input_chars": 5000,
            "assist_confidence": 0.82,
            "verified_confidence": 0.96,
        }

        config = self.load(raw)

        self.assertTrue(config.ai.enabled)
        self.assertEqual(config.ai.rollout_mode, "assist")
        self.assertEqual(config.ai.max_calls_per_sync, 6)
        self.assertEqual(config.ai.max_mail_calls_per_sync, 2)
        self.assertIn("notion-excel-sync", config.ai.cache_db.parts)

        raw["ai"]["max_mail_calls_per_sync"] = 7
        with self.assertRaisesRegex(ConfigError, "max_mail_calls"):
            self.load(raw)

        raw["ai"]["max_mail_calls_per_sync"] = 2
        raw["ai"]["privacy_mode"] = "unrestricted"
        with self.assertRaisesRegex(ConfigError, "privacy_mode"):
            self.load(raw)

    def test_wiki_policy_and_limits_are_validated(self) -> None:
        raw = _base_config()
        raw["wiki"] = {
            "enabled": True,
            "root_item_id": "folder-item",
            "rollout_mode": "inventory",
            "allowed_extensions": ["docx", ".txt"],
            "chunk_chars": 1000,
            "chunk_overlap_chars": 100,
            "max_generation_chars": 12_345,
            "max_generation_chunks": 321,
        }
        config = self.load(raw)
        self.assertTrue(config.wiki.enabled)
        self.assertEqual(config.wiki.root_item_id, "folder-item")
        self.assertEqual(config.wiki.allowed_extensions, (".docx", ".txt"))
        self.assertEqual(config.wiki.max_generation_chars, 12_345)
        self.assertEqual(config.wiki.max_generation_chunks, 321)

        invalid = _base_config()
        invalid["wiki"] = {
            "enabled": True,
            "root_item_id": "folder-item",
            "chunk_chars": 100,
            "chunk_overlap_chars": 100,
        }
        with self.assertRaises(ConfigError):
            self.load(invalid)

        invalid_generation_cap = _base_config()
        invalid_generation_cap["wiki"] = {"max_generation_chunks": 0}
        with self.assertRaises(ConfigError):
            self.load(invalid_generation_cap)

    def test_wiki_index_must_be_absolute_and_inside_fixed_local_runtime(self) -> None:
        relative = _base_config()
        relative["wiki"] = {"index_db": "../data/wiki/index.db"}
        with self.assertRaisesRegex(ConfigError, "absolute path"):
            self.load(relative)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            local_app_data = root / "local"
            project = root / "project"
            local_app_data.mkdir()
            (project / "config").mkdir(parents=True)
            path = project / "config" / "sync.json"
            outside = root / "outside" / "index.db"
            raw = _base_config()
            raw["wiki"] = {"index_db": str(outside)}
            path.write_text(json.dumps(raw), encoding="utf-8")
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "OneDrive": "",
                "OneDriveConsumer": "",
                "OneDriveCommercial": "",
            }
            with mock.patch.dict("os.environ", environment, clear=False):
                with self.assertRaisesRegex(ConfigError, "must be inside"):
                    load_config(path)

            raw["wiki"] = {
                "index_db": "%LOCALAPPDATA%/notion-excel-sync/wiki/custom/index.db"
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            with mock.patch.dict("os.environ", environment, clear=False):
                config = load_config(path)
            self.assertEqual(
                config.wiki.index_db,
                local_app_data
                / "notion-excel-sync"
                / "wiki"
                / "custom"
                / "index.db",
            )

    def test_wiki_runtime_rejects_project_and_cloud_roots(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            (project / "config").mkdir(parents=True)
            path = project / "config" / "sync.json"
            raw = _base_config()
            raw["wiki"] = {"enabled": True, "root_item_id": "folder-item"}
            path.write_text(json.dumps(raw), encoding="utf-8")

            project_environment = {
                "LOCALAPPDATA": str(project),
                "OneDrive": "",
                "OneDriveConsumer": "",
                "OneDriveCommercial": "",
            }
            with mock.patch.dict("os.environ", project_environment, clear=False):
                with self.assertRaisesRegex(ConfigError, "outside the project"):
                    load_config(path)

            cloud = root / "cloud"
            local_app_data = cloud / "local"
            local_app_data.mkdir(parents=True)
            cloud_environment = {
                "LOCALAPPDATA": str(local_app_data),
                "OneDrive": str(cloud),
                "OneDriveConsumer": "",
                "OneDriveCommercial": "",
            }
            with mock.patch.dict("os.environ", cloud_environment, clear=False):
                with self.assertRaisesRegex(ConfigError, "cloud-synced"):
                    load_config(path)

    @unittest.skipUnless(os.name == "nt", "reparse attributes are Windows-only")
    def test_wiki_runtime_rejects_existing_reparse_components(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            local_app_data = root / "local"
            local_app_data.mkdir()
            target = root / "junction-target"
            (target / "wiki").mkdir(parents=True)
            application_link = local_app_data / "notion-excel-sync"
            try:
                application_link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory links unavailable: {exc}")

            project = root / "project"
            (project / "config").mkdir(parents=True)
            path = project / "config" / "sync.json"
            raw = _base_config()
            raw["wiki"] = {"enabled": True, "root_item_id": "folder-item"}
            path.write_text(json.dumps(raw), encoding="utf-8")
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "OneDrive": "",
                "OneDriveConsumer": "",
                "OneDriveCommercial": "",
            }
            with mock.patch.dict("os.environ", environment, clear=False):
                with self.assertRaisesRegex(ConfigError, "reparse point"):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
