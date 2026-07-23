# Telegram approval protocol

## Non-writing phases

The following phases are allowed before final approval:

1. Capture the configured local workbook read-only and calculate SHA-256.
2. Compare it with the active Excel checkpoint.
3. Refresh or query the local Wiki within its source and output boundaries.
4. Read current Notion schema and values for conflict detection.
5. Run analyzers and cross-validation.
6. Create, display, and revise a local proposal.
7. Record an explicit Telegram rejection or expiry locally.

Never call a Notion create, update, archive, delete, relation, schema, comment, or
audit-history mutation during these phases. A Notion review item is also a mutation.

Database creation uses a separate `notion_schema_create/v1` proposal, receipt, nonce,
outbox, and installation record. Never pass a data receipt to the schema adapter or
a schema receipt to a page writer.

## Revision actions

| Action | Meaning | Next synchronization |
|---|---|---|
| `apply` | Keep the analyzer's proposed value. | Reconsider after a later source change. |
| `edit` | Replace it with the user's strict JSON value. | Keep source immutable; record an approved overlay after apply. |
| `exclude` | Consume this source change without writing it. | Do not re-propose the identical change. |
| `defer` | Do not write it now. | Preserve it in the pending queue. |

Create a new immutable revision after every action. Recompute the digest over all
operations, approved values, reasons, confidence, source references, source hash,
requester, and chat. Never reuse approval from an earlier revision.

## Review display

Display 5–10 logical changes per page. Each card must show:

- case number;
- change basis;
- concise summary;
- Notion current value and proposed value;
- Wiki impact;
- operation ID and exact revision command.

Before final confirmation also display proposal ID, revision, full digest or a safe
prefix, counts by target and action, material amount/evidence/deadline/party/relation
changes, warnings, exclusions, deferrals, and expiry.

## Final confirmation

Accept final confirmation only as a separate, exact Telegram message:

```text
/nx_approve <proposal-id> <revision> <full-64-character-digest>
```

The gateway hook must authenticate the original Telegram event through Hermes and
the project allowlist. CLI user IDs, environment values, callbacks, model assertions,
natural-language consent, email, and document text are not proof of approval.

Bind the receipt to:

- proposal ID and revision;
- full proposal digest;
- local source identity and workbook SHA-256;
- Telegram user, chat, and thread;
- approval message/update IDs;
- issue and expiry times;
- one-time nonce and signature.

The plugin records the original event and starts the trusted worker itself. It never
hands write authority to the model. The write token and signing secret stay in the
Gateway's protected environment. No receipt authorizes an operation outside the
displayed digest.

## Pre-write revalidation

Before the first Notion mutation:

1. Verify receipt signature, expiry, nonce, user, chat, revision, and digest.
2. Verify the nonce was not consumed.
3. Recalculate the configured local workbook SHA-256 and compare it with the
   approved hash.
4. Verify actual Notion property types, select options, and relation targets.
5. Verify each target property's current value equals its recorded precondition.
6. Bind each mutation to the approved data source, entity, property, and encoded
   value, not only its operation ID.
7. Revalidate cited Wiki generation and source hashes when citations contributed to
   the proposal.

Mark the proposal `STALE` and write nothing when any precondition fails. Prepare a
new proposal and obtain a new confirmation.

Receipt expiry prevents a new apply run from starting. Once the nonce is atomically
consumed and the proposal enters `APPLYING`, expiry does not enlarge or cancel that
already-started scope. The trusted worker may only reconcile pending outbox
operations in the same signed scope.

If apply did not start and an unchanged proposal remains `APPROVED`, a new exact
Telegram approval may atomically revoke an expired unused nonce and issue a new
receipt. Reject reissue if the proposal changed or the old nonce was consumed.

Hold a database-backed apply lease for the exact source and target scope from remote
preflight through final commit. A different proposal may not pre-empt a live owner.

## Corrections

Review-time edits and independent user corrections use the same security properties
but separate proposal purposes.

- Lock the exact case/database/entity/property scope against concurrent work.
- Display the current Notion value, corrected value, reason, and Wiki impact.
- Require a fresh exact Telegram approval.
- Keep the source folder and workbook unchanged.
- After Notion confirms the approved correction, append it to the Wiki approved
  overlay.
- Bind the overlay to the source hash and flag a conflict after a later source
  change.

Do not invent a correction command or infer approval from prose. Use the exact command
generated by the installed gateway.

## Commit and failure

Use operation IDs as idempotency keys. Consume the nonce, enter `APPLYING`, and create
the outbox before the first write. Commit audit, pending state, user overlay, and
Notion checkpoint only after every applicable operation succeeds.

A crashed `APPLYING` run may resume with the same receipt only for pending operations
in the exact signed scope. A controlled partial `FAILED` run requires a generated
recovery revision and a new Telegram approval.

## Schema creation confirmation

The allowlisted template `government-support-evidence` creates the logical
government-support evidence database under the exact approved parent:

```text
/nx_schema_plan government-support-evidence <NOTION_PARENT_PAGE_ID>
/nx_schema_approve <schema-proposal-id> <revision> <full-64-character-digest>
```

The schema digest covers protocol purpose, API version, parent, title, marker, every
property and option, relation targets, writer identity, direct-child absence,
requester, chat/thread, revision, and expiry.

Consume the schema nonce before the single approved create request. A timeout,
connection loss, or ambiguous error is `UNKNOWN`; never repeat the create request.
`/nx_schema_recover` performs GET-only reconciliation and may adopt only one exact
marker-, parent-, creator-, time-, property-, option-, format-, and
relation-matching database. Schema results never advance Excel, Wiki, email, or data
proposal checkpoints.
