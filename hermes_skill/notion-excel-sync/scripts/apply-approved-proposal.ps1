[CmdletBinding()]
param()

throw @"
Direct apply scripts are disabled. An authenticated /nx_approve Telegram
message starts the trusted Hermes gateway worker, which alone can access the
Notion write credential and approval signing secret.
"@
