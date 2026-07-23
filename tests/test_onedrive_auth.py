from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notion_excel_sync.adapters.onedrive_auth import (
    EnvironmentTokenProvider,
    MsalDeviceCodeTokenProvider,
    OneDriveAuthenticationError,
    PersistedTokenCache,
)


class OneDriveAuthenticationTest(unittest.TestCase):
    @patch("notion_excel_sync.adapters.onedrive_auth.msal.SerializableTokenCache.modify")
    @patch("notion_excel_sync.adapters.onedrive_auth.AtomicFileLock")
    def test_persisted_cache_uses_atomic_file_lock_instead_of_portalocker(
        self,
        lock_factory: MagicMock,
        base_modify: MagicMock,
    ) -> None:
        persistence = MagicMock()
        persistence.is_encrypted = True
        with tempfile.TemporaryDirectory() as folder:
            cache_path = str(Path(folder) / "token.bin")
            persistence.get_location.return_value = cache_path
            cache = PersistedTokenCache(persistence)

            with patch.object(cache, "_reload_if_necessary") as reload_cache:
                cache.modify("Account", None, {"key": "value"})

        lock_factory.assert_called_once_with(cache_path + ".lockfile")
        reload_cache.assert_called_once_with()
        base_modify.assert_called_once_with(
            cache,
            "Account",
            None,
            new_key_value_pairs={"key": "value"},
        )
        persistence.save.assert_called_once_with(cache.serialize())

    def test_environment_provider_is_runtime_compatibility_only(self) -> None:
        with patch.dict(os.environ, {"TEST_GRAPH_TOKEN": " token "}):
            self.assertEqual(EnvironmentTokenProvider("TEST_GRAPH_TOKEN")(), "token")

    @patch("notion_excel_sync.adapters.onedrive_auth.msal.PublicClientApplication")
    @patch("notion_excel_sync.adapters.onedrive_auth.PersistedTokenCache")
    @patch("notion_excel_sync.adapters.onedrive_auth.build_encrypted_persistence")
    def test_silent_provider_uses_single_cached_account(
        self,
        persistence_factory: MagicMock,
        cache_factory: MagicMock,
        application_factory: MagicMock,
    ) -> None:
        persistence_factory.return_value.is_encrypted = True
        application = application_factory.return_value
        account = {"username": "test.user@example.com"}
        application.get_accounts.return_value = [account]
        application.acquire_token_silent.return_value = {"access_token": "access"}
        with tempfile.TemporaryDirectory() as folder:
            provider = MsalDeviceCodeTokenProvider(
                client_id="11111111-2222-4333-8444-555555555555",
                cache_path=Path(folder) / "cache.bin",
                authority="https://login.microsoftonline.com/consumers",
                username="test.user@example.com",
            )
            self.assertEqual(provider(), "access")
        cache_factory.assert_called_once_with(persistence_factory.return_value)
        application.get_accounts.assert_called_once_with(username="test.user@example.com")
        application.acquire_token_silent.assert_called_once_with(
            scopes=["Files.Read"],
            account=account,
        )

    @patch("notion_excel_sync.adapters.onedrive_auth.msal.PublicClientApplication")
    @patch("notion_excel_sync.adapters.onedrive_auth.PersistedTokenCache")
    @patch("notion_excel_sync.adapters.onedrive_auth.build_encrypted_persistence")
    def test_gateway_path_never_starts_interactive_login(
        self,
        persistence_factory: MagicMock,
        _cache_factory: MagicMock,
        application_factory: MagicMock,
    ) -> None:
        persistence_factory.return_value.is_encrypted = True
        application_factory.return_value.get_accounts.return_value = []
        with tempfile.TemporaryDirectory() as folder:
            provider = MsalDeviceCodeTokenProvider(
                client_id="11111111-2222-4333-8444-555555555555",
                cache_path=Path(folder) / "cache.bin",
                authority="https://login.microsoftonline.com/consumers",
            )
            with self.assertRaises(OneDriveAuthenticationError):
                provider()
        application_factory.return_value.initiate_device_flow.assert_not_called()

    @patch("notion_excel_sync.adapters.onedrive_auth.msal.PublicClientApplication")
    @patch("notion_excel_sync.adapters.onedrive_auth.PersistedTokenCache")
    @patch("notion_excel_sync.adapters.onedrive_auth.build_encrypted_persistence")
    def test_login_displays_device_message_and_seeds_cache(
        self,
        persistence_factory: MagicMock,
        _cache_factory: MagicMock,
        application_factory: MagicMock,
    ) -> None:
        persistence_factory.return_value.is_encrypted = True
        application = application_factory.return_value
        flow = {"user_code": "ABCD", "message": "Open the login page"}
        application.initiate_device_flow.return_value = flow
        application.acquire_token_by_device_flow.return_value = {
            "access_token": "access"
        }
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as folder:
            provider = MsalDeviceCodeTokenProvider(
                client_id="11111111-2222-4333-8444-555555555555",
                cache_path=Path(folder) / "cache.bin",
                authority="https://login.microsoftonline.com/consumers",
            )
            self.assertEqual(provider.login(messages.append), "access")
        self.assertEqual(messages, ["Open the login page"])
        application.acquire_token_by_device_flow.assert_called_once_with(flow)


if __name__ == "__main__":
    unittest.main()
