[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [string]$ProjectRoot = "",

    [string]$ConfigPath = "",

    [string]$HermesHome = "",

    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$aclSections = (
    [System.Security.AccessControl.AccessControlSections]::Owner -bor
    [System.Security.AccessControl.AccessControlSections]::Group -bor
    [System.Security.AccessControl.AccessControlSections]::Access
)
$fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$noInheritance = [System.Security.AccessControl.InheritanceFlags]::None
$dataInheritance = (
    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
)
$noPropagation = [System.Security.AccessControl.PropagationFlags]::None

function Get-NormalizedPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    try {
        $fullPath = [System.IO.Path]::GetFullPath($Path)
        $filesystemRoot = [System.IO.Path]::GetPathRoot($fullPath)
        if ($fullPath.Equals(
                $filesystemRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            return $filesystemRoot
        }
        return $fullPath.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    }
    catch {
        throw "A required runtime path is invalid."
    }
}

function Assert-PathWithinRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$Path,

        [switch]$AllowRoot
    )

    $normalizedRoot = Get-NormalizedPath -Path $Root
    $normalizedPath = Get-NormalizedPath -Path $Path
    if (
        $AllowRoot -and
        $normalizedPath.Equals(
            $normalizedRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return
    }

    $prefix = $normalizedRoot
    if (-not $prefix.EndsWith([string][System.IO.Path]::DirectorySeparatorChar)) {
        $prefix += [System.IO.Path]::DirectorySeparatorChar
    }
    if (-not $normalizedPath.StartsWith(
            $prefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "A configured runtime path escapes its required project directory."
    }
}

function Test-PathWithinRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$Path,

        [switch]$AllowRoot
    )

    try {
        Assert-PathWithinRoot -Root $Root -Path $Path -AllowRoot:$AllowRoot
        return $true
    }
    catch {
        return $false
    }
}

function Assert-NotCloudPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    foreach ($name in @("OneDrive", "OneDriveConsumer", "OneDriveCommercial")) {
        $value = [System.Environment]::GetEnvironmentVariable($name)
        if (
            -not [string]::IsNullOrWhiteSpace($value) -and
            [System.IO.Path]::IsPathRooted($value) -and
            (Test-PathWithinRoot -Root $value -Path $Path -AllowRoot)
        ) {
            throw "The local Wiki runtime must not be inside a cloud-synced directory."
        }
    }
    $segments = (Get-NormalizedPath -Path $Path) -split '[\\/]'
    if (@($segments | Where-Object { $_ -match '(?i)^OneDrive(?:\s*-.*)?$' }).Count -gt 0) {
        throw "The local Wiki runtime must not be inside OneDrive."
    }
}

function Assert-NoReparseComponents {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $normalizedRoot = Get-NormalizedPath -Path $Root
    $current = Get-NormalizedPath -Path $Path
    Assert-PathWithinRoot -Root $normalizedRoot -Path $current -AllowRoot

    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "A protected runtime path contains a reparse point."
            }
        }
        if ($current.Equals(
                $normalizedRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            break
        }
        $parent = Split-Path -Parent $current
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) {
            throw "A protected runtime path could not be anchored to the project."
        }
        $current = Get-NormalizedPath -Path $parent
    }
}

function Resolve-ExistingDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
        throw "$Description does not exist."
    }
    $item = Get-Item -LiteralPath $resolved.Path -Force
    if (-not $item.PSIsContainer) {
        throw "$Description must be a directory."
    }
    return $item.FullName
}

function Resolve-ExistingFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
        throw "$Description does not exist."
    }
    $item = Get-Item -LiteralPath $resolved.Path -Force
    if ($item.PSIsContainer) {
        throw "$Description must be a file."
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description must not be a reparse point."
    }
    return $item.FullName
}

function Read-ConfigurationDocument {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $offset = 0
    if (
        $bytes.Length -ge 3 -and
        $bytes[0] -eq 0xEF -and
        $bytes[1] -eq 0xBB -and
        $bytes[2] -eq 0xBF
    ) {
        $offset = 3
    }
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $text = $strictUtf8.GetString($bytes, $offset, $bytes.Length - $offset)
    }
    catch [System.Text.DecoderFallbackException] {
        throw "The configuration file must be valid UTF-8."
    }
    if ($text.Contains([char]0)) {
        throw "The configuration file contains unsupported data."
    }
    try {
        $document = $text | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "The configuration file is not valid JSON."
    }
    if ($null -eq $document -or $document -is [System.Array]) {
        throw "The configuration root must be a JSON object."
    }
    return $document
}

function Get-OptionalTextProperty {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [object]$InputObject,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Default
    )

    if ($null -eq $InputObject) {
        return $Default
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        return $Default
    }
    $value = [string]$property.Value
    if ($value.IndexOfAny([char[]]@([char]0, [char]10, [char]13)) -ge 0) {
        throw "A configured runtime path contains unsupported characters."
    }
    return $value
}

function Resolve-ConfiguredRuntimePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ConfigDirectory,

        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $expanded = [System.Environment]::ExpandEnvironmentVariables($Value)
    if ([System.IO.Path]::IsPathRooted($expanded)) {
        return Get-NormalizedPath -Path $expanded
    }
    return Get-NormalizedPath -Path (Join-Path $ConfigDirectory $expanded)
}

function New-SecurityDescriptorFromSddl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Sddl,

        [Parameter(Mandatory = $true)]
        [bool]$Directory
    )

    $security = if ($Directory) {
        New-Object System.Security.AccessControl.DirectorySecurity
    }
    else {
        New-Object System.Security.AccessControl.FileSecurity
    }
    $security.SetSecurityDescriptorSddlForm($Sddl)
    return $security
}

function Get-AclSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [bool]$Directory
    )

    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    return [pscustomobject]@{
        Path = $Path
        Directory = $Directory
        Owner = $owner
        Sddl = $acl.GetSecurityDescriptorSddlForm($aclSections)
    }
}

function New-DesiredAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [object]$Snapshot,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    $acl = New-SecurityDescriptorFromSddl `
        -Sddl $Snapshot.Sddl `
        -Directory ([bool]$Snapshot.Directory)
    $acl.SetAccessRuleProtection($true, $false)
    $existing = @(
        $acl.GetAccessRules(
            $true,
            $true,
            [System.Security.Principal.SecurityIdentifier]
        )
    )
    foreach ($rule in $existing) {
        [void]$acl.RemoveAccessRuleSpecific($rule)
    }

    $inheritance = if ($Snapshot.Directory) {
        $dataInheritance
    }
    else {
        $noInheritance
    }
    foreach ($sidValue in $AllowedSids) {
        $sid = New-Object System.Security.Principal.SecurityIdentifier($sidValue)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid,
            $fullControl,
            $inheritance,
            $noPropagation,
            $allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    $acl.SetOwner(
        (New-Object System.Security.Principal.SecurityIdentifier($OwnerSid))
    )
    return $acl
}

function Test-AclMatchesPolicy {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSystemSecurity]$Acl,

        [Parameter(Mandatory = $true)]
        [object]$Snapshot,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    if (-not $Acl.AreAccessRulesProtected) {
        return $false
    }
    $owner = $Acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($owner -cne $OwnerSid) {
        return $false
    }

    $expected = @{}
    foreach ($sid in $AllowedSids) {
        $expected[$sid] = $true
    }
    $expectedInheritance = if ($Snapshot.Directory) {
        $dataInheritance
    }
    else {
        $noInheritance
    }
    $rules = @(
        $Acl.GetAccessRules(
            $true,
            $true,
            [System.Security.Principal.SecurityIdentifier]
        )
    )
    if ($rules.Count -ne $expected.Count) {
        return $false
    }
    $seen = @{}
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if (
            -not $expected.ContainsKey($sid) -or
            $seen.ContainsKey($sid) -or
            $rule.IsInherited -or
            $rule.AccessControlType -ne $allow -or
            [int]$rule.FileSystemRights -ne [int]$fullControl -or
            $rule.InheritanceFlags -ne $expectedInheritance -or
            $rule.PropagationFlags -ne $noPropagation
        ) {
            return $false
        }
        $seen[$sid] = $true
    }
    return $seen.Count -eq $expected.Count
}

function Assert-AclMatchesPolicy {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [object]$Plan,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    $current = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Plan.Snapshot.Path
    if (-not (Test-AclMatchesPolicy `
                -Acl $current `
                -Snapshot $Plan.Snapshot `
                -AllowedSids $AllowedSids `
                -OwnerSid $OwnerSid)) {
        throw "ACL policy verification failed after an attempted update."
    }
}

function Get-DataTreeItems {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$DataPath
    )

    $items = @(
        Get-ChildItem -LiteralPath $DataPath -Force -Recurse |
            Sort-Object @{ Expression = { $_.FullName.Length }; Descending = $true },
                @{ Expression = { $_.FullName }; Descending = $false }
    )
    foreach ($item in $items) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The runtime data directory contains a reparse point."
        }
        Assert-PathWithinRoot -Root $DataPath -Path $item.FullName
    }
    return $items
}

function Assert-DataTreeUnchanged {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$DataPath,

        [Parameter(Mandatory = $true)]
        [string[]]$ExpectedPaths
    )

    $currentPaths = [string[]]@(
        Get-DataTreeItems -DataPath $DataPath |
            ForEach-Object { Get-NormalizedPath -Path $_.FullName } |
            Sort-Object
    )
    $expected = [string[]]@($ExpectedPaths | Sort-Object)
    if ($currentPaths.Count -ne $expected.Count) {
        throw "The runtime data tree changed during ACL hardening."
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if (-not $currentPaths[$index].Equals(
                $expected[$index],
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "The runtime data tree changed during ACL hardening."
        }
    }
}

function Restore-AclSnapshots {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Snapshots
    )

    $rollbackFailed = $false
    for ($index = $Snapshots.Count - 1; $index -ge 0; $index--) {
        $snapshot = $Snapshots[$index]
        try {
            $original = New-SecurityDescriptorFromSddl `
                -Sddl $snapshot.Sddl `
                -Directory ([bool]$snapshot.Directory)
            Microsoft.PowerShell.Security\Set-Acl `
                -LiteralPath $snapshot.Path `
                -AclObject $original
            $restored = Microsoft.PowerShell.Security\Get-Acl `
                -LiteralPath $snapshot.Path
            $restoredSddl = $restored.GetSecurityDescriptorSddlForm($aclSections)
            $restoredOwner = $restored.GetOwner(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
            if (
                $restoredSddl -cne [string]$snapshot.Sddl -or
                $restoredOwner -cne [string]$snapshot.Owner
            ) {
                $rollbackFailed = $true
            }
        }
        catch {
            $rollbackFailed = $true
        }
    }
    if ($rollbackFailed) {
        throw (
            "ACL hardening failed and rollback verification also failed. " +
            "Do not use the runtime paths until their ACLs are restored manually."
        )
    }
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        throw "Cannot determine the project root; pass -ProjectRoot explicitly."
    }
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
}
$ProjectRoot = Resolve-ExistingDirectory `
    -Path $ProjectRoot `
    -Description "Project root"
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "pyproject.toml") -PathType Leaf)) {
    throw "Project root must contain pyproject.toml."
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot "config\sync.local.json"
}
$ConfigPath = Resolve-ExistingFile `
    -Path $ConfigPath `
    -Description "Runtime configuration"
Assert-PathWithinRoot -Root $ProjectRoot -Path $ConfigPath
Assert-NoReparseComponents -Root $ProjectRoot -Path $ConfigPath

$dataPath = Join-Path $ProjectRoot "data"
$dataPath = Resolve-ExistingDirectory -Path $dataPath -Description "Runtime data directory"
Assert-PathWithinRoot -Root $ProjectRoot -Path $dataPath
Assert-NoReparseComponents -Root $ProjectRoot -Path $dataPath
$dataTreeItems = @(Get-DataTreeItems -DataPath $dataPath)
$expectedDataPaths = [string[]]@(
    $dataTreeItems | ForEach-Object { Get-NormalizedPath -Path $_.FullName }
)

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "LOCALAPPDATA is required to resolve the local Wiki runtime."
}
$localAppData = Resolve-ExistingDirectory `
    -Path $env:LOCALAPPDATA `
    -Description "LOCALAPPDATA"
$localVolumeRoot = [System.IO.Path]::GetPathRoot($localAppData)
if ([string]::IsNullOrWhiteSpace($localVolumeRoot)) {
    throw "LOCALAPPDATA could not be anchored to a filesystem root."
}
Assert-NoReparseComponents -Root $localVolumeRoot -Path $localAppData
$wikiApplicationRoot = Resolve-ExistingDirectory `
    -Path (Join-Path $localAppData "notion-excel-sync") `
    -Description "Local Wiki application root"
Assert-PathWithinRoot -Root $localAppData -Path $wikiApplicationRoot
Assert-NoReparseComponents -Root $localAppData -Path $wikiApplicationRoot
$wikiRuntimeRoot = Resolve-ExistingDirectory `
    -Path (Join-Path $wikiApplicationRoot "wiki") `
    -Description "Local Wiki runtime"
Assert-PathWithinRoot -Root $localAppData -Path $wikiRuntimeRoot
Assert-NoReparseComponents -Root $localAppData -Path $wikiRuntimeRoot
Assert-NotCloudPath -Path $wikiRuntimeRoot
if (
    (Test-PathWithinRoot -Root $ProjectRoot -Path $wikiRuntimeRoot -AllowRoot) -or
    (Test-PathWithinRoot -Root $wikiRuntimeRoot -Path $ProjectRoot -AllowRoot)
) {
    throw "The local Wiki runtime must be outside the project directory."
}
$wikiTreeItems = @(Get-DataTreeItems -DataPath $wikiRuntimeRoot)
$expectedWikiPaths = [string[]]@(
    $wikiTreeItems | ForEach-Object { Get-NormalizedPath -Path $_.FullName }
)

if ([string]::IsNullOrWhiteSpace($HermesHome)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is required to resolve the Hermes home directory."
    }
    $HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
}
$HermesHome = Resolve-ExistingDirectory -Path $HermesHome -Description "Hermes home"
$hermesVolumeRoot = [System.IO.Path]::GetPathRoot($HermesHome)
if ([string]::IsNullOrWhiteSpace($hermesVolumeRoot)) {
    throw "Hermes home could not be anchored to a filesystem root."
}
Assert-NoReparseComponents -Root $hermesVolumeRoot -Path $HermesHome
$hermesEnvPath = Resolve-ExistingFile `
    -Path (Join-Path $HermesHome ".env") `
    -Description "Hermes environment file"
Assert-PathWithinRoot -Root $HermesHome -Path $hermesEnvPath
Assert-NoReparseComponents -Root $HermesHome -Path $hermesEnvPath

$configuration = Read-ConfigurationDocument -Path $ConfigPath
$configDirectory = Split-Path -Parent $ConfigPath
$stateDbValue = Get-OptionalTextProperty `
    -InputObject $configuration `
    -Name "state_db" `
    -Default "../data/state.db"
$workDirValue = Get-OptionalTextProperty `
    -InputObject $configuration `
    -Name "work_dir" `
    -Default "../data/snapshots"
$onedriveProperty = $configuration.PSObject.Properties["onedrive"]
$onedriveConfiguration = if ($null -eq $onedriveProperty) {
    $null
}
else {
    $onedriveProperty.Value
}
$tokenCacheValue = Get-OptionalTextProperty `
    -InputObject $onedriveConfiguration `
    -Name "token_cache" `
    -Default "../data/msal-token-cache.bin"
$wikiProperty = $configuration.PSObject.Properties["wiki"]
$wikiConfiguration = if ($null -eq $wikiProperty) {
    $null
}
else {
    $wikiProperty.Value
}
$wikiIndexValue = Get-OptionalTextProperty `
    -InputObject $wikiConfiguration `
    -Name "index_db" `
    -Default (Join-Path $wikiRuntimeRoot "index.db")

$projectRuntimePaths = @(
    (Resolve-ConfiguredRuntimePath `
        -ConfigDirectory $configDirectory `
        -Value $stateDbValue),
    (Resolve-ConfiguredRuntimePath `
        -ConfigDirectory $configDirectory `
        -Value $workDirValue),
    (Resolve-ConfiguredRuntimePath `
        -ConfigDirectory $configDirectory `
        -Value $tokenCacheValue)
)
foreach ($runtimePath in $projectRuntimePaths) {
    Assert-PathWithinRoot -Root $dataPath -Path $runtimePath -AllowRoot
    Assert-NoReparseComponents -Root $dataPath -Path $runtimePath
}
$wikiIndexPath = Resolve-ConfiguredRuntimePath `
    -ConfigDirectory $configDirectory `
    -Value $wikiIndexValue
if (-not [System.IO.Path]::IsPathRooted(
        [System.Environment]::ExpandEnvironmentVariables($wikiIndexValue)
    )) {
    throw "Configured wiki.index_db must be an absolute local path."
}
if (-not (Test-PathWithinRoot `
        -Root $wikiRuntimeRoot `
        -Path $wikiIndexPath)) {
    throw (
        "Configured wiki.index_db must be inside " +
        "%LOCALAPPDATA%\notion-excel-sync\wiki."
    )
}
Assert-NoReparseComponents -Root $wikiRuntimeRoot -Path $wikiIndexPath
if (Test-Path -LiteralPath $projectRuntimePaths[0] -PathType Container) {
    throw "Configured state_db must be a file path."
}
if (Test-Path -LiteralPath $projectRuntimePaths[1] -PathType Leaf) {
    throw "Configured work_dir must be a directory path."
}
if (Test-Path -LiteralPath $projectRuntimePaths[2] -PathType Container) {
    throw "Configured token_cache must be a file path."
}
if (Test-Path -LiteralPath $wikiIndexPath -PathType Container) {
    throw "Configured wiki.index_db must be a file path."
}

$operator = [System.Security.Principal.WindowsIdentity]::GetCurrent()
if ($null -eq $operator.User) {
    throw "The current Windows operator has no security identifier."
}
$allowedSidMap = [ordered]@{}
foreach ($sid in @(
        $operator.User.Value,
        "S-1-5-18",
        "S-1-5-32-544"
    )) {
    $allowedSidMap[$sid] = $true
}
$allowedSids = [string[]]@($allowedSidMap.Keys)
$operatorSid = $operator.User.Value

$targets = New-Object 'System.Collections.Generic.List[object]'
foreach ($item in $dataTreeItems) {
    $targets.Add([pscustomobject]@{
            Label = "runtime data descendant"
            Path = $item.FullName
            Directory = [bool]$item.PSIsContainer
        })
}
$targets.Add(
    [pscustomobject]@{
        Label = "local Wiki runtime root"
        Path = $wikiRuntimeRoot
        Directory = $true
    }
)
foreach ($item in $wikiTreeItems) {
    $targets.Add([pscustomobject]@{
            Label = "local Wiki runtime descendant"
            Path = $item.FullName
            Directory = [bool]$item.PSIsContainer
        })
}
$targets.Add(
    [pscustomobject]@{
        Label = "local Wiki application root"
        Path = $wikiApplicationRoot
        Directory = $true
    }
)
$targets.Add(
    [pscustomobject]@{
        Label = "runtime data root"
        Path = $dataPath
        Directory = $true
    }
)
$targets.Add(
    [pscustomobject]@{
        Label = "configuration"
        Path = $ConfigPath
        Directory = $false
    }
)
$targets.Add(
    [pscustomobject]@{
        Label = "Hermes environment"
        Path = $hermesEnvPath
        Directory = $false
    }
)
$plans = New-Object 'System.Collections.Generic.List[object]'
foreach ($target in $targets) {
    $snapshot = Get-AclSnapshot `
        -Path $target.Path `
        -Directory ([bool]$target.Directory)
    $desired = New-DesiredAcl `
        -Snapshot $snapshot `
        -AllowedSids $allowedSids `
        -OwnerSid $operatorSid
    $current = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $target.Path
    $needsChange = -not (Test-AclMatchesPolicy `
            -Acl $current `
            -Snapshot $snapshot `
            -AllowedSids $allowedSids `
            -OwnerSid $operatorSid)
    $plans.Add([pscustomobject]@{
            Label = $target.Label
            Snapshot = $snapshot
            Desired = $desired
            NeedsChange = $needsChange
        })
}

$changesRequired = @($plans | Where-Object { $_.NeedsChange }).Count
$changesApplied = 0
$applyExecuted = $false
$rollbackPerformed = $false
if ($Apply -and $changesRequired -gt 0) {
    $applyExecuted = $PSCmdlet.ShouldProcess(
        "resolved configuration, runtime data tree, and Hermes environment file",
        "replace ACLs with the protected three-principal policy"
    )
}
elseif ($Apply) {
    $applyExecuted = $true
}

if ($applyExecuted -and $changesRequired -gt 0) {
    $attempted = New-Object 'System.Collections.Generic.List[object]'
    try {
        Assert-DataTreeUnchanged `
            -DataPath $dataPath `
            -ExpectedPaths $expectedDataPaths
        Assert-DataTreeUnchanged `
            -DataPath $wikiRuntimeRoot `
            -ExpectedPaths $expectedWikiPaths
        foreach ($plan in $plans) {
            if (-not $plan.NeedsChange) {
                continue
            }
            $attempted.Add($plan.Snapshot)
            Microsoft.PowerShell.Security\Set-Acl `
                -LiteralPath $plan.Snapshot.Path `
                -AclObject $plan.Desired
            Assert-AclMatchesPolicy `
                -Plan $plan `
                -AllowedSids $allowedSids `
                -OwnerSid $operatorSid
            $changesApplied++
        }
        foreach ($plan in $plans) {
            Assert-AclMatchesPolicy `
                -Plan $plan `
                -AllowedSids $allowedSids `
                -OwnerSid $operatorSid
        }
        Assert-DataTreeUnchanged `
            -DataPath $dataPath `
            -ExpectedPaths $expectedDataPaths
        Assert-DataTreeUnchanged `
            -DataPath $wikiRuntimeRoot `
            -ExpectedPaths $expectedWikiPaths
    }
    catch {
        $originalFailure = $_.Exception.Message
        $rollbackPerformed = $attempted.Count -gt 0
        if ($attempted.Count -gt 0) {
            try {
                [object[]]$rollbackSnapshots = $attempted.ToArray()
                Restore-AclSnapshots -Snapshots $rollbackSnapshots
            }
            catch {
                throw (
                    "ACL hardening failed and rollback failed. " +
                    "Original failure: $originalFailure Rollback failure: " +
                    $_.Exception.Message
                )
            }
        }
        throw (
            "ACL hardening failed; every attempted ACL was restored and verified. " +
            "Cause: $originalFailure"
        )
    }
}

$compliant = $changesRequired -eq 0 -or $applyExecuted

$mode = if (-not $Apply) {
    "DryRun"
}
elseif ([bool]$WhatIfPreference) {
    "WhatIf"
}
elseif ($applyExecuted) {
    "Apply"
}
else {
    "Declined"
}

[pscustomobject]@{
    RuntimeAclHardening = $true
    Mode = $mode
    ApplyRequested = [bool]$Apply
    ChangesRequired = $changesRequired
    ChangesApplied = $changesApplied
    Compliant = $compliant
    RollbackPerformed = $rollbackPerformed
    RequiredTargets = @(
        $plans |
            Where-Object { $_.NeedsChange } |
            ForEach-Object {
                [pscustomobject]@{
                    Label = $_.Label
                    Path = $_.Snapshot.Path
                    Owner = $_.Snapshot.Owner
                }
            }
    )
    TargetsEvaluated = $plans.Count
    ConfigPath = $ConfigPath
    DataPath = $dataPath
    WikiRuntimeRoot = $wikiRuntimeRoot
    WikiApplicationRoot = $wikiApplicationRoot
    WikiIndexPath = $wikiIndexPath
    HermesEnvPath = $hermesEnvPath
}
