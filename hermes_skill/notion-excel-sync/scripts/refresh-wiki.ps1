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

# This command reads only the configured immutable local source. It replaces
# the protected Wiki generation atomically and never changes the source,
# Excel, or Notion.
Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments $cliArguments
