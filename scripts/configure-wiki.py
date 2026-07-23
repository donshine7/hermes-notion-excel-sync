from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path


INCLUDE_ROOTS = [
    "2. 해외총괄",
    "3. 사무소규정관련",
    "4. 사무소사업관련",
    "4-1. 기타 과제관련(4번 항목 제외 과제)",
    "4-2. 대리인 지원 자료 및 고객 관리",
    "4-3. 절세",
    "5. 사무소특허제도_특허,실시권,이전,감평",
    "5-1. 소송 및 권리행사",
    "6. 사무소상표제도",
    "7. 사무소디자인제도",
    "7-1. 저작권",
    "8. 기술분류",
    "9. 법령",
    "16. 당소 사무소 정보",
]

EXCLUDE_ROOTS = [
    "0-1. 영업 관련 정보",
    "1. 업무분류_국내",
    "1. 업무분류_국내2",
    "1-1. 업무분류_인커밍",
    "1-2. 업무분류_아웃고잉",
    "1-3. 업무분류(사업계획서_제안서)",
    "1-4. 업무분류_과제",
    "11. 팀원실적정산_월간업무보고",
    "12. 파트너_회의록_단톡방자료",
    "20. 상상인력이력정보",
]

_REPARSE_POINT = 0x400


def _normalized_absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(os.fspath(value)))))


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
            raise RuntimeError("The local Wiki runtime contains a reparse point")
        if current.parent == current:
            break
        current = current.parent


def _resolve_runtime_root(config_path: Path) -> Path:
    local_app_data_value = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data_value:
        raise RuntimeError("LOCALAPPDATA is required for the local Wiki runtime")
    local_app_data = _normalized_absolute_path(local_app_data_value)
    if not local_app_data.is_dir():
        raise RuntimeError("LOCALAPPDATA must be an existing local directory")
    runtime_root = local_app_data / "notion-excel-sync" / "wiki"
    project_root = config_path.parent.parent
    if _is_within(runtime_root, project_root) or _is_within(project_root, runtime_root):
        raise RuntimeError("The local Wiki runtime must be outside the project")
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(name, "").strip()
        if value and _is_within(runtime_root, _normalized_absolute_path(value)):
            raise RuntimeError("The local Wiki runtime must not be cloud-synced")
    if any(
        part.casefold() == "onedrive"
        or part.casefold().startswith("onedrive - ")
        for part in runtime_root.parts
    ):
        raise RuntimeError("The local Wiki runtime must not be inside OneDrive")
    _assert_no_reparse_components(runtime_root)
    return runtime_root


def _protect_runtime_tree(runtime_root: Path, operator_sid: str) -> None:
    application_root = runtime_root.parent
    application_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(exist_ok=True)
    _assert_no_reparse_components(runtime_root)

    # Set-Acl is used instead of localized icacls output parsing so the final
    # descriptor can be verified as exactly three explicit FullControl grants.
    script = r"""
param([string]$Root, [string]$OwnerSid)
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
        $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -cne $OwnerSid
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
    Get-ChildItem -LiteralPath $Root -Force -Recurse |
        Sort-Object @{Expression={$_.FullName.Length};Descending=$true}
)
$rootItem = Get-Item -LiteralPath $Root -Force
$items += $rootItem
foreach ($item in $items) {
    if (($item.Attributes -band $reparse) -ne 0) {
        throw 'The local Wiki runtime contains a reparse point.'
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
    $inheritance = if ($isDirectory) {$directoryInheritance} else {$noInheritance}
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
        throw 'The local Wiki runtime ACL verification failed.'
    }
}
"""
    command = "& {\n" + script + "\n}"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
            str(application_root),
            operator_sid,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Cannot protect and verify the local Wiki runtime ACL")
    _assert_no_reparse_components(runtime_root)


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        identity = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        rows = list(csv.reader([identity]))
        if len(rows) != 1 or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
            raise RuntimeError("Cannot determine the Windows operator SID")
        operator_sid = rows[0][1]
        subprocess.run(
            [
                "icacls.exe",
                str(temporary),
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
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atomically add the bounded read-only LLM Wiki configuration."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--root-item-id", required=True)
    parser.add_argument(
        "--root-path",
        default="Desktop/[상상] 업무분류",
    )
    args = parser.parse_args()

    path = Path(args.config).expanduser().resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    drive_id = str(raw.get("onedrive", {}).get("drive_id", "")).strip()
    if not drive_id:
        raise RuntimeError("onedrive.drive_id is missing")
    runtime_root = _resolve_runtime_root(path)
    identity = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    rows = list(csv.reader([identity]))
    if len(rows) != 1 or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeError("Cannot determine the Windows operator SID")
    _protect_runtime_tree(runtime_root, rows[0][1])

    raw["wiki"] = {
        "enabled": True,
        "drive_id": drive_id,
        "root_item_id": args.root_item_id.strip(),
        "root_path": args.root_path.strip().strip("/"),
        "index_db": str(runtime_root / "index.db"),
        "rollout_mode": "shadow",
        "include_roots": INCLUDE_ROOTS,
        "exclude_roots": EXCLUDE_ROOTS,
        "allowed_extensions": [
            ".txt",
            ".md",
            ".csv",
            ".html",
            ".htm",
            ".docx",
            ".pptx",
            ".xlsx",
            ".hwpx",
        ],
        "max_file_bytes": 20 * 1024 * 1024,
        "max_total_bytes": 250 * 1024 * 1024,
        "max_documents": 300,
        "max_output_chars": 2_000_000,
        "max_generation_chars": 30_000_000,
        "max_generation_chunks": 50_000,
        "extractor_timeout_seconds": 30,
        "chunk_chars": 1200,
        "chunk_overlap_chars": 180,
        "retrieval_top_k": 5,
        "policy_version": "sangsang-wiki-policy-v2",
        "selective_roots": [
            "2. 해외총괄",
            "4. 사무소사업관련",
            "4-1. 기타 과제관련(4번 항목 제외 과제)",
            "4-2. 대리인 지원 자료 및 고객 관리",
            "16. 당소 사무소 정보",
        ],
        "selective_keywords": [
            "공고",
            "규정",
            "지침",
            "기준",
            "요령",
            "안내",
            "가이드",
            "매뉴얼",
            "절차",
            "서식",
            "양식",
            "템플릿",
            "faq",
            "사업비",
            "지원사업",
            "바우처",
            "증빙",
            "정산",
            "수행기관",
            "관납료",
            "비용",
        ],
    }
    _write_atomic(path, raw)
    print(
        json.dumps(
            {
                "ok": True,
                "config": str(path),
                "drive_id": drive_id,
                "root_item_id": args.root_item_id.strip(),
                "rollout_mode": "shadow",
                "wiki_runtime_root": str(runtime_root),
                "notion_mutated": False,
                "onedrive_mutated": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
