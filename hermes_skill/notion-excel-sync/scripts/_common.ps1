Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-NesProjectRoot {
    [CmdletBinding()]
    param(
        [string]$ProjectRoot
    )

    $candidate = $ProjectRoot
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = $env:NOTION_EXCEL_SYNC_PROJECT_ROOT
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = Join-Path $PSScriptRoot "..\..\.."
    }

    $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction SilentlyContinue
    if ($null -eq $resolved -or -not (Test-Path -LiteralPath (Join-Path $resolved.Path "pyproject.toml"))) {
        throw "Set NOTION_EXCEL_SYNC_PROJECT_ROOT to the project directory containing pyproject.toml."
    }
    return $resolved.Path
}

function Resolve-NesExistingPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
        throw "$Description not found: $Path"
    }
    return $resolved.Path
}

function Resolve-NesConfigPath {
    [CmdletBinding()]
    param(
        [string]$Config,

        [string]$ProjectRoot
    )

    $root = Resolve-NesProjectRoot -ProjectRoot $ProjectRoot
    $candidate = $Config
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = $env:NOTION_EXCEL_SYNC_CONFIG
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = Join-Path $root "config\sync.local.json"
    }
    elseif (-not [System.IO.Path]::IsPathRooted($candidate)) {
        $fromCurrentDirectory = Resolve-Path -LiteralPath $candidate -ErrorAction SilentlyContinue
        if ($null -eq $fromCurrentDirectory) {
            $candidate = Join-Path $root $candidate
        }
    }

    $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction SilentlyContinue
    if ($null -eq $resolved -or -not (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
        throw "Wiki configuration was not found. Set -Config or NOTION_EXCEL_SYNC_CONFIG."
    }
    return $resolved.Path
}

function Invoke-NesCli {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$CliArguments,

        [string]$ProjectRoot
    )

    $root = Resolve-NesProjectRoot -ProjectRoot $ProjectRoot
    $python = $env:NOTION_EXCEL_SYNC_PYTHON
    if ([string]::IsNullOrWhiteSpace($python)) {
        $projectPython = Join-Path $root ".venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
            $python = $projectPython
        }
        else {
            $python = "python"
        }
    }

    $exitCode = 1
    Push-Location -LiteralPath $root
    try {
        & $python -m notion_excel_sync.cli @CliArguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw "notion-excel-sync exited with code $exitCode."
    }
}
