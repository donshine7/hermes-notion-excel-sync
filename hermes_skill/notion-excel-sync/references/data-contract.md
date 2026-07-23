# Source, checkpoint, and Notion contract

## Source contract

Identify the source with the configured normalized local root and workbook path.
Both must be absolute, and the workbook must be inside the source root.

Open the source read-only and calculate SHA-256 for every capture. Check file identity,
size, and modification time before and after reading. Abort if the file changes
during capture.

Never save, replace, rename, move, delete, or create any file in the source root.
Snapshots, manifests, locks, indexes, temp files, state, logs, and Wiki output must
be outside the source.

Retain field-level provenance:

- normalized source identity;
- workbook SHA-256;
- sheet and row;
- cell or range;
- raw value;
- capture time.

Technical provenance is not a government-support evidence package.

## Baseline contract

The first authenticated `/nx_sync` event defines `T0`. It is not a configurable
historical cutoff.

For the first run:

- capture the full current workbook;
- create the complete data-folder manifest;
- build the initial Wiki from Excel and data files only;
- exclude email;
- reconcile the full Excel state with current Notion.

For later runs:

- compare the last successful Excel hash with the current workbook;
- compare the last successful data manifest with the current folder;
- ingest unseen Hiworks UIDLs received after `T0`;
- preserve separate checkpoints for each input.

A verified current workbook with no selected-sheet changes may create a local NOOP
checkpoint without touching Notion. Rejection, expiry, staleness, or partial failure
does not move the Notion checkpoint. Deferred items remain in the pending queue.

## Change matching

Match records by configured stable key columns before row position. Detect creates,
updates, and deletion candidates. Treat reordered rows as unchanged when stable keys
and normalized values match. Require item-level review for deletion candidates and
prefer status/archive proposals over destructive deletion.

## Notion targets

Resolve actual database/data-source IDs from `notion.databases`. Treat
`<NOTION_PARENT_PAGE_ID>` as an entry point only, not every database ID. Validate
configured IDs, property types, select options, number formats, and relation targets
before proposing or writing.

Expected semantic targets include cases, groups, parties, work, deadlines, actual
costs, government-support evidence, registration follow-up, contacts, source
materials, review, and change history.

Missing schema is a separate schema proposal requiring its own exact Telegram
approval.

## Writer contract

Accept only operations protected by a verified approval receipt. Use stable entity
keys and operation IDs for idempotent upsert. Never let analyzers, Telegram
formatting, source readers, Wiki projection, mail ingestion, or proposal editing call
the writer directly.

An approved user correction may update Notion and then append the same decision to a
Wiki overlay. It must never mutate the raw source projection.
