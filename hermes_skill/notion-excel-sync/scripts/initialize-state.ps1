[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [string]$ProjectRoot
)

. (Join-Path $PSScriptRoot "_common.ps1")

$configPath = Resolve-NesExistingPath -Path $Config -Description "Configuration file"
Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments @(
    "init-db",
    "--config", $configPath
)

