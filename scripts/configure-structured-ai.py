from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


_REPARSE_POINT = 0x400
_DEFAULTS: Mapping[str, object] = {
    "provider": "hermes",
    "task_name": "web_extract",
    "model": None,
    "cache_db": "%LOCALAPPDATA%\\notion-excel-sync\\ai\\analysis.db",
    "timeout_seconds": 45,
    "max_calls_per_sync": 8,
    "max_mail_calls_per_sync": 4,
    "max_input_chars": 12_000,
    "assist_confidence": 0.8,
    "verified_confidence": 0.95,
}


def _normalized_absolute_path(value: str | Path) -> Path:
    return Path(
        os.path.abspath(
            os.path.expandvars(os.path.expanduser(os.fspath(value)))
        )
    )


def _is_within(path: Path, root: Path, *, allow_root: bool = True) -> bool:
    try:
        common = os.path.commonpath((os.fspath(path), os.fspath(root)))
    except ValueError:
        return False
    if os.path.normcase(common) != os.path.normcase(os.fspath(root)):
        return False
    return allow_root or os.path.normcase(os.fspath(path)) != os.path.normcase(
        os.fspath(root)
    )


def _is_reparse(path: Path) -> bool:
    try:
        stat_result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        int(getattr(stat_result, "st_file_attributes", 0)) & _REPARSE_POINT
    )


def _assert_no_reparse_components(path: Path) -> None:
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and _is_reparse(current):
            raise RuntimeError("A protected configuration path contains a reparse point")
        if current.parent == current:
            break
        current = current.parent


def _operator_sid() -> str:
    identity = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    rows = list(csv.reader([identity]))
    if len(rows) != 1 or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeError("Cannot determine the Windows operator SID")
    return rows[0][1]


def _resolve_ai_runtime(config_path: Path) -> Path:
    local_app_data_value = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data_value:
        raise RuntimeError("LOCALAPPDATA is required for the AI runtime")
    local_app_data = _normalized_absolute_path(local_app_data_value)
    if not local_app_data.is_dir():
        raise RuntimeError("LOCALAPPDATA must be an existing local directory")
    runtime_root = local_app_data / "notion-excel-sync" / "ai"
    project_root = config_path.parent.parent
    if _is_within(runtime_root, project_root) or _is_within(
        project_root, runtime_root
    ):
        raise RuntimeError("The AI runtime must be outside the project")
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(name, "").strip()
        if value and _is_within(runtime_root, _normalized_absolute_path(value)):
            raise RuntimeError("The AI runtime must not be cloud-synced")
    if any(
        part.casefold() == "onedrive"
        or part.casefold().startswith("onedrive - ")
        for part in runtime_root.parts
    ):
        raise RuntimeError("The AI runtime must not be inside OneDrive")
    _assert_no_reparse_components(runtime_root)
    return runtime_root


def _protect_runtime_tree(runtime_root: Path, operator_sid: str) -> None:
    application_root = runtime_root.parent
    application_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(exist_ok=True)
    _assert_no_reparse_components(runtime_root)
    script = r"""
param([string]$ApplicationRoot, [string]$RuntimeRoot, [string]$OwnerSid)
$ErrorActionPreference = 'Stop'
$reparse = [System.IO.FileAttributes]::ReparsePoint
$full = [System.Security.AccessControl.FileSystemRights]::FullControl
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$none = [System.Security.AccessControl.PropagationFlags]::None
$directoryInheritance = (
    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
)
$noInheritance = [System.Security.AccessControl.InheritanceFlags]::None
$sids = @($OwnerSid, 'S-1-5-18', 'S-1-5-32-544')
function Test-ExactAcl([object]$Item) {
    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Item.FullName
    if (
        -not $acl.AreAccessRulesProtected -or
        $acl.GetOwner(
            [System.Security.Principal.SecurityIdentifier]
        ).Value -cne $OwnerSid
    ) {
        return $false
    }
    $rules = @($acl.GetAccessRules(
        $true, $true, [System.Security.Principal.SecurityIdentifier]
    ))
    if ($rules.Count -ne $sids.Count) {
        return $false
    }
    $expectedInheritance = if ([bool]$Item.PSIsContainer) {
        $directoryInheritance
    } else {
        $noInheritance
    }
    foreach ($rule in $rules) {
        if (
            $rule.IdentityReference.Value -notin $sids -or
            $rule.IsInherited -or
            $rule.AccessControlType -ne $allow -or
            [int]$rule.FileSystemRights -ne [int]$full -or
            $rule.InheritanceFlags -ne $expectedInheritance -or
            $rule.PropagationFlags -ne $none
        ) {
            return $false
        }
    }
    return $true
}
$items = @(
    Get-ChildItem -LiteralPath $RuntimeRoot -Force -Recurse |
        Sort-Object @{Expression={$_.FullName.Length};Descending=$true}
)
$items += Get-Item -LiteralPath $RuntimeRoot -Force
$items += Get-Item -LiteralPath $ApplicationRoot -Force
foreach ($item in $items) {
    if (($item.Attributes -band $reparse) -ne 0) {
        throw 'The AI runtime contains a reparse point.'
    }
    if (Test-ExactAcl -Item $item) {
        continue
    }
    $isDirectory = [bool]$item.PSIsContainer
    $acl = if ($isDirectory) {
        [System.Security.AccessControl.DirectorySecurity]::new()
    } else {
        [System.Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetOwner([System.Security.Principal.SecurityIdentifier]::new($OwnerSid))
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = if ($isDirectory) {
        $directoryInheritance
    } else {
        $noInheritance
    }
    foreach ($sidText in $sids) {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $full, $inheritance, $none, $allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Microsoft.PowerShell.Security\Set-Acl -LiteralPath $item.FullName -AclObject $acl
}
foreach ($item in $items) {
    if (-not (Test-ExactAcl -Item $item)) {
        throw 'The AI runtime ACL verification failed.'
    }
}
"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "& {\n" + script + "\n}",
            str(application_root),
            str(runtime_root),
            operator_sid,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Cannot protect and verify the AI runtime ACL")
    _assert_no_reparse_components(runtime_root)


def _protect_file(path: Path) -> None:
    operator_sid = _operator_sid()
    subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{operator_sid}:(F)",
            "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.close(handle)
        _protect_file(temporary)
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def updated_configuration(
    raw: Mapping[str, Any],
    *,
    privacy_mode: str,
    rollout_mode: str,
    external_provider_consent: bool,
) -> dict[str, Any]:
    if privacy_mode == "redacted_remote" and not external_provider_consent:
        raise RuntimeError(
            "redacted_remote requires explicit external provider consent"
        )
    if privacy_mode not in {"local_only", "redacted_remote"}:
        raise RuntimeError("Unsupported AI privacy mode")
    if rollout_mode not in {"shadow", "assist", "verified"}:
        raise RuntimeError("Unsupported AI rollout mode")
    result = dict(raw)
    existing = result.get("ai", {})
    if not isinstance(existing, dict):
        raise RuntimeError("The existing ai configuration must be an object")
    ai = {**_DEFAULTS, **existing}
    ai.update(
        {
            "enabled": True,
            "provider": "hermes",
            "rollout_mode": rollout_mode,
            "privacy_mode": privacy_mode,
        }
    )
    result["ai"] = ai
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enable bounded structured AI with an ACL-protected local cache."
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--privacy-mode",
        choices=("local_only", "redacted_remote"),
        required=True,
    )
    parser.add_argument(
        "--rollout-mode",
        choices=("shadow", "assist", "verified"),
        default="shadow",
    )
    parser.add_argument(
        "--external-provider-consent",
        action="store_true",
        help="Required when privacy-mode is redacted_remote.",
    )
    args = parser.parse_args()

    project_root = _normalized_absolute_path(args.project_root)
    config_path = _normalized_absolute_path(args.config)
    config_root = project_root / "config"
    if not project_root.is_dir() or not (project_root / "pyproject.toml").is_file():
        raise RuntimeError("The project root is invalid")
    if (
        not config_path.is_file()
        or not _is_within(config_path, config_root, allow_root=False)
        or not config_path.name.endswith(".local.json")
    ):
        raise RuntimeError("The local configuration path is invalid")
    _assert_no_reparse_components(config_path)
    if config_path.stat().st_size > 1_000_000:
        raise RuntimeError("The local configuration is unexpectedly large")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("The configuration root must be an object")

    runtime_root = _resolve_ai_runtime(config_path)
    _protect_runtime_tree(runtime_root, _operator_sid())
    updated = updated_configuration(
        raw,
        privacy_mode=args.privacy_mode,
        rollout_mode=args.rollout_mode,
        external_provider_consent=args.external_provider_consent,
    )

    sys.path.insert(0, str(project_root / "src"))
    from notion_excel_sync.config import load_config

    handle, candidate_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.validation.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    candidate = Path(candidate_name)
    try:
        os.close(handle)
        _protect_file(candidate)
        with candidate.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        validated = load_config(candidate)
        if (
            not validated.ai.enabled
            or validated.ai.privacy_mode != args.privacy_mode
            or validated.ai.rollout_mode != args.rollout_mode
        ):
            raise RuntimeError("The candidate AI configuration did not validate")
    finally:
        candidate.unlink(missing_ok=True)

    _write_atomic(config_path, updated)
    print(
        json.dumps(
            {
                "ok": True,
                "ai_enabled": True,
                "provider": "hermes",
                "privacy_mode": args.privacy_mode,
                "rollout_mode": args.rollout_mode,
                "cache_acl_hardened": True,
                "notion_mutated": False,
                "source_mutated": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
