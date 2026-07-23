[CmdletBinding()]
param()

throw @"
Direct approval scripts are disabled. Send the exact command below as a new
Telegram message so the trusted Hermes gateway plugin can authenticate it:

  /nx_approve <proposal-id> <revision> <full-64-character-digest>
"@
