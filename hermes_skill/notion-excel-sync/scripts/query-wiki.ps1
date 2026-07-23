[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateLength(1, 1000)]
    [string]$Query,

    [ValidateSet(
        "actual-cost",
        "billing-policy",
        "government-support-evidence",
        "eligible-cost",
        "proof-documents",
        "legal-deadline",
        "procedure-rule",
        "registration-followup",
        "office-sop",
        "document-checklist",
        "template",
        "grouping-policy",
        "case-numbering",
        "data-quality"
    )]
    [string[]]$Topic = @(),

    [ValidateRange(1, 10)]
    [int]$TopK = 5,

    [switch]$VerifiedOnly,

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$EffectiveOn,

    [string]$Config,

    [string]$ProjectRoot
)

. (Join-Path $PSScriptRoot "_common.ps1")

$configPath = Resolve-NesConfigPath -Config $Config -ProjectRoot $ProjectRoot
$cliArguments = @(
    "wiki-query",
    "--config", $configPath,
    "--query=$Query",
    "--top-k", $TopK.ToString([System.Globalization.CultureInfo]::InvariantCulture)
)
foreach ($topicName in $Topic) {
    $cliArguments += @("--topic", $topicName)
}
if ($VerifiedOnly) {
    $cliArguments += "--verified-only"
}
if (-not [string]::IsNullOrWhiteSpace($EffectiveOn)) {
    $cliArguments += @("--effective-on", $EffectiveOn)
}

Invoke-NesCli -ProjectRoot $ProjectRoot -CliArguments $cliArguments
