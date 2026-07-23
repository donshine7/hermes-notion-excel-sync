[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [Parameter(Mandatory = $true)]
    [string]$TelegramUserId,

    [Parameter(Mandatory = $true)]
    [string]$ChatId,

    [string]$ProjectRoot
)

throw "Direct prepare is disabled. Send /nx_sync from an authorized Telegram account."

. (Join-Path $PSScriptRoot "_common.ps1")

$configPath = Resolve-NesExistingPath -Path $Config -Description "Configuration file"
Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments @(
    "prepare",
    "--config", $configPath,
    "--telegram-user-id", $TelegramUserId,
    "--chat-id", $ChatId
)
