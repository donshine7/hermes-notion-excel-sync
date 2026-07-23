# Analysis business rules

## Analyzer platform

Route each changed row to every matching active analyzer. Use header, sheet, keyword, dependency, and priority signals from the analyzer registry. Preserve unclassified columns and values; never discard them silently.

Run the default analyzers in dependency order where applicable:

1. `case_identity`
2. `party`
3. `group`
4. `workflow_status`
5. `deadline`
6. `actual_cost`
7. `government_support_evidence`
8. `registration_followup`
9. `contact`
10. `document_material`
11. `relationship`
12. `anomaly`

Allow new analyzers to be registered without changing the writer. Keep generated analyzers in `DRAFT`, then `SHADOW`; require verification before `ACTIVE`. Never execute generated code merely because an unfamiliar field was discovered.

## Case and relationship analysis

- Prefer a stable Excel business key, such as the firm's case number, over the row number.
- Split a row containing multiple case numbers into separate case entities while retaining a shared source-row relationship.
- Propose one Korean patent case page per firm case number.
- Treat ambiguous matches, duplicate case numbers, relation conflicts, and deletion candidates as material review items.

## Group analysis

Distinguish government-support programs, bulk retainers, billing bundles, and related-invention portfolios. Use agreement number, program year, administering agency, client, explicit bundle wording, and shared commercial terms as evidence. Do not create or change a group from name similarity alone.

Allow the Telegram user to select an existing group, edit a proposed new group, add or remove cases, omit grouping, or defer the decision.

## Actual case cost analysis

Use `actual_cost` only for the real cost of the underlying case. Analyze, when present:

- supply amount and VAT;
- official fees;
- agreed total and discount;
- invoice amount and date;
- payment amount and date;
- deposit deduction and remaining deposit;
- outstanding amount, payer, billing condition, and payment status.

Keep source amounts separate from computed amounts. Validate totals but do not silently repair inconsistent figures. Do not overwrite actual case cost with a government-support eligible or subsidy amount.

## Government-support evidence analysis

Use `government_support_evidence` to construct a proposed evidence package for a specific government-support program. Determine:

- the program, agreement, administering agency, year, and eligible period;
- the cases and work products used as evidence;
- the evidence unit and count, such as three provisional applications;
- the calculation method and recognized unit price;
- actual-cost reference total;
- eligible amount, subsidy, self-payment, and VAT treatment;
- support limit, shortfall, or excess;
- duplicate use across programs;
- required documents and completeness.

Use `actual_cost` outputs as read-only constraints. Do not infer an unprovided recognized unit price, subsidy ratio, VAT rule, or program limit. Mark the package for review when these rules are unavailable or conflicting.

When any row in an evidence package changes, rebuild that package from every related row in the full current workbook snapshot. Use the changed rows only to decide which package needs a proposal; do not shrink a three-case package to the single changed row, and do not emit independent changes for unchanged context rows.

Example distinction:

- Three provisional cases with total actual cost KRW 3,300,000 remain actual-cost records.
- A program package that recognizes those three cases for KRW 3,000,000, split into KRW 2,400,000 subsidy and KRW 600,000 self-payment, remains a separate government-support evidence allocation.

Editing either package in Telegram must not change the other implicitly.

## Other domain analysis

- Use `deadline` for statutory, internal, client-response, and registration deadlines; flag impossible or reversed dates.
- Use `party` for clients, applicants, inventors, representatives, and cost bearers; avoid fuzzy merging without strong identifiers.
- Use `workflow_status` for work type, procedural stage, filing, completion, and status consistency.
- Use `registration_followup` for registration deadlines, division decisions, payment follow-up, and next contact.
- Use `contact` for phone, email, message, and other communication events.
- Use `document_material` for invention materials, office-action materials, requests, receipt, and confirmation states.
- Use `relationship` after entity analyzers to resolve case, group, party, work, cost, and evidence-package links.
- Use `anomaly` last to detect duplicates, date errors, amount mismatches, policy conflicts, unsupported values, and cross-analyzer contradictions.

## Model output

Require every proposed value to satisfy the typed result schema and carry analyzer name, analyzer version, reason, confidence, and cell-level source references. Treat model-generated text as a candidate only. Reject values outside configured Notion types or select options, and surface the rejection for Telegram review.
