[CmdletBinding()]
param(
    [string]$HermesHome = $(
        if ($env:HERMES_HOME) {
            $env:HERMES_HOME
        }
        else {
            Join-Path $env:LOCALAPPDATA "hermes"
        }
    ),

    [string]$HermesRuntimeRoot = "",

    # Development/CI only. Production activation must use HermesHome\hermes-agent.
    [switch]$AllowExternalHermesRuntimeForTest,

    # Development/CI only. Production activation revalidates the installed tree.
    [switch]$SkipInstalledIntegrityCheckForTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-ExistingDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Required directory does not exist."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to use a reparse-point directory."
    }
    return $item.FullName
}

function Resolve-ExistingFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file does not exist."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to use a reparse-point file."
    }
    return $item.FullName
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

function Get-AclAccessFingerprint {
    param(
        [Parameter(Mandatory = $true)]
        [Security.AccessControl.FileSecurity]$Acl
    )

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
        "Protected=" + $Acl.AreAccessRulesProtected.ToString() +
        "|Rules=" + ($rules.ToArray() -join ";")
    )
}

function Get-RelativeRuntimePath {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$FilePath
    )

    $root = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $path = [IO.Path]::GetFullPath($FilePath)
    $prefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installed runtime file escaped its root."
    }
    return $path.Substring($prefix.Length).Replace('\', '/')
}

function Test-JsonMapContract {
    param(
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($null -eq $Actual) {
        throw "Installed runtime manifest is missing $Label."
    }
    $properties = @($Actual.PSObject.Properties)
    if ($properties.Count -ne $Expected.Count) {
        throw "Installed runtime manifest $Label contract differs."
    }
    foreach ($key in $Expected.Keys) {
        $property = $Actual.PSObject.Properties[$key]
        if ($null -eq $property -or [string]$property.Value -cne $Expected[$key]) {
            throw "Installed runtime manifest $Label contract differs."
        }
    }
}

function Test-PluginAclContract {
    param([Parameter(Mandatory = $true)][string]$Path)

    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Installed Hermes plugin ACL is not protected."
    }
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentUser) {
        throw "Cannot determine the current Windows identity."
    }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier])
    if ($owner.Value -cne $currentUser.Value) {
        throw "Installed Hermes plugin owner differs from the installing user."
    }
    $expectedSids = @{
        $currentUser.Value = $true
        "S-1-5-18" = $true
        "S-1-5-32-544" = $true
    }
    $rules = @($acl.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        ))
    if ($rules.Count -ne $expectedSids.Count) {
        throw "Installed Hermes plugin ACL has an unexpected rule count."
    }
    $requiredInheritance = (
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if (
            -not $expectedSids.ContainsKey($sid) -or
            $rule.AccessControlType -ne
                [Security.AccessControl.AccessControlType]::Allow -or
            $rule.FileSystemRights -ne
                [Security.AccessControl.FileSystemRights]::FullControl -or
            $rule.InheritanceFlags -ne $requiredInheritance -or
            $rule.PropagationFlags -ne
                [Security.AccessControl.PropagationFlags]::None -or
            $rule.IsInherited
        ) {
            throw "Installed Hermes plugin ACL differs from the installer contract."
        }
        $expectedSids.Remove($sid)
    }
    if ($expectedSids.Count -ne 0) {
        throw "Installed Hermes plugin ACL is missing a required principal."
    }
}

function Test-InstalledRuntimeManifest {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath
    )

    foreach ($item in Get-ChildItem -LiteralPath $RuntimeRoot -Recurse -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Installed runtime contains a reparse point."
        }
    }
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if (
        $manifest.format -ne 1 -or
        $manifest.algorithm -ne "sha256" -or
        $manifest.package -ne "notion_excel_sync"
    ) {
        throw "Installed runtime manifest has an unsupported contract."
    }
    Test-JsonMapContract `
        -Actual $manifest.distributions `
        -Expected ([ordered]@{
            openpyxl = "3.1.5"
            "et-xmlfile" = "2.0.0"
            msal = "1.37.0"
            "msal-extensions" = "1.3.1"
        }) `
        -Label "distribution"
    Test-JsonMapContract `
        -Actual $manifest.host_requirements `
        -Expected ([ordered]@{
            "hermes-agent" = "==0.18.2"
            requests = "==2.33.0"
            urllib3 = "==2.7.0"
            certifi = "==2026.5.20"
            "charset-normalizer" = "==3.4.4"
            idna = "==3.15"
            PyJWT = "==2.13.0"
            cryptography = "==46.0.7"
            cffi = "==2.0.0"
            pycparser = "==3.0"
            Pillow = "==12.2.0"
            defusedxml = "==0.7.1"
            portalocker = "==3.2.0"
        }) `
        -Label "host dependency"
    $expected = @{}
    foreach ($property in $manifest.files.PSObject.Properties) {
        $relative = [string]$property.Name
        $digest = [string]$property.Value
        if (
            -not $relative -or
            $relative.StartsWith("/") -or
            $relative.Contains("\") -or
            $relative.Split('/') -contains ".." -or
            $digest -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "Installed runtime manifest contains an unsafe entry."
        }
        $expected[$relative] = $digest
    }
    if ($expected.Count -eq 0) {
        throw "Installed runtime manifest is empty."
    }
    if (-not $expected.ContainsKey(
            "notion_excel_sync/security/hermes_gateway.py"
        )) {
        throw "Installed runtime manifest is missing the trusted gateway module."
    }

    $actual = @{}
    foreach ($file in Get-ChildItem -LiteralPath $RuntimeRoot -Recurse -File -Force) {
        $relative = Get-RelativeRuntimePath `
            -RuntimeRoot $RuntimeRoot `
            -FilePath $file.FullName
        $actual[$relative] = (
            Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
    if ($actual.Count -ne $expected.Count) {
        throw "Installed runtime file count does not match its manifest."
    }
    foreach ($relative in $expected.Keys) {
        if (
            -not $actual.ContainsKey($relative) -or
            $actual[$relative] -cne $expected[$relative]
        ) {
            throw "Installed runtime integrity verification failed."
        }
    }
}

function New-ProtectedSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$AccessFingerprint
    )

    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $Path,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $stream.Dispose()
        $stream = $null

        # The snapshot is created beside config.yaml and therefore inherits the
        # same DACL. Check that access contract while the file is still empty;
        # do not attempt to copy the original owner (which may require elevation).
        $snapshotAcl = Get-Acl -LiteralPath $Path
        if (
            (Get-AclAccessFingerprint -Acl $snapshotAcl) -ne $AccessFingerprint
        ) {
            throw "Activation snapshot access ACL verification failed."
        }

        $stream = New-Object IO.FileStream(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $stream.SetLength(0)
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "This activation wrapper is supported only on Windows."
}

$scriptPath = $MyInvocation.MyCommand.Path
if (-not $scriptPath) {
    throw "Cannot determine the activation script path."
}
$scriptDirectory = Split-Path -Parent $scriptPath
$projectRoot = Split-Path -Parent $scriptDirectory
$helperPath = Resolve-ExistingFile -Path (
    Join-Path $scriptDirectory "activate-hermes-plugin.py"
)

$HermesHome = Resolve-ExistingDirectory -Path $HermesHome
if (-not $HermesRuntimeRoot) {
    $HermesRuntimeRoot = Join-Path $HermesHome "hermes-agent"
}
$HermesRuntimeRoot = Resolve-ExistingDirectory -Path $HermesRuntimeRoot
$expectedRuntime = [IO.Path]::GetFullPath(
    (Join-Path $HermesHome "hermes-agent")
).TrimEnd('\')
if (
    -not $AllowExternalHermesRuntimeForTest -and
    -not $HermesRuntimeRoot.TrimEnd('\').Equals(
        $expectedRuntime,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "Hermes runtime does not belong to the selected Hermes home."
}

$pluginName = "notion-excel-sync-attestor"
$pluginDirectory = Resolve-ExistingDirectory -Path (
    Join-Path $HermesHome "plugins\$pluginName"
)
[void](Resolve-ExistingFile -Path (Join-Path $pluginDirectory "plugin.yaml"))
$installedShim = Resolve-ExistingFile -Path (Join-Path $pluginDirectory "__init__.py")
$installedPluginManifest = Resolve-ExistingFile -Path (
    Join-Path $pluginDirectory "plugin.yaml"
)
$installedRuntimeManifest = Resolve-ExistingFile -Path (
    Join-Path $pluginDirectory "runtime-manifest.json"
)
Test-PluginAclContract -Path $pluginDirectory
if (-not $SkipInstalledIntegrityCheckForTest) {
    $sourcePlugin = Resolve-ExistingDirectory -Path (
        Join-Path $projectRoot ".hermes\plugins\notion-excel-sync-attestor"
    )
    $sourceShim = Resolve-ExistingFile -Path (Join-Path $sourcePlugin "__init__.py")
    $sourcePluginManifest = Resolve-ExistingFile -Path (
        Join-Path $sourcePlugin "plugin.yaml"
    )
    if (
        (Get-FileHash -LiteralPath $sourceShim -Algorithm SHA256).Hash -cne
        (Get-FileHash -LiteralPath $installedShim -Algorithm SHA256).Hash -or
        (Get-FileHash -LiteralPath $sourcePluginManifest -Algorithm SHA256).Hash -cne
        (Get-FileHash -LiteralPath $installedPluginManifest -Algorithm SHA256).Hash
    ) {
        throw "Installed Hermes plugin shim differs from the verified project source."
    }
    Test-InstalledRuntimeManifest `
        -RuntimeRoot (Join-Path $pluginDirectory "runtime") `
        -ManifestPath $installedRuntimeManifest
}

$configPath = Resolve-ExistingFile -Path (Join-Path $HermesHome "config.yaml")
$originalBytes = [IO.File]::ReadAllBytes($configPath)
$originalHash = Get-Sha256 -Bytes $originalBytes
$originalAcl = Get-Acl -LiteralPath $configPath
$originalAccessFingerprint = Get-AclAccessFingerprint -Acl $originalAcl
$originalOwner = $originalAcl.GetOwner(
    [Security.Principal.SecurityIdentifier]
).Value
$currentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

$venvConfig = Resolve-ExistingFile -Path (
    Join-Path $HermesRuntimeRoot "venv\pyvenv.cfg"
)
$homeLine = Get-Content -LiteralPath $venvConfig |
    Where-Object { $_ -match '^home\s*=\s*(.+?)\s*$' } |
    Select-Object -First 1
$homeMatch = [regex]::Match([string]$homeLine, '^home\s*=\s*(.+?)\s*$')
if (-not $homeMatch.Success) {
    throw "Hermes base Python location is missing."
}
$basePython = Resolve-ExistingFile -Path (
    Join-Path $homeMatch.Groups[1].Value.Trim() "python.exe"
)

$operationId = [Guid]::NewGuid().ToString("N")
$configDirectory = Split-Path -Parent $configPath
$snapshotPath = Join-Path $configDirectory (
    ".config.notion-excel-sync.$operationId.snapshot"
)
$failedPath = Join-Path $configDirectory (
    ".config.notion-excel-sync.$operationId.failed"
)
$activationSucceeded = $false
$rollbackConfirmed = $false

try {
    New-ProtectedSnapshot `
        -Path $snapshotPath `
        -Bytes $originalBytes `
        -AccessFingerprint $originalAccessFingerprint

    $helperArguments = @(
        "-I",
        $helperPath,
        "--hermes-home",
        $HermesHome,
        "--hermes-runtime",
        $HermesRuntimeRoot
    )
    if ($AllowExternalHermesRuntimeForTest) {
        $helperArguments += "--allow-external-runtime-for-test"
    }
    $output = @(& $basePython $helperArguments 2>&1)
    $helperExitCode = $LASTEXITCODE
    if ($helperExitCode -ne 0) {
        throw "Hermes rejected the plugin activation request."
    }
    $payload = ($output -join [Environment]::NewLine) | ConvertFrom-Json
    if (
        $payload.ok -ne $true -or
        $payload.plugin -ne $pluginName -or
        $payload.enabled -ne $true -or
        $payload.override_allowed -ne $false -or
        $payload.restart_required -ne $true
    ) {
        throw "Hermes plugin activation verification returned an unsafe result."
    }

    $currentAcl = Get-Acl -LiteralPath $configPath
    $currentOwner = $currentAcl.GetOwner(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if (
        (Get-AclAccessFingerprint -Acl $currentAcl) -ne
            $originalAccessFingerprint -or
        ($currentOwner -cne $originalOwner -and $currentOwner -cne $currentUserSid)
    ) {
        throw "Hermes config ACL changed during activation."
    }

    Remove-Item -LiteralPath $snapshotPath -Force
    $activationSucceeded = $true

    [pscustomobject]@{
        Plugin = $pluginName
        Enabled = $true
        AllowToolOverride = $false
        ConfigChanged = [bool]$payload.changed
        RestartRequired = $true
    }
}
catch {
    try {
        $currentBytes = [IO.File]::ReadAllBytes($configPath)
        if ((Get-Sha256 -Bytes $currentBytes) -ne $originalHash) {
            if (-not (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
                throw "Protected activation snapshot is unavailable."
            }
            [IO.File]::Replace(
                $snapshotPath,
                $configPath,
                $failedPath,
                $false
            )
        }
        $restoredBytes = [IO.File]::ReadAllBytes($configPath)
        $restoredAcl = Get-Acl -LiteralPath $configPath
        $restoredOwner = $restoredAcl.GetOwner(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if (
            (Get-Sha256 -Bytes $restoredBytes) -ne $originalHash -or
            (Get-AclAccessFingerprint -Acl $restoredAcl) -ne
                $originalAccessFingerprint -or
            (
                $restoredOwner -cne $originalOwner -and
                $restoredOwner -cne $currentUserSid
            )
        ) {
            throw "Activation rollback verification failed."
        }
        $rollbackConfirmed = $true
        if (Test-Path -LiteralPath $snapshotPath) {
            Remove-Item -LiteralPath $snapshotPath -Force
        }
        if (Test-Path -LiteralPath $failedPath) {
            Remove-Item -LiteralPath $failedPath -Force
        }
    }
    catch {
        throw (
            "Hermes plugin activation failed and rollback could not be " +
            "verified. Do not restart Hermes."
        )
    }
    throw "Hermes plugin activation failed safely; the original config was restored."
}
finally {
    if ($activationSucceeded -or $rollbackConfirmed) {
        if (Test-Path -LiteralPath $snapshotPath) {
            Remove-Item -LiteralPath $snapshotPath -Force
        }
        if (Test-Path -LiteralPath $failedPath) {
            Remove-Item -LiteralPath $failedPath -Force
        }
    }
}
