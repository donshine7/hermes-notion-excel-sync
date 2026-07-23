[CmdletBinding()]
param(
    [string]$Config,

    [string]$ProjectRoot
)

. (Join-Path $PSScriptRoot "_common.ps1")

$configPath = Resolve-NesConfigPath -Config $Config -ProjectRoot $ProjectRoot
Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments @(
    "wiki-status",
    "--config", $configPath
)
