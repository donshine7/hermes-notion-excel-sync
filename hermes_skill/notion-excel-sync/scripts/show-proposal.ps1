[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [Parameter(Mandatory = $true)]
    [string]$ProposalId,

    [string]$ProjectRoot
)

. (Join-Path $PSScriptRoot "_common.ps1")

$configPath = Resolve-NesExistingPath -Path $Config -Description "Configuration file"
Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments @(
    "show",
    "--config", $configPath,
    "--proposal-id", $ProposalId
)

