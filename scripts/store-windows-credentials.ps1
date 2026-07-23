[CmdletBinding()]
param(
    [string[]]$Names = @(
        "NOTION_READ_TOKEN"
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Windows Credential Manager is available only on Windows."
}

if ($null -eq ("NxCredentialNative" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class NxCredentialNative
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL
    {
        public UInt32 Flags;
        public UInt32 Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public UInt32 CredentialBlobSize;
        public IntPtr CredentialBlob;
        public UInt32 Persist;
        public UInt32 AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("Advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode,
        SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CredWrite(ref CREDENTIAL credential, UInt32 flags);
}
"@
}

$targetPrefix = "NotionExcelSync:"
$uniqueNames = @($Names | Select-Object -Unique)
if ($uniqueNames.Count -eq 0) {
    throw "At least one credential name is required."
}

foreach ($name in $uniqueNames) {
    if ($name -ne "NOTION_READ_TOKEN") {
        throw (
            "Unsupported Credential Manager name: $name. Only NOTION_READ_TOKEN " +
            "is allowed. Gateway-only write/signing secrets must remain in the " +
            "protected Hermes Gateway process environment."
        )
    }

    $target = $targetPrefix + $name
    $secret = Read-Host "Enter secret for Windows credential '$target'" -AsSecureString
    if ($secret.Length -eq 0) {
        $secret.Dispose()
        throw "Empty secrets are not allowed for target: $target"
    }

    $secretPointer = [IntPtr]::Zero
    try {
        # The plaintext exists only briefly in unmanaged memory. It is never placed
        # in a command argument, environment variable, transcript, or output stream.
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
        $blobSize = [Runtime.InteropServices.Marshal]::ReadInt32($secretPointer, -4)
        if ($blobSize -gt 2560) {
            throw "Secret exceeds the Windows Generic Credential blob limit: $target"
        }

        $credential = New-Object NxCredentialNative+CREDENTIAL
        $credential.Flags = 0
        $credential.Type = 1
        $credential.TargetName = $target
        $credential.Comment = "Notion Excel Sync protected application secret"
        $credential.CredentialBlobSize = [uint32]$blobSize
        $credential.CredentialBlob = $secretPointer
        $credential.Persist = 2
        $credential.AttributeCount = 0
        $credential.Attributes = [IntPtr]::Zero
        $credential.TargetAlias = $null
        $credential.UserName = [Environment]::UserName

        if (-not [NxCredentialNative]::CredWrite([ref]$credential, 0)) {
            $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw "CredWriteW failed for target '$target' with Windows error $errorCode"
        }
    }
    finally {
        if ($secretPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
        }
        $secret.Dispose()
    }

    Write-Host "Stored Windows Generic Credential target: $target (value not displayed)"
}
