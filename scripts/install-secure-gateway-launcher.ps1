[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [string]$ProjectRoot = "",

    [string]$HermesHome = "",

    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$launcherDirectoryName = "secure-gateway-launcher"
$pluginName = "notion-excel-sync-attestor"
$launcherFileNames = [string[]]@(
    "restart-hermes-gateway-secure.ps1",
    "restart-hermes-gateway-secure.py"
)
$manifestFileName = "secure-launcher-manifest.json"
$pluginFileNames = [string[]]@(
    "plugin.yaml",
    "__init__.py",
    "runtime-manifest.json"
)
$allowedBundleNames = [string[]]@($launcherFileNames + $manifestFileName)
$aclSections = (
    [System.Security.AccessControl.AccessControlSections]::Owner -bor
    [System.Security.AccessControl.AccessControlSections]::Group -bor
    [System.Security.AccessControl.AccessControlSections]::Access
)
$fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$noInheritance = [System.Security.AccessControl.InheritanceFlags]::None
$directoryInheritance = (
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
        throw "A secure launcher path is invalid."
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
        throw "A secure launcher path escaped its trusted root."
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
                throw "A secure launcher path contains a reparse point."
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
            throw "A secure launcher path could not be anchored to its trusted root."
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
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description must not be a reparse point."
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

function Get-Sha256Hex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes
    )

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString(
            $algorithm.ComputeHash($Bytes)
        ).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Read-SourceBundle {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ScriptsPath
    )

    $files = [ordered]@{}
    foreach ($name in $launcherFileNames) {
        $path = Resolve-ExistingFile `
            -Path (Join-Path $ScriptsPath $name) `
            -Description "Secure launcher source file"
        Assert-PathWithinRoot -Root $ScriptsPath -Path $path
        Assert-NoReparseComponents -Root $ScriptsPath -Path $path
        $bytes = [System.IO.File]::ReadAllBytes($path)
        if ($bytes.Length -eq 0) {
            throw "A secure launcher source file is empty."
        }
        $files[$name] = [pscustomobject]@{
            Path = $path
            Bytes = $bytes
            Sha256 = Get-Sha256Hex -Bytes $bytes
        }
    }
    return $files
}

function New-ManifestBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$SourceFiles,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$PluginFiles
    )

    $hashes = [ordered]@{}
    foreach ($name in $launcherFileNames) {
        $hashes[$name] = [string]$SourceFiles[$name].Sha256
    }
    $pluginHashes = [ordered]@{}
    foreach ($name in $pluginFileNames) {
        $pluginHashes[$name] = [string]$PluginFiles[$name].Sha256
    }
    $document = [ordered]@{
        format = 1
        algorithm = "sha256"
        files = $hashes
        plugin_files = $pluginHashes
    }
    $text = ($document | ConvertTo-Json -Depth 4) + "`n"
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    return $strictUtf8.GetBytes($text)
}

function Read-ProtectedPluginBinding {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$HermesHomePath,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    $pluginsPath = Resolve-ExistingDirectory `
        -Path (Join-Path $HermesHomePath "plugins") `
        -Description "Hermes plugins directory"
    Assert-PathWithinRoot -Root $HermesHomePath -Path $pluginsPath
    Assert-NoReparseComponents -Root $HermesHomePath -Path $pluginsPath
    $pluginPath = Resolve-ExistingDirectory `
        -Path (Join-Path $pluginsPath $pluginName) `
        -Description "Installed synchronization plugin"
    Assert-PathWithinRoot -Root $pluginsPath -Path $pluginPath
    Assert-NoReparseComponents -Root $pluginsPath -Path $pluginPath

    $pluginAcl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $pluginPath
    if (-not (Test-AclMatchesPolicy `
                -Acl $pluginAcl `
                -Directory $true `
                -AllowedSids $AllowedSids `
                -OwnerSid $OwnerSid)) {
        throw "The installed synchronization plugin root ACL is not protected."
    }

    $files = [ordered]@{}
    foreach ($name in $pluginFileNames) {
        $path = Resolve-ExistingFile `
            -Path (Join-Path $pluginPath $name) `
            -Description "Installed synchronization plugin binding file"
        Assert-PathWithinRoot -Root $pluginPath -Path $path
        Assert-NoReparseComponents -Root $pluginPath -Path $path
        $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $path
        if (-not (Test-InheritedPluginFileAclMatchesPolicy `
                    -Acl $acl `
                    -AllowedSids $AllowedSids `
                    -OwnerSid $OwnerSid)) {
            throw "An installed synchronization plugin binding file ACL is not protected."
        }
        $bytes = [System.IO.File]::ReadAllBytes($path)
        if ($bytes.Length -eq 0) {
            throw "An installed synchronization plugin binding file is empty."
        }
        $files[$name] = [pscustomobject]@{
            Path = $path
            Sha256 = Get-Sha256Hex -Bytes $bytes
        }
    }
    return $files
}

function Test-InheritedPluginFileAclMatchesPolicy {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSystemSecurity]$Acl,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    if ($Acl.AreAccessRulesProtected) {
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
            -not $rule.IsInherited -or
            $rule.AccessControlType -ne $allow -or
            [int]$rule.FileSystemRights -ne [int]$fullControl -or
            $rule.InheritanceFlags -ne $noInheritance -or
            $rule.PropagationFlags -ne $noPropagation
        ) {
            return $false
        }
        $seen[$sid] = $true
    }
    return $seen.Count -eq $expected.Count
}

function Get-BundleEntries {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundlePath,

        [switch]$AllowMissing
    )

    if (-not (Test-Path -LiteralPath $BundlePath)) {
        if ($AllowMissing) {
            return @()
        }
        throw "The secure launcher bundle is unavailable."
    }
    $bundle = Get-Item -LiteralPath $BundlePath -Force
    if (-not $bundle.PSIsContainer) {
        throw "The secure launcher install path must be a directory."
    }
    if (($bundle.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The secure launcher install path must not be a reparse point."
    }

    $expected = @{}
    foreach ($name in $allowedBundleNames) {
        $expected[$name] = $true
    }
    $seen = @{}
    $entries = @(
        Get-ChildItem -LiteralPath $BundlePath -Force |
            Sort-Object Name
    )
    foreach ($entry in $entries) {
        Assert-PathWithinRoot -Root $BundlePath -Path $entry.FullName
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The secure launcher bundle contains a reparse point."
        }
        if ($entry.PSIsContainer -or -not $expected.ContainsKey($entry.Name)) {
            throw "The secure launcher bundle contains an unexpected entry."
        }
        if ($seen.ContainsKey($entry.Name)) {
            throw "The secure launcher bundle contains duplicate entries."
        }
        $seen[$entry.Name] = $true
    }
    return $entries
}

function Test-AclMatchesPolicy {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSystemSecurity]$Acl,

        [Parameter(Mandatory = $true)]
        [bool]$Directory,

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
    $expectedInheritance = if ($Directory) {
        $directoryInheritance
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

function New-DesiredAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [bool]$Directory,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path
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
    $inheritance = if ($Directory) {
        $directoryInheritance
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

function Set-ExactAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [bool]$Directory,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    $desired = New-DesiredAcl `
        -Path $Path `
        -Directory $Directory `
        -AllowedSids $AllowedSids `
        -OwnerSid $OwnerSid
    Microsoft.PowerShell.Security\Set-Acl -LiteralPath $Path -AclObject $desired
    $current = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path
    if (-not (Test-AclMatchesPolicy `
                -Acl $current `
                -Directory $Directory `
                -AllowedSids $AllowedSids `
                -OwnerSid $OwnerSid)) {
        throw "Secure launcher ACL verification failed after an attempted update."
    }
}

function Test-BytesEqual {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [byte[]]$Left,

        [Parameter(Mandatory = $true)]
        [byte[]]$Right
    )

    if ($Left.Length -ne $Right.Length) {
        return $false
    }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) {
            return $false
        }
    }
    return $true
}

function Test-BundleCompliant {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundlePath,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$SourceFiles,

        [Parameter(Mandatory = $true)]
        [byte[]]$ManifestBytes,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    if (-not (Test-Path -LiteralPath $BundlePath)) {
        return $false
    }
    $entries = @(Get-BundleEntries -BundlePath $BundlePath)
    if ($entries.Count -ne $allowedBundleNames.Count) {
        return $false
    }

    $directoryAcl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $BundlePath
    if (-not (Test-AclMatchesPolicy `
                -Acl $directoryAcl `
                -Directory $true `
                -AllowedSids $AllowedSids `
                -OwnerSid $OwnerSid)) {
        return $false
    }

    foreach ($name in $launcherFileNames) {
        $path = Join-Path $BundlePath $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $false
        }
        $bytes = [System.IO.File]::ReadAllBytes($path)
        if ((Get-Sha256Hex -Bytes $bytes) -cne [string]$SourceFiles[$name].Sha256) {
            return $false
        }
        $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $path
        if (-not (Test-AclMatchesPolicy `
                    -Acl $acl `
                    -Directory $false `
                    -AllowedSids $AllowedSids `
                    -OwnerSid $OwnerSid)) {
            return $false
        }
    }

    $manifestPath = Join-Path $BundlePath $manifestFileName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $false
    }
    $installedManifest = [System.IO.File]::ReadAllBytes($manifestPath)
    if (-not (Test-BytesEqual -Left $installedManifest -Right $ManifestBytes)) {
        return $false
    }
    $manifestAcl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $manifestPath
    return Test-AclMatchesPolicy `
        -Acl $manifestAcl `
        -Directory $false `
        -AllowedSids $AllowedSids `
        -OwnerSid $OwnerSid
}

function Get-PathAclFingerprint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path
    return [pscustomobject]@{
        Owner = $acl.GetOwner(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        Protected = [bool]$acl.AreAccessRulesProtected
        Sddl = $acl.GetSecurityDescriptorSddlForm($aclSections)
    }
}

function Get-BundleFingerprint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundlePath
    )

    $entries = @(Get-BundleEntries -BundlePath $BundlePath)
    $files = [ordered]@{}
    foreach ($name in $allowedBundleNames) {
        $path = Join-Path $BundlePath $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $files[$name] = [pscustomobject]@{ Present = $false }
            continue
        }
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $files[$name] = [pscustomobject]@{
            Present = $true
            Length = $bytes.Length
            Sha256 = Get-Sha256Hex -Bytes $bytes
            Acl = Get-PathAclFingerprint -Path $path
        }
    }
    return ([ordered]@{
            EntryCount = $entries.Count
            DirectoryAcl = Get-PathAclFingerprint -Path $BundlePath
            Files = $files
        } | ConvertTo-Json -Compress -Depth 8)
}

function Write-NewFileBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes
    )

    $stream = New-Object System.IO.FileStream(
        $Path,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

function Remove-BundleDirectorySafely {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundlePath,

        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    if (-not (Test-Path -LiteralPath $BundlePath)) {
        return
    }
    Assert-PathWithinRoot -Root $TrustedRoot -Path $BundlePath
    Assert-NoReparseComponents -Root $TrustedRoot -Path $BundlePath
    $null = @(Get-BundleEntries -BundlePath $BundlePath)
    foreach ($name in $allowedBundleNames) {
        $path = Join-Path $BundlePath $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            [System.IO.File]::Delete($path)
        }
    }
    [System.IO.Directory]::Delete($BundlePath, $false)
}

function New-StagedBundle {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$StagingPath,

        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$SourceFiles,

        [Parameter(Mandatory = $true)]
        [byte[]]$ManifestBytes,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [string]$OwnerSid
    )

    Assert-PathWithinRoot -Root $TrustedRoot -Path $StagingPath
    Assert-NoReparseComponents -Root $TrustedRoot -Path $StagingPath
    if (Test-Path -LiteralPath $StagingPath) {
        throw "The secure launcher staging path already exists."
    }
    [void][System.IO.Directory]::CreateDirectory($StagingPath)
    Set-ExactAcl `
        -Path $StagingPath `
        -Directory $true `
        -AllowedSids $AllowedSids `
        -OwnerSid $OwnerSid

    foreach ($name in $launcherFileNames) {
        $path = Join-Path $StagingPath $name
        Write-NewFileBytes -Path $path -Bytes $SourceFiles[$name].Bytes
        Set-ExactAcl `
            -Path $path `
            -Directory $false `
            -AllowedSids $AllowedSids `
            -OwnerSid $OwnerSid
    }
    $manifestPath = Join-Path $StagingPath $manifestFileName
    Write-NewFileBytes -Path $manifestPath -Bytes $ManifestBytes
    Set-ExactAcl `
        -Path $manifestPath `
        -Directory $false `
        -AllowedSids $AllowedSids `
        -OwnerSid $OwnerSid

    if (-not (Test-BundleCompliant `
                -BundlePath $StagingPath `
                -SourceFiles $SourceFiles `
                -ManifestBytes $ManifestBytes `
                -AllowedSids $AllowedSids `
                -OwnerSid $OwnerSid)) {
        throw "The staged secure launcher bundle failed verification."
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
$projectVolumeRoot = [System.IO.Path]::GetPathRoot($ProjectRoot)
if ([string]::IsNullOrWhiteSpace($projectVolumeRoot)) {
    throw "Project root could not be anchored to a filesystem root."
}
Assert-NoReparseComponents -Root $projectVolumeRoot -Path $ProjectRoot

$scriptsPath = Resolve-ExistingDirectory `
    -Path (Join-Path $ProjectRoot "scripts") `
    -Description "Project scripts directory"
Assert-PathWithinRoot -Root $ProjectRoot -Path $scriptsPath
Assert-NoReparseComponents -Root $ProjectRoot -Path $scriptsPath
$sourceFiles = Read-SourceBundle -ScriptsPath $scriptsPath

if ([string]::IsNullOrWhiteSpace($HermesHome)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is required to resolve the Hermes home directory."
    }
    $HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
}
$HermesHome = Resolve-ExistingDirectory `
    -Path $HermesHome `
    -Description "Hermes home"
$hermesVolumeRoot = [System.IO.Path]::GetPathRoot($HermesHome)
if ([string]::IsNullOrWhiteSpace($hermesVolumeRoot)) {
    throw "Hermes home could not be anchored to a filesystem root."
}
Assert-NoReparseComponents -Root $hermesVolumeRoot -Path $HermesHome

$installPath = Get-NormalizedPath -Path (
    Join-Path $HermesHome $launcherDirectoryName
)
Assert-PathWithinRoot -Root $HermesHome -Path $installPath
Assert-NoReparseComponents -Root $HermesHome -Path $installPath

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
$pluginFiles = Read-ProtectedPluginBinding `
    -HermesHomePath $HermesHome `
    -AllowedSids $allowedSids `
    -OwnerSid $operatorSid
$manifestBytes = New-ManifestBytes `
    -SourceFiles $sourceFiles `
    -PluginFiles $pluginFiles

$alreadyCompliant = Test-BundleCompliant `
    -BundlePath $installPath `
    -SourceFiles $sourceFiles `
    -ManifestBytes $manifestBytes `
    -AllowedSids $allowedSids `
    -OwnerSid $operatorSid
$changesRequired = if ($alreadyCompliant) { 0 } else { 1 }
$changesApplied = 0
$applyExecuted = $false
$rollbackPerformed = $false
$backupRetained = $false

if ($Apply -and $changesRequired -gt 0) {
    $applyExecuted = $PSCmdlet.ShouldProcess(
        $installPath,
        "transactionally install the protected secure Gateway launcher bundle"
    )
}
elseif ($Apply) {
    $applyExecuted = $true
}

if ($applyExecuted -and $changesRequired -gt 0) {
    $transactionId = [System.Guid]::NewGuid().ToString("N")
    $stagingPath = Get-NormalizedPath -Path (
        Join-Path $HermesHome ".secure-gateway-launcher.staging-$transactionId"
    )
    $backupPath = Get-NormalizedPath -Path (
        Join-Path $HermesHome ".secure-gateway-launcher.backup-$transactionId"
    )
    Assert-PathWithinRoot -Root $HermesHome -Path $stagingPath
    Assert-PathWithinRoot -Root $HermesHome -Path $backupPath
    Assert-NoReparseComponents -Root $HermesHome -Path $stagingPath
    Assert-NoReparseComponents -Root $HermesHome -Path $backupPath
    if (
        (Test-Path -LiteralPath $stagingPath) -or
        (Test-Path -LiteralPath $backupPath)
    ) {
        throw "A secure launcher transaction path already exists."
    }

    $hadExisting = Test-Path -LiteralPath $installPath -PathType Container
    $originalFingerprint = if ($hadExisting) {
        Get-BundleFingerprint -BundlePath $installPath
    }
    else {
        $null
    }
    $backupCreated = $false
    $newInstalled = $false
    try {
        New-StagedBundle `
            -StagingPath $stagingPath `
            -TrustedRoot $HermesHome `
            -SourceFiles $sourceFiles `
            -ManifestBytes $manifestBytes `
            -AllowedSids $allowedSids `
            -OwnerSid $operatorSid

        if ($hadExisting) {
            $currentFingerprint = Get-BundleFingerprint -BundlePath $installPath
            if ($currentFingerprint -cne $originalFingerprint) {
                throw "The existing secure launcher bundle changed during installation."
            }
            [System.IO.Directory]::Move($installPath, $backupPath)
            $backupCreated = $true
        }
        [System.IO.Directory]::Move($stagingPath, $installPath)
        $newInstalled = $true
        if (-not (Test-BundleCompliant `
                    -BundlePath $installPath `
                    -SourceFiles $sourceFiles `
                    -ManifestBytes $manifestBytes `
                    -AllowedSids $allowedSids `
                    -OwnerSid $operatorSid)) {
            throw "The installed secure launcher bundle failed verification."
        }
        $changesApplied = 1
    }
    catch {
        $rollbackFailed = $false
        if ($newInstalled) {
            try {
                Remove-BundleDirectorySafely `
                    -BundlePath $installPath `
                    -TrustedRoot $HermesHome
            }
            catch {
                $rollbackFailed = $true
            }
        }
        if ($backupCreated -and -not $rollbackFailed) {
            try {
                [System.IO.Directory]::Move($backupPath, $installPath)
                if ((Get-BundleFingerprint -BundlePath $installPath) -cne $originalFingerprint) {
                    $rollbackFailed = $true
                }
            }
            catch {
                $rollbackFailed = $true
            }
        }
        elseif (-not $hadExisting -and -not $rollbackFailed) {
            if (Test-Path -LiteralPath $installPath) {
                $rollbackFailed = $true
            }
        }
        if (Test-Path -LiteralPath $stagingPath) {
            try {
                Remove-BundleDirectorySafely `
                    -BundlePath $stagingPath `
                    -TrustedRoot $HermesHome
            }
            catch {
                $rollbackFailed = $true
            }
        }
        $rollbackPerformed = $newInstalled -or $backupCreated
        if ($rollbackFailed) {
            throw (
                "Secure launcher installation failed and rollback verification also " +
                "failed. Do not use the launcher until the install path is reviewed."
            )
        }
        throw "Secure launcher installation failed; the previous bundle was restored and verified."
    }

    if ($backupCreated) {
        try {
            Remove-BundleDirectorySafely `
                -BundlePath $backupPath `
                -TrustedRoot $HermesHome
        }
        catch {
            $backupRetained = $true
        }
    }
}

$compliant = Test-BundleCompliant `
    -BundlePath $installPath `
    -SourceFiles $sourceFiles `
    -ManifestBytes $manifestBytes `
    -AllowedSids $allowedSids `
    -OwnerSid $operatorSid
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
    SecureGatewayLauncherInstall = $true
    Mode = $mode
    ApplyRequested = [bool]$Apply
    ChangesRequired = $changesRequired
    ChangesApplied = $changesApplied
    Compliant = $compliant
    RollbackPerformed = $rollbackPerformed
    BackupRetained = $backupRetained
    InstallPath = $installPath
    ManifestPath = Join-Path $installPath $manifestFileName
}
