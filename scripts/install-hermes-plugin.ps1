[CmdletBinding()]
param(
    [string]$ProjectRoot = "",

    [string]$DependencySitePackages = "",

    [string]$HermesHome = $(
        if ($env:HERMES_HOME) {
            $env:HERMES_HOME
        }
        else {
            Join-Path $env:LOCALAPPDATA "hermes"
        }
    ),

    [string]$HermesRuntimeRoot = "",

    [switch]$Enable,

    # Development/CI escape hatch. Production installations must retain ACL hardening.
    [switch]$SkipAclHardening
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) {
        throw "Cannot determine the installer script path; pass -ProjectRoot explicitly."
    }
    $scriptDirectory = Split-Path -Parent $scriptPath
    $ProjectRoot = Split-Path -Parent $scriptDirectory
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith(
            $prefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Refusing to manage a path outside the intended root: $resolvedPath"
    }
}

function Get-RelativeRuntimePath {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$FilePath
    )

    $rootPath = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $fullPath = [System.IO.Path]::GetFullPath($FilePath)
    $prefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith(
            $prefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Runtime file escaped the staging directory: $fullPath"
    }
    return $fullPath.Substring($prefix.Length).Replace('\', '/')
}

function Write-RuntimeManifest {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$VendoredDistributions
    )

    $hashes = [ordered]@{}
    $files = Get-ChildItem -LiteralPath $RuntimeRoot -Recurse -File |
        Sort-Object FullName
    foreach ($file in $files) {
        $relative = Get-RelativeRuntimePath `
            -RuntimeRoot $RuntimeRoot `
            -FilePath $file.FullName
        $hashes[$relative] = (
            Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
    if ($hashes.Count -eq 0) {
        throw "Refusing to create an empty runtime manifest."
    }
    if (-not $hashes.Contains("notion_excel_sync/security/hermes_gateway.py")) {
        throw "Trusted Hermes gateway module is missing from the runtime package."
    }
    $manifest = [ordered]@{
        format = 1
        algorithm = "sha256"
        package = "notion_excel_sync"
        distributions = $VendoredDistributions
        host_requirements = [ordered]@{
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
        }
        files = $hashes
    }
    $json = $manifest | ConvertTo-Json -Depth 8
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $ManifestPath,
        $json + [Environment]::NewLine,
        $encoding
    )
}

function Get-NormalizedDistributionName {
    param([Parameter(Mandatory = $true)][string]$Name)

    return (($Name.ToLowerInvariant() -replace '[_.]+', '-') -replace '-+', '-')
}

function Get-PythonDistributionMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$SitePackages,
        [Parameter(Mandatory = $true)][string]$DistributionName
    )

    $normalized = Get-NormalizedDistributionName -Name $DistributionName
    $metadataDirectories = @(
        Get-ChildItem -LiteralPath $SitePackages -Directory |
            Where-Object {
                $_.Name.EndsWith('.dist-info') -and
                (Get-NormalizedDistributionName -Name (
                    $_.Name.Substring(0, $_.Name.Length - '.dist-info'.Length) -replace '-[0-9].*$', ''
                )) -eq $normalized
            }
    )
    if ($metadataDirectories.Count -ne 1) {
        throw (
            "Expected exactly one dist-info directory for {0}; found {1}." -f
            $DistributionName,
            $metadataDirectories.Count
        )
    }

    $metadataPath = Join-Path $metadataDirectories[0].FullName "METADATA"
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        throw "Distribution metadata is missing: $metadataPath"
    }
    $metadataText = Get-Content -LiteralPath $metadataPath -Raw
    $nameMatch = [regex]::Match($metadataText, '(?m)^Name:\s*([^\r\n]+)\s*$')
    $versionMatch = [regex]::Match($metadataText, '(?m)^Version:\s*([^\r\n]+)\s*$')
    if (-not $nameMatch.Success -or -not $versionMatch.Success) {
        throw "Distribution metadata is incomplete: $metadataPath"
    }
    $metadataName = $nameMatch.Groups[1].Value.Trim()
    $version = $versionMatch.Groups[1].Value.Trim()
    if ((Get-NormalizedDistributionName -Name $metadataName) -ne $normalized) {
        throw "Distribution name mismatch in metadata: $metadataPath"
    }
    if ($version -notmatch '^\d+(?:\.\d+)*(?:[A-Za-z0-9._+-]*)$') {
        throw "Unsafe distribution version in metadata: $metadataPath"
    }
    return [pscustomobject]@{
        Name = $metadataName
        Version = $version
        Directory = $metadataDirectories[0].FullName
    }
}

function Copy-VendoredPythonDistribution {
    param(
        [Parameter(Mandatory = $true)][string]$SitePackages,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$DistributionName,
        [Parameter(Mandatory = $true)][string]$ImportDirectory,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    $packageSource = Join-Path $SitePackages $ImportDirectory
    if (-not (Test-Path -LiteralPath $packageSource -PathType Container)) {
        throw "Required Python package is missing: $packageSource"
    }

    $metadata = Get-PythonDistributionMetadata `
        -SitePackages $SitePackages `
        -DistributionName $DistributionName
    if ($metadata.Version -cne $ExpectedVersion) {
        throw (
            "Unsupported vendored distribution version: {0} {1}; expected {2}." -f
            $DistributionName,
            $metadata.Version,
            $ExpectedVersion
        )
    }

    $packageDestination = Join-Path $RuntimeRoot $ImportDirectory
    $metadataDirectory = Get-Item -LiteralPath $metadata.Directory
    $metadataDestination = Join-Path $RuntimeRoot $metadataDirectory.Name
    if (
        (Test-Path -LiteralPath $packageDestination) -or
        (Test-Path -LiteralPath $metadataDestination)
    ) {
        throw "Vendored distribution destination already exists: $DistributionName"
    }
    Copy-Item -LiteralPath $packageSource -Destination $packageDestination -Recurse
    Copy-Item `
        -LiteralPath $metadata.Directory `
        -Destination $metadataDestination `
        -Recurse

    $nativeFiles = @(
        Get-ChildItem -LiteralPath $packageDestination -Recurse -File |
            Where-Object { $_.Extension -in @('.pyd', '.dll', '.so', '.dylib') }
    )
    if ($nativeFiles.Count -ne 0) {
        throw "Refusing to vendor a native Python distribution: $DistributionName"
    }
    return $metadata.Version
}

function Assert-HermesRuntimeDependencies {
    param([Parameter(Mandatory = $true)][string]$RuntimeRoot)

    $sitePackages = Join-Path $RuntimeRoot "venv\Lib\site-packages"
    if (-not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
        throw "Hermes runtime site-packages directory is missing: $sitePackages"
    }
    $requirements = [ordered]@{
        "hermes-agent" = "0.18.2"
        requests = "2.33.0"
        urllib3 = "2.7.0"
        certifi = "2026.5.20"
        "charset-normalizer" = "3.4.4"
        idna = "3.15"
        PyJWT = "2.13.0"
        cryptography = "46.0.7"
        cffi = "2.0.0"
        pycparser = "3.0"
        Pillow = "12.2.0"
        defusedxml = "0.7.1"
        portalocker = "3.2.0"
    }
    foreach ($requirement in $requirements.GetEnumerator()) {
        $metadata = Get-PythonDistributionMetadata `
            -SitePackages $sitePackages `
            -DistributionName $requirement.Key
        if ($metadata.Version -cne $requirement.Value) {
            throw (
                "Unsupported Hermes dependency version: {0} {1}; expected {2}." -f
                $requirement.Key,
                $metadata.Version,
                $requirement.Value
            )
        }
    }
    return (Resolve-Path -LiteralPath $sitePackages -ErrorAction Stop).Path
}

function Invoke-HermesPluginSmoke {
    param(
        [Parameter(Mandatory = $true)][string]$PluginPath,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$HermesRuntime,
        [Parameter(Mandatory = $true)][string]$SmokeScript
    )

    $venvConfig = Join-Path $HermesRuntime "venv\pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $venvConfig -PathType Leaf)) {
        throw "Hermes Python configuration is missing: $venvConfig"
    }
    $homeLine = Get-Content -LiteralPath $venvConfig |
        Where-Object { $_ -match '^home\s*=\s*(.+?)\s*$' } |
        Select-Object -First 1
    $homeMatch = [regex]::Match([string]$homeLine, '^home\s*=\s*(.+?)\s*$')
    if (-not $homeMatch.Success) {
        throw "Hermes Python home is missing from: $venvConfig"
    }
    $basePython = Join-Path $homeMatch.Groups[1].Value.Trim() "python.exe"
    if (-not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
        throw "Hermes base Python is missing: $basePython"
    }

    $output = @(
        & $basePython -I $SmokeScript `
            --plugin $PluginPath `
            --project-root $ProjectRoot `
            --hermes-runtime $HermesRuntime 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Hermes plugin production smoke failed: $($output -join [Environment]::NewLine)"
    }
    $payload = ($output -join [Environment]::NewLine) | ConvertFrom-Json
    if (
        $payload.mode -ne "vendored" -or
        $payload.openpyxl_lxml -ne $false -or
        $payload.openpyxl_defusedxml -ne $true
    ) {
        throw "Hermes plugin production smoke returned an unsafe result."
    }
    return $payload
}

function Test-RuntimeManifest {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath
    )

    $manifest = Get-Content -LiteralPath $ManifestPath -Raw |
        ConvertFrom-Json
    if ($manifest.format -ne 1 -or $manifest.algorithm -ne "sha256") {
        throw "Generated runtime manifest has an unsupported format."
    }
    $expected = @{}
    foreach ($property in $manifest.files.PSObject.Properties) {
        $expected[$property.Name] = [string]$property.Value
    }
    $actual = @{}
    foreach ($file in Get-ChildItem -LiteralPath $RuntimeRoot -Recurse -File) {
        $relative = Get-RelativeRuntimePath `
            -RuntimeRoot $RuntimeRoot `
            -FilePath $file.FullName
        $actual[$relative] = (
            Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
    if ($actual.Count -ne $expected.Count) {
        throw "Generated runtime manifest file count does not match the runtime."
    }
    foreach ($relative in $expected.Keys) {
        if (-not $actual.ContainsKey($relative)) {
            throw "Generated runtime manifest refers to a missing file: $relative"
        }
        if ($actual[$relative] -cne $expected[$relative]) {
            throw "Generated runtime manifest hash mismatch: $relative"
        }
    }
}

function Protect-PluginAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentUser) {
        throw "Cannot determine the Windows identity used to install the plugin."
    }
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetOwner($currentUser)
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
    $principals = @(
        $currentUser,
        (New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")),
        (New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544"))
    )
    foreach ($principal in $principals) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $principal,
            $fullControl,
            $inheritance,
            $propagation,
            $allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
    $verified = Get-Acl -LiteralPath $Path
    if (-not $verified.AreAccessRulesProtected) {
        throw "Plugin ACL inheritance is still enabled after hardening."
    }
}

$root = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
$dependencyRoot = if ($DependencySitePackages) {
    (Resolve-Path -LiteralPath $DependencySitePackages -ErrorAction Stop).Path
}
else {
    (Resolve-Path -LiteralPath (
        Join-Path $root ".venv\Lib\site-packages"
    ) -ErrorAction Stop).Path
}
$hermesRuntime = if ($HermesRuntimeRoot) {
    (Resolve-Path -LiteralPath $HermesRuntimeRoot -ErrorAction Stop).Path
}
else {
    (Resolve-Path -LiteralPath (
        Join-Path $HermesHome "hermes-agent"
    ) -ErrorAction Stop).Path
}
$hermesSitePackages = Assert-HermesRuntimeDependencies -RuntimeRoot $hermesRuntime
$source = Join-Path $root ".hermes\plugins\notion-excel-sync-attestor"
if (-not (Test-Path -LiteralPath (Join-Path $source "plugin.yaml") -PathType Leaf)) {
    throw "Hermes plugin manifest not found: $source"
}
$runtimeSource = Join-Path $root "src\notion_excel_sync"
if (-not (Test-Path -LiteralPath (
            Join-Path $runtimeSource "security\hermes_gateway.py"
        ) -PathType Leaf)) {
    throw "Trusted runtime package not found: $runtimeSource"
}
$skillSource = Join-Path $root "hermes_skill\notion-excel-sync"
if (-not (Test-Path -LiteralPath (Join-Path $skillSource "SKILL.md") -PathType Leaf)) {
    throw "Hermes skill not found: $skillSource"
}
$smokeScript = Join-Path $root "scripts\smoke-hermes-plugin.py"
if (-not (Test-Path -LiteralPath $smokeScript -PathType Leaf)) {
    throw "Hermes production smoke script not found: $smokeScript"
}

$pluginsRoot = [System.IO.Path]::GetFullPath((Join-Path $HermesHome "plugins"))
$destination = Join-Path $pluginsRoot "notion-excel-sync-attestor"
$installId = [Guid]::NewGuid().ToString("N")
$staging = Join-Path $pluginsRoot ".notion-excel-sync-attestor.staging-$installId"
$backup = Join-Path $pluginsRoot ".notion-excel-sync-attestor.backup-$installId"
Assert-ChildPath -Root $pluginsRoot -Path $destination
Assert-ChildPath -Root $pluginsRoot -Path $staging
Assert-ChildPath -Root $pluginsRoot -Path $backup
New-Item -ItemType Directory -Path $pluginsRoot -Force | Out-Null

$skillsRoot = [System.IO.Path]::GetFullPath((Join-Path $HermesHome "skills"))
$skillDestination = Join-Path $skillsRoot "notion-excel-sync"
$skillStaging = Join-Path $skillsRoot ".notion-excel-sync.staging-$installId"
$skillBackup = Join-Path $skillsRoot ".notion-excel-sync.backup-$installId"
Assert-ChildPath -Root $skillsRoot -Path $skillDestination
Assert-ChildPath -Root $skillsRoot -Path $skillStaging
Assert-ChildPath -Root $skillsRoot -Path $skillBackup
New-Item -ItemType Directory -Path $skillsRoot -Force | Out-Null
$enabledByInstaller = $false

try {
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $source "plugin.yaml") `
        -Destination $staging -Force
    Copy-Item -LiteralPath (Join-Path $source "__init__.py") `
        -Destination $staging -Force
    New-Item -ItemType Directory -Path $skillStaging -Force | Out-Null
    Copy-Item -Path (Join-Path $skillSource "*") `
        -Destination $skillStaging -Recurse -Force
    $runtimeDestination = Join-Path $staging "runtime"
    New-Item -ItemType Directory -Path $runtimeDestination -Force | Out-Null
    Copy-Item -LiteralPath $runtimeSource `
        -Destination $runtimeDestination -Recurse -Force

    $vendoredDistributions = [ordered]@{}
    $dependencySpecifications = @(
        @{ Distribution = "openpyxl"; ImportDirectory = "openpyxl"; Version = "3.1.5" },
        @{ Distribution = "et-xmlfile"; ImportDirectory = "et_xmlfile"; Version = "2.0.0" },
        @{ Distribution = "msal"; ImportDirectory = "msal"; Version = "1.37.0" },
        @{
            Distribution = "msal-extensions"
            ImportDirectory = "msal_extensions"
            Version = "1.3.1"
        }
    )
    foreach ($specification in $dependencySpecifications) {
        $version = Copy-VendoredPythonDistribution `
            -SitePackages $dependencyRoot `
            -RuntimeRoot $runtimeDestination `
            -DistributionName $specification.Distribution `
            -ImportDirectory $specification.ImportDirectory `
            -ExpectedVersion $specification.Version
        $vendoredDistributions[$specification.Distribution] = $version
    }

    Get-ChildItem -LiteralPath $runtimeDestination -Recurse -Directory |
        Where-Object Name -EQ "__pycache__" |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $runtimeDestination -Recurse -File |
        Where-Object Extension -EQ ".pyc" |
        Remove-Item -Force

    $manifestPath = Join-Path $staging "runtime-manifest.json"
    Write-RuntimeManifest `
        -RuntimeRoot $runtimeDestination `
        -ManifestPath $manifestPath `
        -VendoredDistributions $vendoredDistributions
    Test-RuntimeManifest `
        -RuntimeRoot $runtimeDestination `
        -ManifestPath $manifestPath
    $smokeResult = Invoke-HermesPluginSmoke `
        -PluginPath (Join-Path $staging "__init__.py") `
        -ProjectRoot $root `
        -HermesRuntime $hermesRuntime `
        -SmokeScript $smokeScript

    if (-not $SkipAclHardening) {
        Protect-PluginAcl -Path $staging
    }

    $pluginBackedUp = $false
    $skillBackedUp = $false
    $pluginInstalled = $false
    $skillInstalled = $false
    try {
        if (Test-Path -LiteralPath $destination) {
            Move-Item -LiteralPath $destination -Destination $backup
            $pluginBackedUp = $true
        }
        if (Test-Path -LiteralPath $skillDestination) {
            Move-Item -LiteralPath $skillDestination -Destination $skillBackup
            $skillBackedUp = $true
        }
        Move-Item -LiteralPath $staging -Destination $destination
        $pluginInstalled = $true
        Move-Item -LiteralPath $skillStaging -Destination $skillDestination
        $skillInstalled = $true
        if (-not $SkipAclHardening) {
            $installedAcl = Get-Acl -LiteralPath $destination
            if (-not $installedAcl.AreAccessRulesProtected) {
                throw "Installed plugin ACL is not protected."
            }
        }
        if ($Enable) {
            $hermes = Get-Command hermes -ErrorAction Stop
            & $hermes.Source plugins enable notion-excel-sync-attestor
            if ($LASTEXITCODE -ne 0) {
                throw "Hermes could not enable notion-excel-sync-attestor."
            }
            $enabledByInstaller = $true
        }
    }
    catch {
        if ($pluginInstalled -and (Test-Path -LiteralPath $destination)) {
            Remove-Item -LiteralPath $destination -Recurse -Force
        }
        if ($skillInstalled -and (Test-Path -LiteralPath $skillDestination)) {
            Remove-Item -LiteralPath $skillDestination -Recurse -Force
        }
        if ($pluginBackedUp -and (Test-Path -LiteralPath $backup)) {
            Move-Item -LiteralPath $backup -Destination $destination
        }
        if ($skillBackedUp -and (Test-Path -LiteralPath $skillBackup)) {
            Move-Item -LiteralPath $skillBackup -Destination $skillDestination
        }
        throw
    }
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
    if (Test-Path -LiteralPath $skillBackup) {
        Remove-Item -LiteralPath $skillBackup -Recurse -Force
    }
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    if (Test-Path -LiteralPath $skillStaging) {
        Remove-Item -LiteralPath $skillStaging -Recurse -Force
    }
}

[pscustomobject]@{
    InstalledPlugin = $destination
    InstalledRuntime = (Join-Path $destination "runtime\notion_excel_sync")
    RuntimeManifest = (Join-Path $destination "runtime-manifest.json")
    VendoredDistributions = $vendoredDistributions
    RuntimeAclHardened = -not [bool]$SkipAclHardening
    InstalledSkill = $skillDestination
    HermesRuntimeRoot = $hermesRuntime
    HermesSitePackages = $hermesSitePackages
    ProductionSmoke = $smokeResult
    EnableRequested = [bool]$Enable
    EnabledByInstaller = $enabledByInstaller
    ConfigProjectRoot = $root
    RequiredConfig = (Join-Path $root "config\sync.local.json")
    RestartRequired = $true
}
