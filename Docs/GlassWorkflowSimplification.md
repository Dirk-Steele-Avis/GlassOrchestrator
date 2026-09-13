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
3. Inserts FieldPO next-action data.
4. Creates missing complaints and work items.

Behavior:

- If FieldPO next-action fails, the workflow continues to complaint/work-item creation.
- Any arguments are forwarded to the complaint/work-item creation step.

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

Supported overrides:

- `--csv <path>` for reviewed CSV-driven closeout
- `--max-rows <n>`
- `--max-invoices <n>`
- `--invoice-number <n>`
- `--sweep-terminal-to-needs-review`

## Canonical Invoice Path

- CompletedInvoices is the active invoice runner.
- `Run-PayCompletedInvoices.cmd` remains legacy, deprecated, and retained in the repo for future reference.
- The deprecated scripts are kept available for historical context and troubleshooting, but they are not part of the active daily workflow.