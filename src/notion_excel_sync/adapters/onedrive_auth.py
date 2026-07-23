from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msal
from msal_extensions import (
    PersistedTokenCache as _PersistedTokenCache,
    build_encrypted_persistence,
)
from msal_extensions.filelock import CrossPlatLock as AtomicFileLock

from notion_excel_sync.config import OneDriveConfig


class OneDriveAuthenticationError(RuntimeError):
    """Raised when a delegated Microsoft Graph token cannot be obtained safely."""


class PersistedTokenCache(_PersistedTokenCache):
    """DPAPI cache pinned to the dependency-free atomic lock implementation.

    Hermes preloads portalocker without processing the pywin32 ``.pth`` file because its
    detached gateway starts from a base interpreter. The upstream cache would therefore select
    a Windows lock whose native DLL search path is incomplete. This override keeps the same
    encrypted persistence format while selecting msal-extensions' built-in exclusive-file lock.
    """

    def modify(
        self,
        credential_type: str,
        old_entry: dict[str, Any] | None,
        new_key_value_pairs: dict[str, Any] | None = None,
    ) -> None:
        with AtomicFileLock(self._lock_location):
            self._reload_if_necessary()
            msal.SerializableTokenCache.modify(
                self,
                credential_type,
                old_entry,
                new_key_value_pairs=new_key_value_pairs,
            )
            self._persistence.save(self.serialize())
            self._last_sync = time.time()


class EnvironmentTokenProvider:
    """Compatibility provider for short-lived tokens used by tests and diagnostics."""

    def __init__(self, environment_name: str) -> None:
        self.environment_name = environment_name

    def __call__(self) -> str:
        token = os.environ.get(self.environment_name, "").strip()
        if not token:
            raise OneDriveAuthenticationError(
                f"Required environment variable is not set: {self.environment_name}"
            )
        return token


class MsalDeviceCodeTokenProvider:
    """Delegated Graph token provider backed by an encrypted, locked MSAL cache."""

    def __init__(
        self,
        *,
        client_id: str,
        cache_path: Path,
        authority: str,
        username: str | None = None,
        scopes: tuple[str, ...] = ("Files.Read",),
    ) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            persistence = build_encrypted_persistence(str(cache_path))
        except Exception as exc:  # pragma: no cover - platform-specific exception classes
            raise OneDriveAuthenticationError(
                "Windows-protected MSAL token cache could not be initialized; "
                "unencrypted token storage is intentionally disabled"
            ) from exc
        if not persistence.is_encrypted:
            raise OneDriveAuthenticationError(
                "MSAL token cache is not encrypted; refusing to store delegated credentials"
            )
        self._cache = PersistedTokenCache(persistence)
        self._app = msal.PublicClientApplication(
            client_id=client_id,
            authority=authority,
            token_cache=self._cache,
        )
        self._username = username.strip() if username else None
        self._scopes = list(scopes)

    @staticmethod
    def _access_token(result: dict[str, Any] | None) -> str:
        if isinstance(result, dict) and result.get("access_token"):
            return str(result["access_token"])
        if not result:
            raise OneDriveAuthenticationError(
                "No cached OneDrive login is available; run the onedrive-login command"
            )
        error = str(result.get("error", "authentication_failed"))
        description = str(result.get("error_description", "")).strip()
        message = f"Microsoft delegated authentication failed: {error}"
        if description:
            message = f"{message}: {description}"
        raise OneDriveAuthenticationError(message)

    def _account(self) -> dict[str, Any]:
        accounts = self._app.get_accounts(username=self._username)
        if not accounts:
            raise OneDriveAuthenticationError(
                "No cached OneDrive login is available; run the onedrive-login command"
            )
        if len(accounts) > 1 and not self._username:
            raise OneDriveAuthenticationError(
                "Multiple Microsoft accounts exist in the token cache; configure "
                "onedrive.username explicitly"
            )
        return accounts[0]

    def __call__(self) -> str:
        result = self._app.acquire_token_silent(
            scopes=self._scopes,
            account=self._account(),
        )
        return self._access_token(result)

    def login(self, display: Callable[[str], None] = print) -> str:
        """Perform the one-time operator device-code login and seed the cache."""

        flow = self._app.initiate_device_flow(scopes=self._scopes)
        if not isinstance(flow, dict) or "user_code" not in flow:
            self._access_token(flow if isinstance(flow, dict) else None)
            raise OneDriveAuthenticationError("Microsoft did not return a device login code")
        message = str(flow.get("message", "")).strip()
        if message:
            display(message)
        return self._access_token(self._app.acquire_token_by_device_flow(flow))


def build_onedrive_token_provider(
    config: OneDriveConfig,
) -> MsalDeviceCodeTokenProvider | EnvironmentTokenProvider:
    if config.client_id:
        return MsalDeviceCodeTokenProvider(
            client_id=config.client_id,
            cache_path=config.token_cache,
            authority=config.authority,
            username=config.username,
        )
    return EnvironmentTokenProvider(config.token_env)
