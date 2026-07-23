[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [Parameter(Mandatory = $true)]
    [string]$ProposalId,

    [Parameter(Mandatory = $true)]
    [string]$OperationId,

    [Parameter(Mandatory = $true)]
    [ValidateSet("apply", "edit", "exclude", "defer")]
    [string]$Action,

    [string]$ValueJson,

    [string]$ProjectRoot
)

throw "Direct edit is disabled. Send /nx_set from the proposal owner's original Telegram chat."

. (Join-Path $PSScriptRoot "_common.ps1")

if ($Action -eq "edit" -and [string]::IsNullOrWhiteSpace($ValueJson)) {
    throw "ValueJson is required when Action is edit."
}
if ($Action -ne "edit" -and -not [string]::IsNullOrWhiteSpace($ValueJson)) {
    throw "ValueJson is allowed only when Action is edit."
}

$configPath = Resolve-NesExistingPath -Path $Config -Description "Configuration file"
$cliArguments = @(
    "edit",
    "--config", $configPath,
    "--proposal-id", $ProposalId,
    "--operation-id", $OperationId,
    "--action", $Action
)
if ($Action -eq "edit") {
    $cliArguments += @("--value-json", $ValueJson)
}

Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments $cliArguments
