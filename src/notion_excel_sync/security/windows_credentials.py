from __future__ import annotations

import os
import re


CREDENTIAL_TARGET_PREFIX = "NotionExcelSync:"
_CRED_TYPE_GENERIC = 1
_ERROR_NOT_FOUND = 1168
_SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class WindowsCredentialError(RuntimeError):
    """Raised when Windows Credential Manager cannot safely return a credential."""


def credential_target(secret_name: str) -> str:
    """Return the stable Generic Credential target for a configured secret name."""

    normalized = secret_name.strip()
    if _SECRET_NAME_RE.fullmatch(normalized) is None:
        raise WindowsCredentialError(
            "Secret names used with Windows Credential Manager must look like "
            "environment variable names"
        )
    return CREDENTIAL_TARGET_PREFIX + normalized


def read_windows_generic_credential(secret_name: str) -> str | None:
    """Read a current-user Generic Credential, or return None when unavailable.

    CredentialBlob is an application-defined byte sequence. The companion
    PowerShell installer writes UTF-16LE without a trailing NUL, so this reader
    deliberately accepts only that representation.
    """

    if os.name != "nt":
        return None

    # Imported lazily so this module remains importable and testable off Windows.
    import ctypes
    from ctypes import wintypes

    class CredentialW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    credential_pointer_type = ctypes.POINTER(CredentialW)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(credential_pointer_type),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None

    target = credential_target(secret_name)
    pointer = credential_pointer_type()
    if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error_code = ctypes.get_last_error()
        if error_code == _ERROR_NOT_FOUND:
            return None
        raise WindowsCredentialError(
            f"CredReadW failed for target {target!r} with Windows error {error_code}"
        )

    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize)
        if size == 0:
            return ""
        if credential.CredentialBlob is None or size % 2:
            raise WindowsCredentialError(
                f"Credential target {target!r} has an invalid UTF-16LE blob"
            )
        raw = ctypes.string_at(credential.CredentialBlob, size)
        try:
            value = raw.decode("utf-16-le", errors="strict")
        except UnicodeDecodeError as exc:
            raise WindowsCredentialError(
                f"Credential target {target!r} is not valid UTF-16LE"
            ) from exc
        if "\x00" in value:
            raise WindowsCredentialError(
                f"Credential target {target!r} contains a NUL character"
            )
        return value
    finally:
        advapi32.CredFree(pointer)
