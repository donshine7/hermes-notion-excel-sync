[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [Parameter(Mandatory = $true)]
    [string]$ProposalId,

    [Parameter(Mandatory = $true)]
    [string]$TelegramUserId,

    [Parameter(Mandatory = $true)]
    [string]$ChatId,

    [string]$ProjectRoot
)

throw "Direct recovery is disabled. Send /nx_recover from the proposal owner's original Telegram chat."

. (Join-Path $PSScriptRoot "_common.ps1")

$configPath = Resolve-NesExistingPath -Path $Config -Description "Configuration file"
Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments @(
    "recover",
    "--config", $configPath,
    "--proposal-id", $ProposalId,
    "--telegram-user-id", $TelegramUserId,
    "--chat-id", $ChatId
)
