# Glass Workflow Simplification

## Scope

- Glass only.
- PM flow is excluded.
- Vendor tracking is excluded from the active simplified flow.

## Primary Commands

### 1. Intake

`Run-Glass-Intake.cmd`

What it does:

1. Reviews incoming glass inventory email.
2. Transfers email data to the spreadsheet.
3. Uses the email Type date as the Inventory Date, whether it is today or tomorrow.
4. Inserts FieldPO next-action data for all rows matching that Type date.
5. Creates missing complaints and work items for all rows matching that Type date.
6. Marks the email read only after both downstream stages succeed.

Behavior:

- Every email must use one Type date equal to today or tomorrow.
- A malformed scan rejects the complete email; valid sibling scans are not processed separately.
- If FieldPO fails, the workflow stops before complaint/work-item creation and leaves the email unread.
- Multiple unread emails are processed one at a time. Emails sharing a Type date each receive their own downstream pass.
- Before each reused Compass vehicle search, all retained filter pills and the keyword field are cleared; the next MVA is selected through the dedicated `Search by MVA` filter.
- Standalone FieldPO and Compass runs require an explicit ISO Inventory Date or another explicit target mode.

### 2. Closeout

`Run-Glass-Closeout.cmd`

What it does:

1. Builds close candidates automatically from invoice/sheet signals.
2. Closes work items.
3. Runs CompletedInvoices for PO/invoice-side closure.

Modes:

- Close by MVA: `--mva <value>`
- Close batch: default mode

Close batch default source:

- Automatic build from invoices/sheet.
- The candidate report lists every invoice that does not produce a new candidate,
  with its subject, VIN, matched MVA when available, and reason: recalibration,
  already queued, recorded in close history, checkpointed today, duplicate MVA in
  this batch, an explicit selection limit, or a failed source/Sheet check.
  Close history records handled work items, not invoice approval or payment.
  Recalibration and other normal exclusions are separate from review failures.
  History exclusions include the recorded date and outcome: `closed`,
  `not_found` (nothing was closed), or `checkpointed` (no closure outcome
  recorded). All matching history records are shown. History contains only MVA,
  complaint type, date, and result; it cannot establish an identical VIN, PO,
  or invoice.

Supported overrides:

- `--csv <path>` for reviewed CSV-driven closeout
- `--max-rows <n>`
- `--max-invoices <n>` (alias `--Max <n>`) selects at most n eligible invoices,
  oldest first, once before changes; every closeout stage is restricted to that
  same selection, including work items already in the queue.
- `--invoice-number <n>` scopes every closeout stage to that exact invoice number.
- `--sweep-terminal-to-needs-review`

Invoice-scoped options cannot be combined with `--mva`, `--csv`, `--max-rows`,
or `--sweep-terminal-to-needs-review`. Selection or work-item execution failures
stop the scoped workflow; no other invoice is selected as a replacement.

## Canonical Invoice Path

- CompletedInvoices is the active invoice runner.
- `Run-PayCompletedInvoices.cmd` remains legacy, deprecated, and retained in the repo for future reference.
- The deprecated scripts are kept available for historical context and troubleshooting, but they are not part of the active daily workflow.