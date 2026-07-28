<#
.SYNOPSIS
Securely validates and performs a one-shot Hermes Gateway restart on Windows.

.DESCRIPTION
This entry point runs only from the protected Hermes secure-gateway-launcher
directory. It verifies the exact owner/DACL and fixed manifest hashes for
itself and its Python worker before any token is requested.

The Notion write token is collected with a masked SecureString prompt, or from
a Windows pipe when -ReadTokenFromStdin is explicitly selected. The approval
signing secret is generated in memory and exists only in the launched Gateway
process environment. Neither value is stored in .env, a file, a command-line
argument, PowerShell history, or Windows Credential Manager.

The approval secret intentionally rotates on every secure launch. A restart or
reboot therefore cannot continue a previously signed but unused approval
receipt. Finish or reject outstanding approved/applying work before restarting,
then obtain a fresh Telegram approval after the new Gateway is ready. This
one-shot launcher does not install or modify login persistence.
#>
[CmdletBinding(DefaultParameterSetName = "Interactive")]
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
    ),

    # Trusted automation only: standard input must be a Windows pipe, not a
    # redirected disk file. The line is accumulated directly into SecureString.
    [Parameter(ParameterSetName = "Pipe")]
    [switch]$ReadTokenFromStdin,

    # Read-only validation. It never prompts for or reads a secret and never
    # calls Hermes stop/start functions.
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$pluginName = "notion-excel-sync-attestor"
$secretEnvironmentNames = @(
    "GATEWAY_RELAY_NX_NOTION_TOKEN",
    "GATEWAY_RELAY_NX_APPROVAL_SECRET"
)

function Resolve-ExistingDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "A required directory is unavailable."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "A trusted directory must not be a reparse point."
    }
    return $item.FullName
}

function Resolve-ExistingFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "A required file is unavailable."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "A trusted file must not be a reparse point."
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
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "A required path escaped its trusted root."
    }
}

function Assert-PluginAclContract {
    param([Parameter(Mandatory = $true)][string]$PluginPath)

    $acl = Get-Acl -LiteralPath $PluginPath
    if (-not $acl.AreAccessRulesProtected) {
        throw "The installed plugin ACL is not protected."
    }
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentUser) {
        throw "The current Windows identity is unavailable."
    }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier])
    if ($owner.Value -cne $currentUser.Value) {
        throw "The installed plugin owner differs from the current user."
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
        throw "The installed plugin ACL rule count differs."
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
            throw "The installed plugin ACL differs from the installer contract."
        }
        $expectedSids.Remove($sid)
    }
    if ($expectedSids.Count -ne 0) {
        throw "The installed plugin ACL is missing a required principal."
    }
}

function Assert-PluginChildAclContract {
    param([Parameter(Mandatory = $true)][string]$Path)

    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path
    if ($acl.AreAccessRulesProtected) {
        throw "An installed plugin file does not inherit its protected root ACL."
    }
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentUser) {
        throw "The current Windows identity is unavailable."
    }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier])
    if ($owner.Value -cne $currentUser.Value) {
        throw "An installed plugin file owner differs from the current user."
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
        throw "An installed plugin file ACL rule count differs."
    }
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if (
            -not $expectedSids.ContainsKey($sid) -or
            -not $rule.IsInherited -or
            $rule.AccessControlType -ne
                [Security.AccessControl.AccessControlType]::Allow -or
            $rule.FileSystemRights -ne
                [Security.AccessControl.FileSystemRights]::FullControl -or
            $rule.InheritanceFlags -ne
                [Security.AccessControl.InheritanceFlags]::None -or
            $rule.PropagationFlags -ne
                [Security.AccessControl.PropagationFlags]::None
        ) {
            throw "An installed plugin file ACL differs from policy."
        }
        $expectedSids.Remove($sid)
    }
    if ($expectedSids.Count -ne 0) {
        throw "An installed plugin file ACL is missing a required principal."
    }
}

function Assert-ProtectedPathAclContract {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Directory,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "$Description has inherited ACLs."
    }
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentUser) {
        throw "The current Windows identity is unavailable."
    }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier])
    if ($owner.Value -cne $currentUser.Value) {
        throw "$Description owner differs from the current user."
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
        throw "$Description ACL rule count differs."
    }
    $requiredInheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    else {
        [Security.AccessControl.InheritanceFlags]::None
    }
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
            throw "$Description ACL differs from policy."
        }
        $expectedSids.Remove($sid)
    }
    if ($expectedSids.Count -ne 0) {
        throw "$Description ACL is missing a required principal."
    }
}

function Assert-SensitiveFileAclContract {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-ProtectedPathAclContract `
        -Path $Path `
        -Directory $false `
        -Description "A protected Hermes configuration file"
}

function Read-StrictUtf8Text {
    param([Parameter(Mandatory = $true)][string]$Path)

    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
        $offset = 0
        if (
            $bytes.Length -ge 3 -and
            $bytes[0] -eq 0xEF -and
            $bytes[1] -eq 0xBB -and
            $bytes[2] -eq 0xBF
        ) {
            $offset = 3
        }
        $text = $strictUtf8.GetString($bytes, $offset, $bytes.Length - $offset)
    }
    catch [Text.DecoderFallbackException] {
        throw "A trusted launcher file is not valid UTF-8."
    }
    if ($text.Contains([char]0)) {
        throw "A trusted launcher file contains unsupported data."
    }
    return $text
}

function Assert-LauncherBundleContract {
    param(
        [Parameter(Mandatory = $true)][string]$LauncherRoot,
        [Parameter(Mandatory = $true)][string]$WrapperPath,
        [Parameter(Mandatory = $true)][string]$WorkerPath,
        [Parameter(Mandatory = $true)][string]$PluginPath
    )

    $manifestPath = Resolve-ExistingFile -Path (
        Join-Path $LauncherRoot "secure-launcher-manifest.json"
    )
    foreach ($path in @($WrapperPath, $WorkerPath, $manifestPath)) {
        Assert-ChildPath -Root $LauncherRoot -Path $path
    }
    Assert-ProtectedPathAclContract `
        -Path $LauncherRoot `
        -Directory $true `
        -Description "The secure launcher directory"
    foreach ($path in @($WrapperPath, $WorkerPath, $manifestPath)) {
        Assert-ProtectedPathAclContract `
            -Path $path `
            -Directory $false `
            -Description "A secure launcher file"
    }

    try {
        $manifest = Read-StrictUtf8Text -Path $manifestPath |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "The secure launcher manifest is invalid."
    }
    if (
        $null -eq $manifest -or
        $manifest -is [Array] -or
        $manifest.format -ne 1 -or
        $manifest.algorithm -cne "sha256" -or
        $null -eq $manifest.files -or
        $null -eq $manifest.plugin_files
    ) {
        throw "The secure launcher manifest contract differs."
    }
    $expectedFiles = [ordered]@{
        "restart-hermes-gateway-secure.ps1" = $WrapperPath
        "restart-hermes-gateway-secure.py" = $WorkerPath
    }
    $properties = @($manifest.files.PSObject.Properties)
    if ($properties.Count -ne $expectedFiles.Count) {
        throw "The secure launcher manifest file set differs."
    }
    foreach ($property in $properties) {
        if (
            -not $expectedFiles.Contains($property.Name) -or
            $property.Value -isnot [string] -or
            $property.Value -cnotmatch '^[0-9a-f]{64}$'
        ) {
            throw "The secure launcher manifest entry is invalid."
        }
        $actual = (
            Microsoft.PowerShell.Utility\Get-FileHash `
                -LiteralPath $expectedFiles[$property.Name] `
                -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($actual -cne $property.Value) {
            throw "The secure launcher integrity check failed."
        }
        [void]$expectedFiles.Remove($property.Name)
    }
    if ($expectedFiles.Count -ne 0) {
        throw "The secure launcher manifest is incomplete."
    }

    $expectedPluginFiles = [ordered]@{
        "plugin.yaml" = Resolve-ExistingFile -Path (
            Join-Path $PluginPath "plugin.yaml"
        )
        "__init__.py" = Resolve-ExistingFile -Path (
            Join-Path $PluginPath "__init__.py"
        )
        "runtime-manifest.json" = Resolve-ExistingFile -Path (
            Join-Path $PluginPath "runtime-manifest.json"
        )
    }
    $pluginProperties = @($manifest.plugin_files.PSObject.Properties)
    if ($pluginProperties.Count -ne $expectedPluginFiles.Count) {
        throw "The secure launcher plugin manifest file set differs."
    }
    foreach ($property in $pluginProperties) {
        if (
            -not $expectedPluginFiles.Contains($property.Name) -or
            $property.Value -isnot [string] -or
            $property.Value -cnotmatch '^[0-9a-f]{64}$'
        ) {
            throw "The secure launcher plugin manifest entry is invalid."
        }
        $pluginFile = $expectedPluginFiles[$property.Name]
        Assert-ChildPath -Root $PluginPath -Path $pluginFile
        Assert-PluginChildAclContract -Path $pluginFile
        $actual = (
            Microsoft.PowerShell.Utility\Get-FileHash `
                -LiteralPath $pluginFile `
                -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($actual -cne $property.Value) {
            throw "The installed plugin integrity check failed."
        }
        [void]$expectedPluginFiles.Remove($property.Name)
    }
    if ($expectedPluginFiles.Count -ne 0) {
        throw "The secure launcher plugin manifest is incomplete."
    }
}

function Get-UvBasePython {
    param([Parameter(Mandatory = $true)][string]$HermesRuntimeRoot)

    $pyvenvPath = Resolve-ExistingFile -Path (
        Join-Path $HermesRuntimeRoot "venv\pyvenv.cfg"
    )
    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    try {
        $text = $strictUtf8.GetString([IO.File]::ReadAllBytes($pyvenvPath))
    }
    catch [Text.DecoderFallbackException] {
        throw "Hermes pyvenv.cfg is not valid UTF-8."
    }
    if ($text -notmatch '(?im)^\s*uv\s*=\s*\S+\s*$') {
        throw "The Hermes virtual environment is not uv-managed."
    }
    $homeMatch = [regex]::Match($text, '(?im)^\s*home\s*=\s*(?<home>\S.*)\s*$')
    if (-not $homeMatch.Success) {
        throw "The uv base Python home is missing."
    }
    if (-not $env:APPDATA) {
        throw "APPDATA is unavailable."
    }
    $uvPythonRoot = Resolve-ExistingDirectory -Path (Join-Path $env:APPDATA "uv\python")
    $configuredHome = [IO.Path]::GetFullPath($homeMatch.Groups['home'].Value.Trim())
    Assert-ChildPath -Root $uvPythonRoot -Path $configuredHome
    if (-not (Test-Path -LiteralPath $configuredHome -PathType Container)) {
        throw "The uv base Python home is unavailable."
    }
    $homeItem = Get-Item -LiteralPath $configuredHome -Force
    if (($homeItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        # uv maintains an unversioned junction such as
        # cpython-3.11-windows-x86_64-none -> cpython-3.11.x-... . Resolve only
        # that documented alias and require its target to stay in uv\python.
        if ($homeItem.LinkType -cne "Junction" -or @($homeItem.Target).Count -ne 1) {
            throw "The uv base Python alias has an unsupported link type."
        }
        $target = [string]@($homeItem.Target)[0]
        if (-not [IO.Path]::IsPathRooted($target)) {
            $target = Join-Path (Split-Path -Parent $configuredHome) $target
        }
        $target = [IO.Path]::GetFullPath($target)
        Assert-ChildPath -Root $uvPythonRoot -Path $target
        $baseHome = Resolve-ExistingDirectory -Path $target
    }
    else {
        $baseHome = Resolve-ExistingDirectory -Path $configuredHome
    }
    return Resolve-ExistingFile -Path (Join-Path $baseHome "python.exe")
}

function ConvertTo-NativeQuotedArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Contains([char]0)) {
        throw "A process path contains unsupported NUL data."
    }
    # There is only one non-secret path argument. Apply the documented Windows
    # CRT escaping rule for backslashes that precede a quote or closing quote.
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Add-PipeHandleProbe {
    if (-not ("NxSecureGateway.NativeMethods" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace NxSecureGateway {
    public static class NativeMethods {
        public const int STD_INPUT_HANDLE = -10;
        public const uint FILE_TYPE_PIPE = 0x0003;
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr GetStdHandle(int nStdHandle);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint GetFileType(IntPtr hFile);
    }
}
"@
    }
}

function Read-TokenAsSecureString {
    param([switch]$FromStdin)

    if (-not $FromStdin) {
        # Windows PowerShell 5.1 decodes a UTF-8 script without BOM through
        # the active ANSI code page. Build the Korean portion from Unicode
        # code points so the protected launcher remains ASCII-only and the
        # masked prompt renders correctly on every Windows code page.
        $prompt = "Notion " + -join [char[]]@(
            0xC4F0, 0xAE30, 0x0020, 0xD1B5,
            0xD569, 0x0020, 0xD1A0, 0xD070
        )
        return Read-Host $prompt -AsSecureString
    }
    Add-PipeHandleProbe
    $handle = [NxSecureGateway.NativeMethods]::GetStdHandle(
        [NxSecureGateway.NativeMethods]::STD_INPUT_HANDLE
    )
    $fileType = [NxSecureGateway.NativeMethods]::GetFileType($handle)
    if (
        -not [Console]::IsInputRedirected -or
        $fileType -ne [NxSecureGateway.NativeMethods]::FILE_TYPE_PIPE
    ) {
        throw "Trusted automation input must be supplied through a pipe."
    }

    $secure = New-Object Security.SecureString
    try {
        while ($true) {
            $value = [Console]::In.Read()
            if ($value -lt 0 -or $value -eq 10) {
                break
            }
            if ($value -eq 13) {
                continue
            }
            if ($secure.Length -ge 512) {
                throw "The supplied token is too long."
            }
            [void]$secure.AppendChar([char]$value)
        }
        $secure.MakeReadOnly()
        return $secure
    }
    catch {
        $secure.Dispose()
        throw
    }
}

function Assert-SecureTokenContract {
    param([Parameter(Mandatory = $true)][Security.SecureString]$SecureToken)

    if ($SecureToken.Length -lt 20 -or $SecureToken.Length -gt 512) {
        throw "The supplied token does not meet the required length."
    }
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
        for ($index = 0; $index -lt $SecureToken.Length; $index += 1) {
            $value = [Runtime.InteropServices.Marshal]::ReadInt16(
                $pointer,
                $index * 2
            )
            if ($value -lt 0x21 -or $value -gt 0x7E) {
                throw "The supplied token contains unsupported characters."
            }
        }
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

function Invoke-SecureWorker {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("preflight", "restart")]
        [string]$Mode,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$WorkerPath,
        [Parameter(Mandatory = $true)][string]$HermesHomePath,
        [Parameter(Mandatory = $true)][string]$HermesRuntimeRoot,
        [Parameter(Mandatory = $true)][string]$ProjectRootPath,
        [Parameter(Mandatory = $true)][string]$ConfigurationPath,
        [Security.SecureString]$SecureToken
    )

    if ($Mode -eq "restart") {
        if ($null -eq $SecureToken) {
            throw "The secure restart token is unavailable."
        }
        Assert-SecureTokenContract -SecureToken $SecureToken
    }
    elseif ($null -ne $SecureToken) {
        throw "Preflight must not receive a secret."
    }

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $PythonPath
    $quotedWorker = ConvertTo-NativeQuotedArgument -Value $WorkerPath
    $startInfo.Arguments = "-I -S $quotedWorker"
    $startInfo.WorkingDirectory = $HermesRuntimeRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    foreach ($name in $secretEnvironmentNames) {
        [void]$startInfo.EnvironmentVariables.Remove($name)
    }
    [void]$startInfo.EnvironmentVariables.Remove("NOTION_EXCEL_SYNC_DEV_PLUGIN")
    $startInfo.EnvironmentVariables["PYTHONNOUSERSITE"] = "1"
    $startInfo.EnvironmentVariables["NX_SECURE_GATEWAY_MODE"] = $Mode
    $startInfo.EnvironmentVariables["NX_SECURE_GATEWAY_HERMES_HOME"] = $HermesHomePath
    $startInfo.EnvironmentVariables["NX_SECURE_GATEWAY_RUNTIME"] = $HermesRuntimeRoot
    $startInfo.EnvironmentVariables["NX_SECURE_GATEWAY_PROJECT_ROOT"] = $ProjectRootPath
    $startInfo.EnvironmentVariables["NX_SECURE_GATEWAY_CONFIG"] = $ConfigurationPath

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $pointer = [IntPtr]::Zero
    $started = $false
    $committed = $false
    try {
        $started = $process.Start()
        if (-not $started) {
            throw "The secure worker could not be started."
        }
        if ($Mode -eq "restart") {
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
                $SecureToken
            )
            for ($index = 0; $index -lt $SecureToken.Length; $index += 1) {
                $character = [char][Runtime.InteropServices.Marshal]::ReadInt16(
                    $pointer,
                    $index * 2
                )
                $process.StandardInput.Write($character)
                $character = [char]0
            }
            $process.StandardInput.Write("`n")
            $process.StandardInput.Flush()
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
            $pointer = [IntPtr]::Zero
        }
        else {
            $process.StandardInput.Dispose()
        }
        $errorTask = $process.StandardError.ReadToEndAsync()
        if ($Mode -eq "restart") {
            # PREPARED means the token is validated and the old Gateway remains
            # untouched. Mark the worker irreversible/no-kill *before* sending
            # COMMIT, closing the acknowledgement race completely.
            $preparedTask = $process.StandardOutput.ReadLineAsync()
            if (-not $preparedTask.Wait(30000)) {
                try {
                    $process.Kill()
                    [void]$process.WaitForExit(5000)
                }
                catch {
                    # The fixed pre-commit timeout remains the only output.
                }
                throw "The secure worker timed out before restart commit."
            }
            $preparedLine = $preparedTask.Result
            if ($preparedLine -cne "STATUS=PREPARED") {
                [void]$process.WaitForExit(5000)
                throw "The secure worker did not enter the prepared state."
            }
            $committed = $true
            $process.StandardInput.Write("COMMIT`n")
            $process.StandardInput.Dispose()
            $outputTask = $process.StandardOutput.ReadToEndAsync()
            # Do not impose a parent-side kill timeout after COMMIT. The
            # Hermes operations used by the worker have their own finite drain,
            # task-control, absence, and readiness bounds. Killing here could
            # strand the machine after the old Gateway has already stopped.
            $process.WaitForExit()
            $standardOutput = $outputTask.Result
        }
        else {
            $outputTask = $process.StandardOutput.ReadToEndAsync()
            if (-not $process.WaitForExit(60000)) {
                try {
                    $process.Kill()
                    [void]$process.WaitForExit(5000)
                }
                catch {
                    # Preflight is read-only, so forced termination is safe.
                }
                throw "The secure preflight worker timed out."
            }
            $standardOutput = $outputTask.Result
        }
        # Read and discard dependency diagnostics. They are never relayed to the
        # console because only the fixed worker protocol is trusted for output.
        $null = $errorTask.Result
        if ($process.ExitCode -ne 0) {
            throw ("Secure worker rejected the operation (code {0})." -f $process.ExitCode)
        }
        return $standardOutput
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        if ($started) {
            try {
                $process.StandardInput.Dispose()
            }
            catch {
                # The worker may already have closed its pipe.
            }
            try {
                if (-not $committed -and -not $process.HasExited) {
                    $process.Kill()
                    [void]$process.WaitForExit(5000)
                }
            }
            catch {
                # The process may have exited between the liveness check and kill.
            }
        }
        $process.Dispose()
    }
}

if ($DryRun -and $ReadTokenFromStdin) {
    throw "DryRun never accepts secret input."
}

if (-not $ProjectRoot) {
    throw "ProjectRoot is required for the installed secure launcher."
}
$ProjectRoot = Resolve-ExistingDirectory -Path $ProjectRoot
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ProjectRoot "config\sync.local.json"
}
$ConfigPath = Resolve-ExistingFile -Path $ConfigPath
Assert-ChildPath -Root $ProjectRoot -Path $ConfigPath
Assert-ProtectedPathAclContract `
    -Path $ConfigPath `
    -Directory $false `
    -Description "The sync configuration file"

$HermesHome = Resolve-ExistingDirectory -Path $HermesHome
$pluginPath = Resolve-ExistingDirectory -Path (
    Join-Path $HermesHome "plugins\$pluginName"
)
Assert-PluginAclContract -PluginPath $pluginPath
$wrapperPath = Resolve-ExistingFile -Path $MyInvocation.MyCommand.Path
$launcherRoot = Resolve-ExistingDirectory -Path (Split-Path -Parent $wrapperPath)
$expectedLauncherRoot = [IO.Path]::GetFullPath(
    (Join-Path $HermesHome "secure-gateway-launcher")
).TrimEnd('\')
if (
    -not $launcherRoot.TrimEnd('\').Equals(
        $expectedLauncherRoot,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "The secure launcher is outside its protected installation path."
}
$workerPath = Resolve-ExistingFile -Path (
    Join-Path $launcherRoot "restart-hermes-gateway-secure.py"
)
Assert-LauncherBundleContract `
    -LauncherRoot $launcherRoot `
    -WrapperPath $wrapperPath `
    -WorkerPath $workerPath `
    -PluginPath $pluginPath
$hermesEnvironmentPath = Resolve-ExistingFile -Path (
    Join-Path $HermesHome ".env"
)
Assert-SensitiveFileAclContract -Path $hermesEnvironmentPath
$HermesRuntimeRoot = Resolve-ExistingDirectory -Path (
    Join-Path $HermesHome "hermes-agent"
)
$basePython = Get-UvBasePython -HermesRuntimeRoot $HermesRuntimeRoot

$preflight = Invoke-SecureWorker `
    -Mode preflight `
    -PythonPath $basePython `
    -WorkerPath $workerPath `
    -HermesHomePath $HermesHome `
    -HermesRuntimeRoot $HermesRuntimeRoot `
    -ProjectRootPath $ProjectRoot `
    -ConfigurationPath $ConfigPath
if ($preflight -notmatch '^STATUS=PREFLIGHT_OK\r?\n?$') {
    throw "The secure worker returned an invalid preflight response."
}

if ($DryRun) {
    Write-Output "SecureGatewayPreflight : True"
    Write-Output "GatewayChanged         : False"
    Write-Output "SecretsRead            : False"
    return
}

$token = $null
try {
    $token = Read-TokenAsSecureString -FromStdin:$ReadTokenFromStdin
    Assert-PluginAclContract -PluginPath $pluginPath
    $result = Invoke-SecureWorker `
        -Mode restart `
        -PythonPath $basePython `
        -WorkerPath $workerPath `
        -HermesHomePath $HermesHome `
        -HermesRuntimeRoot $HermesRuntimeRoot `
        -ProjectRootPath $ProjectRoot `
        -ConfigurationPath $ConfigPath `
        -SecureToken $token
    $match = [regex]::Match(
        $result,
        '^STATUS=READY\r?\nPID=(?<pid>[1-9][0-9]*)\r?\n?$'
    )
    if (-not $match.Success) {
        throw "The secure worker returned an invalid restart response."
    }
    Write-Output "SecureGatewayRestarted : True"
    Write-Output ("GatewayPid             : {0}" -f $match.Groups['pid'].Value)
    Write-Output "SecretStorageModified  : False"
    Write-Output "GatewayProcessOnly     : True"
    Write-Output "ReceiptContinuity      : RotatedOnRestart"
}
finally {
    if ($null -ne $token) {
        $token.Dispose()
        $token = $null
    }
}
