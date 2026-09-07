# AGN Invoice Automation (single file)

Everything lives in **`agn_invoices.py`**. Double-click **`Run AGN Invoices.bat`**
to run it, or use the command line for more control.

## One-time setup

```
pip install -r requirements.txt
playwright install chromium
python agn_invoices.py --setup-credentials
```

The credentials command stores your FieldPO username/password securely in
Windows Credential Manager (never in a file). You can re-run it any time to
update them.

## Daily use

```
python agn_invoices.py
```

or just double-click `Run AGN Invoices.bat`. This runs, in order:

1. **Extract** - reads new invoices from Outlook (`Inbox\AGN\Invoice`),
   saves PDFs, pulls VIN + amount, moves handled emails to `...\Invoice\Processed`
2. **Check and act** - opens each VIN once in FieldPO, skips anything already
   marked APPROVED, and applies the same strict gates used by dry run
3. **Approve and close** - automatically approves the eligible PO and closes
   the Work Order after every strict gate passes.
   Mismatches, failed gates, declines, and errors remain for manual review.

You can also run any single step on its own:
```
python agn_invoices.py --extract-only
python agn_invoices.py --check-only
python agn_invoices.py --approve
```

Cap each selected stage to one invoice for a controlled trial run:
```
python agn_invoices.py --max-invoices 1
```

For a controlled development run, add `--confirm-each`. The eligible PO remains
visible while the terminal requests permission before any approval action.

Run the complete extraction and FieldPO review path without pressing Approve:
```
python agn_invoices.py --dry-run --max-invoices 1
```

Dry run returns to FieldPO Home after each invoice and writes each `WOULD_CLOSE`
or `SKIPPED` result to `closure_decisions.jsonl`. It updates normal extraction and
check queue state, but it never clicks Approve or Close.

Uncategorized invoices are processed first, oldest by Outlook `ReceivedTime`.
When categorized backlog rechecking is enabled, those invoices follow in
oldest-first order. The run cap is applied after that ordering.

A `WOULD_CLOSE` decision requires all of the following:
- the invoice and FieldPO authorized amounts match exactly
- the invoice date is at least `age_approval_days` old (14 days by default)
- FieldPO resolves a valid MVA
- the Work Order `Created By` value exactly matches `allowed_work_order_created_by` (`Steele, Dirk`)

Trace one or many VINs across queue history and Outlook invoice emails:
```
python agn_invoices.py --trace-vin 1GKENKKSXTJ197778
python agn_invoices.py --trace-vin 1GKENKKSXTJ197778,KL79MPSPXTB193651
python agn_invoices.py --trace-vin 1GKENKKSXTJ197778 --trace-vin KL79MPSPXTB193651
```

The trace report returns where each VIN appears and a summary status:
`processed`, `skipped`, `approved`, `failed`, or `missing`.

To request permission for each approval during development:
```
python agn_invoices.py --approve --confirm-each
python agn_invoices.py --max-invoices 1 --confirm-each
```

`--approve-only` is still supported as an alias for backward compatibility.

## Config

Runtime settings are in `agn_invoices_config.json`:
- `account_name` - must match your Outlook account name exactly
- `processed_category_name` - legacy Outlook category used to identify migration candidates
- `process_categorized_invoices` - recheck legacy categorized invoices still in Invoice before moving them
- `max_items_per_run` - safety limit on extract step (set to null for no limit)
- `max_approvals_per_run` - safety limit on approve step (set to null for no limit)
- `age_approval_days` - minimum invoice age required for dry-run and live closure eligibility

## Selectors still need verification

The FieldPO click-path (search, click vehicle, Active Work Order tab, PO
card, reading the auth amount, clicking APPROVE) is marked `TODO` in the
code — built from screenshots, not the live page. Run `--check-only` first,
watch where it stops, right-click that element in the browser → Inspect,
and send me the HTML so I can fix the selector.

## Where things are stored

Everything lives right in this same folder, alongside the script:
- `pdfs\` — downloaded invoice PDFs
- `invoices_queue.csv` — the working queue (safe to open in Excel anytime)
- `browser_profile\` — saved FieldPO login session

## Safety notes

- Membership in the `Processed` folder is the sole finalized-invoice marker
- Legacy categorized invoices are rechecked because green did not guarantee closure
- Nothing is deleted
- Production mode automatically approves only invoices that pass every strict closure gate
- Anything that doesn't match, or errors out, is never auto-approved
