[CmdletBinding()]
param(
    [string]$ProjectRoot = "",

    [string]$ConfigPath = "",

    [string]$HermesHome = $(
        if ($env:HERMES_HOME) {
            $env:HERMES_HOME
        }
        else {
            Join-Path $env:LOCALAPPDATA "hermes"
        }
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$targetKeys = @(
    "NOTION_EXCEL_SYNC_PROJECT_ROOT",
    "NOTION_EXCEL_SYNC_CONFIG"
)

function Resolve-ExistingDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Required directory does not exist."
    }
    return (Get-Item -LiteralPath $Path -Force).FullName
}

function Resolve-ExistingFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file does not exist."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to update a reparse-point file."
    }
    return $item.FullName
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath($Path)
    $prefix = $rootPath + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith(
            $prefix,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "The sync configuration must be inside the project directory."
    }
}

function Get-TextLines {
    param([Parameter(Mandatory = $true)][string]$Text)

    $lines = New-Object 'System.Collections.Generic.List[object]'
    $start = 0
    $index = 0
    while ($index -lt $Text.Length) {
        $character = $Text[$index]
        if ($character -eq "`r" -or $character -eq "`n") {
            $endingLength = 1
            if (
                $character -eq "`r" -and
                ($index + 1) -lt $Text.Length -and
                $Text[$index + 1] -eq "`n"
            ) {
                $endingLength = 2
            }
            $lines.Add([pscustomobject]@{
                    Content = $Text.Substring($start, $index - $start)
                    Ending = $Text.Substring($index, $endingLength)
                })
            $index += $endingLength
            $start = $index
            continue
        }
        $index += 1
    }
    if ($start -lt $Text.Length) {
        $lines.Add([pscustomobject]@{
                Content = $Text.Substring($start)
                Ending = ""
            })
    }
    return $lines.ToArray()
}

function Assert-SingleLineAssignment {
    param([Parameter(Mandatory = $true)][string]$RawValue)

    $value = $RawValue.Trim()
    if (-not $value) {
        return
    }
    $quote = $value[0]
    if ($quote -ne "'" -and $quote -ne '"') {
        return
    }

    $escaped = $false
    $closingIndex = -1
    for ($index = 1; $index -lt $value.Length; $index += 1) {
        $character = $value[$index]
        if ($escaped) {
            $escaped = $false
            continue
        }
        if ($character -eq '\') {
            $escaped = $true
            continue
        }
        if ($character -eq $quote) {
            $closingIndex = $index
            break
        }
    }
    if ($closingIndex -lt 0) {
        throw "Refusing to rewrite a multiline or unclosed target assignment."
    }
    $remainder = $value.Substring($closingIndex + 1).Trim()
    if ($remainder -and -not $remainder.StartsWith("#")) {
        throw "Refusing to rewrite an ambiguous target assignment."
    }
}

function ConvertTo-DotEnvValue {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "Environment setting paths must be single-line values."
    }
    if ($Value.Contains('${')) {
        throw "Environment setting paths must not contain dotenv interpolation."
    }
    $needsQuotes = (
        $Value.Contains("#") -or
        $Value.Contains('"') -or
        $Value.Contains("'") -or
        $Value -ne $Value.Trim()
    )
    if (-not $needsQuotes) {
        return $Value
    }
    $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [Convert]::ToBase64String($algorithm.ComputeHash($Bytes))
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-AclFingerprint {
    param(
        [Parameter(Mandatory = $true)]
        [Security.AccessControl.FileSecurity]$Acl
    )

    $owner = $Acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $group = $Acl.GetGroup([Security.Principal.SecurityIdentifier]).Value
    $rules = New-Object 'System.Collections.Generic.List[string]'
    foreach ($rule in $Acl.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        )) {
        $rules.Add((
                $rule.IdentityReference.Value + "|" +
                ([int64]$rule.FileSystemRights).ToString() + "|" +
                $rule.AccessControlType.ToString() + "|" +
                $rule.InheritanceFlags.ToString() + "|" +
                $rule.PropagationFlags.ToString() + "|" +
                $rule.IsInherited.ToString()
            ))
    }
    return (
        "Owner=" + $owner +
        "|Group=" + $group +
        "|Protected=" + $Acl.AreAccessRulesProtected.ToString() +
        "|Rules=" + ($rules.ToArray() -join ";")
    )
}

if (-not $ProjectRoot) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) {
        throw "Cannot determine the script path; pass -ProjectRoot explicitly."
    }
    $scriptDirectory = Split-Path -Parent $scriptPath
    $ProjectRoot = Split-Path -Parent $scriptDirectory
}

$ProjectRoot = Resolve-ExistingDirectory -Path $ProjectRoot
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ProjectRoot "config\sync.local.json"
}
$ConfigPath = Resolve-ExistingFile -Path $ConfigPath
Assert-ChildPath -Root $ProjectRoot -Path $ConfigPath

$HermesHome = Resolve-ExistingDirectory -Path $HermesHome
$envPath = Join-Path $HermesHome ".env"
$envPath = Resolve-ExistingFile -Path $envPath
$originalAcl = Get-Acl -LiteralPath $envPath
$originalAclFingerprint = Get-AclFingerprint -Acl $originalAcl
$originalBytes = [IO.File]::ReadAllBytes($envPath)
$originalHash = Get-Sha256 -Bytes $originalBytes

$hasBom = (
    $originalBytes.Length -ge 3 -and
    $originalBytes[0] -eq 0xEF -and
    $originalBytes[1] -eq 0xBB -and
    $originalBytes[2] -eq 0xBF
)
$offset = 0
if ($hasBom) {
    $offset = 3
}
$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
try {
    $text = $strictUtf8.GetString(
        $originalBytes,
        $offset,
        $originalBytes.Length - $offset
    )
}
catch [Text.DecoderFallbackException] {
    throw "Hermes .env must be valid UTF-8; no changes were made."
}
if ($text.Contains([char]0)) {
    throw "Hermes .env contains unsupported NUL data; no changes were made."
}

$lines = @(Get-TextLines -Text $text)
$newline = [Environment]::NewLine
foreach ($line in $lines) {
    if ($line.Ending) {
        $newline = $line.Ending
        break
    }
}

$assignmentPattern = (
    '^[\t ]*(?:export[\t ]+)?' +
    "(?<key>'(?:NOTION_EXCEL_SYNC_PROJECT_ROOT|NOTION_EXCEL_SYNC_CONFIG)'|" +
    '(?:NOTION_EXCEL_SYNC_PROJECT_ROOT|NOTION_EXCEL_SYNC_CONFIG))' +
    '[\t ]*=(?<value>.*)$'
)
$targetMentionPattern = (
    '(?<![A-Za-z0-9_])' +
    "(?:'(?:NOTION_EXCEL_SYNC_PROJECT_ROOT|NOTION_EXCEL_SYNC_CONFIG)'|" +
    '(?:NOTION_EXCEL_SYNC_PROJECT_ROOT|NOTION_EXCEL_SYNC_CONFIG))' +
    '[\t ]*='
)
$regexOptions = [Text.RegularExpressions.RegexOptions]::IgnoreCase

$retained = New-Object 'System.Collections.Generic.List[object]'
foreach ($line in $lines) {
    $lineText = [string]$line.Content
    $trimmedStart = $lineText.TrimStart()
    if ($trimmedStart.StartsWith("#")) {
        $retained.Add($line)
        continue
    }
    $match = [regex]::Match($lineText, $assignmentPattern, $regexOptions)
    if ($match.Success) {
        Assert-SingleLineAssignment -RawValue $match.Groups['value'].Value
        continue
    }
    if ([regex]::IsMatch($lineText, $targetMentionPattern, $regexOptions)) {
        throw "Refusing to rewrite a combined or malformed target assignment."
    }
    $retained.Add($line)
}

$builder = New-Object Text.StringBuilder
foreach ($line in $retained) {
    [void]$builder.Append($line.Content)
    [void]$builder.Append($line.Ending)
}
if ($builder.Length -gt 0) {
    $lastCharacter = $builder[$builder.Length - 1]
    if ($lastCharacter -ne "`r" -and $lastCharacter -ne "`n") {
        [void]$builder.Append($newline)
    }
}

$settings = [ordered]@{
    NOTION_EXCEL_SYNC_PROJECT_ROOT = $ProjectRoot
    NOTION_EXCEL_SYNC_CONFIG = $ConfigPath
}
foreach ($key in $targetKeys) {
    $serialized = ConvertTo-DotEnvValue -Value $settings[$key]
    [void]$builder.Append($key)
    [void]$builder.Append("=")
    [void]$builder.Append($serialized)
    [void]$builder.Append($newline)
}

$payload = $strictUtf8.GetBytes($builder.ToString())
if ($hasBom) {
    $withBom = New-Object byte[] ($payload.Length + 3)
    $withBom[0] = 0xEF
    $withBom[1] = 0xBB
    $withBom[2] = 0xBF
    [Array]::Copy($payload, 0, $withBom, 3, $payload.Length)
    $payload = $withBom
}

if ((Get-Sha256 -Bytes $payload) -eq $originalHash) {
    Write-Output "HermesEnvironmentConfigured : True"
    Write-Output "SettingsChanged            : False"
    Write-Output "SettingsPresent            : 2"
    Write-Output "RestartRequired            : True"
    return
}

$temporaryPath = Join-Path (
    Split-Path -Parent $envPath
) (".env.notion-excel-sync.{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
$backupPath = Join-Path (
    Split-Path -Parent $envPath
) (".env.notion-excel-sync.{0}.bak" -f [Guid]::NewGuid().ToString("N"))
$rollbackDiscardPath = Join-Path (
    Split-Path -Parent $envPath
) (".env.notion-excel-sync.{0}.rollback" -f [Guid]::NewGuid().ToString("N"))
$stream = $null
$replacementApplied = $false
$replacementVerified = $false
try {
    $stream = New-Object IO.FileStream(
        $temporaryPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    $stream.Dispose()
    $stream = $null

    # The temporary file is still empty here. Apply the protected ACL before
    # any existing .env bytes (which can contain secrets) are written to it.
    Set-Acl -LiteralPath $temporaryPath -AclObject $originalAcl
    $temporaryAcl = Get-Acl -LiteralPath $temporaryPath
    if ((Get-AclFingerprint -Acl $temporaryAcl) -ne $originalAclFingerprint) {
        throw "The protected Hermes .env ACL could not be applied to staging."
    }

    $stream = New-Object IO.FileStream(
        $temporaryPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    $stream.SetLength(0)
    $stream.Write($payload, 0, $payload.Length)
    $stream.Flush($true)
    $stream.Dispose()
    $stream = $null

    $currentBytes = [IO.File]::ReadAllBytes($envPath)
    if ((Get-Sha256 -Bytes $currentBytes) -ne $originalHash) {
        throw "Hermes .env changed concurrently; no update was applied."
    }
    [IO.File]::Replace($temporaryPath, $envPath, $backupPath, $false)
    $replacementApplied = $true

    # File.Replace creates the rollback copy from the original destination.
    # Confirm it retained the same protection before doing post-write checks.
    $backupAcl = Get-Acl -LiteralPath $backupPath
    if ((Get-AclFingerprint -Acl $backupAcl) -ne $originalAclFingerprint) {
        Set-Acl -LiteralPath $backupPath -AclObject $originalAcl
        $backupAcl = Get-Acl -LiteralPath $backupPath
        if ((Get-AclFingerprint -Acl $backupAcl) -ne $originalAclFingerprint) {
            throw "The protected rollback copy ACL could not be verified."
        }
    }

    $verifiedBytes = [IO.File]::ReadAllBytes($envPath)
    if ((Get-Sha256 -Bytes $verifiedBytes) -ne (Get-Sha256 -Bytes $payload)) {
        throw "Hermes .env content verification failed after the atomic update."
    }
    $verifiedAcl = Get-Acl -LiteralPath $envPath
    if ((Get-AclFingerprint -Acl $verifiedAcl) -ne $originalAclFingerprint) {
        throw "Hermes .env ACL verification failed after the atomic update."
    }
    $replacementVerified = $true
}
catch {
    if ($replacementApplied -and (Test-Path -LiteralPath $backupPath)) {
        try {
            [IO.File]::Replace(
                $backupPath,
                $envPath,
                $rollbackDiscardPath,
                $false
            )
            Set-Acl -LiteralPath $envPath -AclObject $originalAcl
            $restoredBytes = [IO.File]::ReadAllBytes($envPath)
            $restoredAcl = Get-Acl -LiteralPath $envPath
            if (
                (Get-Sha256 -Bytes $restoredBytes) -ne $originalHash -or
                (Get-AclFingerprint -Acl $restoredAcl) -ne $originalAclFingerprint
            ) {
                throw "Rollback verification failed."
            }
        }
        catch {
            throw (
                "Hermes .env update failed and automatic rollback could not " +
                "be verified. Do not restart Hermes."
            )
        }
    }
    throw
}
finally {
    if ($null -ne $stream) {
        $stream.Dispose()
    }
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
    if ($replacementVerified -and (Test-Path -LiteralPath $backupPath)) {
        Remove-Item -LiteralPath $backupPath -Force
    }
    if (Test-Path -LiteralPath $rollbackDiscardPath) {
        Remove-Item -LiteralPath $rollbackDiscardPath -Force
    }
}

Write-Output "HermesEnvironmentConfigured : True"
Write-Output "SettingsChanged            : True"
Write-Output "SettingsPresent            : 2"
Write-Output "RestartRequired            : True"
