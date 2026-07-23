[CmdletBinding()]
param(
    [switch]$ForceFull,

    [string]$Config,

    [string]$ProjectRoot
)

. (Join-Path $PSScriptRoot "_common.ps1")

$configPath = Resolve-NesConfigPath -Config $Config -ProjectRoot $ProjectRoot
$cliArguments = @(
    "wiki-refresh",
    "--config", $configPath
)
if ($ForceFull) {
    $cliArguments += "--force-full"
}

# This command uses Microsoft Graph GET only. It replaces the protected local
# Wiki generation atomically and never changes OneDrive, Excel, or Notion.
Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments $cliArguments
