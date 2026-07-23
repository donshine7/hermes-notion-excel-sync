# Repository contribution guardrails

These rules apply to every automated or human change in this repository.

## Immutable sources

- Treat the configured local source folder and workbook as read-only.
- Never move, rename, delete, rewrite, format, lock, or save a source file.
- Store snapshots, manifests, indexes, generated Wiki pages, and logs outside
  the source tree.
- Tests must use temporary synthetic fixtures, never the configured production
  source.

## Approval boundary

- Excel is authoritative for case facts sent to Notion.
- Email and the generated Wiki are supplementary analysis inputs.
- A model may prepare a proposal but may not hold Notion write credentials.
- Every Notion mutation requires a fresh, exact Telegram approval bound to the
  displayed revision and digest.
- An edit, exclusion, deferral, source change, or expired approval invalidates
  the previous approval.

## Public repository safety

- Do not commit local configuration, tokens, operational IDs, personal email
  addresses, source documents, mail originals, databases, generated Wiki
  output, snapshots, or audit receipts.
- Use `example.com`, clearly fake identifiers, and synthetic records in tests
  and documentation.
- Stage files explicitly. Do not publish private backup refs or use a mirror
  push.

## Verification

- Run `python -m ruff check .`.
- Run focused tests for the changed behavior and the relevant regression
  groups.
- Verify the staged file list and scan for credentials and forbidden source
  extensions before every public push.
