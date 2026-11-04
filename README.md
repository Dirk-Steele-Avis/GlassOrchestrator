# GlassOrchestrator (GPO)

A modular Python pipeline for vehicle glass procurement, built on a **6-phase architecture**.

Canonical behavioral requirements are maintained in
[Docs/GlassOrchestratorRequirements.md](Docs/GlassOrchestratorRequirements.md).

## VIN lookup from chat

Use the project Copilot skill `/lookup-vins` with a pasted list or screenshot of
eight-digit MVAs, or ask "look up VINs for these MVAs." The skill returns plain
text with an `MVA VIN` header and one row per input, preserving order and leading
zeros. See [.github/skills/lookup-vins/SKILL.md](.github/skills/lookup-vins/SKILL.md).

This is a read-only Compass GO lookup using the dedicated automation Edge
profile; it does not run intake, edit pipeline data, update Sheets, or process
invoices. Complete sign-in in the opened browser if prompted. The lookup never
terminates browsers or changes profiles. Explicit Vehicle Not Found results get
one retry at the end of the list in the same session, preserving output order.
No other failures are retried. A failed step
stops the batch and reports the reason without returning a partial success.
Only an explicit Compass GO Vehicle Not Found result on both attempts produces `N/A`.

## Architecture

| Phase | Name | Description |
|-------|------|-------------|
| 1 | **Input** | Fetch scan data from Gmail (`export@orcascan.com`) via IMAP |
| 2 | **Parsing** | Regex triage (`^(\d{8})([rc]*)$`), build session manifest |
| 3 | **Worker** | Write MVAs to CSV, invoke `GlassDataParser.py` subprocess |
| 4 | **Data Merge** | Left-join manifest with scraper results; missing VIN → `N/A` |
| 5 | **Persistence** | Append all parsed rows to Google Sheet (`GlassClaims` tab); carry forward Original Date within incident window |
| 6 | **Notification** | Optional HTML email for Replacement items; disabled by default with `notifications_enabled` |

## Suffix Rules

| Suffix | Field | Value | Default (no suffix) |
|--------|-------|-------|---------------------|
| `r` | Damage Type | Repair | Replacement |
| `war` | Damage Type | Warranty | Replacement |
| `c` | Claim# | Listed | Missing |
| ` OEM` | Vendor routing | Replace(AVIS) | Replace(AGN) |

Directional area codes use `<side><orientation><area>` order, such as `LFD` for
Left Front Door. OEM is terminal and must follow a configured glass area after one
space: `WS OEM`, `WSc OEM`, `LFD OEM`, or `LFDc OEM`. A bare scan such as
`62155822OEM` is invalid.
The Google Sheet appends `(OEM)` to the physical Area, such as `Windshield(OEM)`.
If an OEM scan includes `r`, it is logged and normalized to `Replace(AVIS)`.

Temporary aliases in `legacy_area_aliases` accept orientation-first scans from
older emails and normalize them to canonical codes. Remove those configured aliases
after all legacy emails have been processed.

## Data Contract — `ATL_Data 2026 : GlassClaims`

The pipeline output maps 1-to-1 with the `GlassClaims` tab in the master workbook.
Phase 5 inserts rows above the summary section and always appends rows (no deduplication).

For returning MVAs, `Original Date` is the earliest sighting in the latest episode. Starting
from the new row, the pipeline walks backward through sightings whose adjacent `Inventory Date`
gaps are at most `incident_window_days` (default: 7 calendar days). A larger gap starts a new
episode: an 8-day difference means seven full intervening days had no sighting, so the search
stops before older damage history can be carried forward.

| # | Column | Source | Phase | Notes |
|---|--------|--------|-------|-------|
| 1 | **Inventory Date** | Email Type/date parsing | 2 | `MM/DD/YYYY` |
| 2 | **Original Date** | Inventory Date at first sighting; carried forward for same incident | 2/5 | Earliest sighting in the latest contiguous episode |
| 3 | **MVA** | Orca Scan Description | 2 | 8-digit, suffixes stripped |
| 4 | **FPO#** | Manual workflow | 2 | Pipeline writes blank |
| 5 | **VIN** | CGI scraper (`GlassResults.txt`) | 4 | `N/A` if scraper miss |
| 6 | **Make** | CGI scraper `Desc` column | 4 | Populated by Phase 4 merge |
| 7 | **Location** | Email Type column suffix | 2 | `0420APO` -> APO, `0420BB` -> BB |
| 8 | **Action** | Suffix `r` -> Repair | 2 | Default: `Replacement`; mapped to vendor labels on write |
| 9 | **Area** | MVA area suffix map | 2 | Example: `WS` -> Windshield |
| 10 | **Claim#** | Suffix `c` -> Listed | 2 | Default: `Missing` |
| 11 | **WorkItem** | Runtime config | 2 | Defaults to `verified` |

## Setup

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python HTTPS connections use the operating system certificate trust store through
`pip-system-certs`, including corporate certificates installed in Windows.
Certificate and hostname verification remain enabled. If a corporate certificate
is not trusted by Windows, have IT install the approved certificate; do not disable
TLS verification. Restart running Python processes after installing dependencies.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GLASS_EMAIL_ACCOUNT` | Gmail address for IMAP login |
| `GLASS_EMAIL_PASSWORD` | Gmail app password |
| `GLASS_SENDER` | From address for outbound notifications |
| `GLASS_NOTIFY_RECIPIENTS` | Comma-separated recipient list |
| `GLASS_LOGIN_USERNAME` | UI login username (overrides config username) |
| `GLASS_LOGIN_PASSWORD` | UI login password (overrides config password) |
| `GLASS_LOGIN_ID` | UI login WWID/login id (overrides config login_id) |

Phase 5 also requires a **Google Service Account** JSON key file at `Service_account.json` in the project root,
with Editor access to the target spreadsheet.

### Config Files

The orchestrator loads config files in this order, with later files overriding earlier ones:

1. `orchestrator_config.json` — shared orchestrator defaults
2. `orchestrator_project.json` — committed project-level overrides
3. `orchestrator_project.local.json` — machine-specific overrides (gitignored)
4. `orchestrator_config.local.json` — legacy local override, still supported (gitignored)
5. `config/config.local.json` — shared local override for cross-module machine settings (gitignored)

Outbound notification email is retained as a rollback option. Set the JSON boolean
`"notifications_enabled": true` to enable it; the default is `false`.

The UI/login config loader merges files separately in this order:

1. `config/config.json` — shared UI/login defaults
2. `config/project.json` — committed project template
3. `config/project.local.json` — machine-specific overrides (gitignored)
4. `config/config.local.json` — legacy local override (gitignored)

Use `.local.json` files for machine-specific credentials, tenant URL, and workflow defaults so each user avoids touching committed files.

## Usage

```bash
Run-GlassOrchestrator.cmd
```

Before first run (or when credentials change), you can launch the interactive env setup:

```bash
Run-Setup-GlassEnv.cmd
```

If you only need to set the login password, use:

```bash
Run-Set-GlassPassword.cmd
```

### Simplified Glass workflow

The new day-to-day Glass flow is split into two commands:

```bash
Run-Glass-Intake.cmd
Run-Glass-Closeout.cmd
```

Intake covers incoming inventory email, spreadsheet updates, FieldPO next-action, and complaint/work-item creation. The Orca email Type date is authoritative and may be today or tomorrow, allowing inventory to be entered the night before. Each email is persisted, processed through FieldPO and Compass for that exact date, and then marked read.

Malformed scan values are logged as warnings and skipped so valid scans can continue.
Correct skipped scans in Orca and resubmit only those scans. The source email is
marked read after its valid scans complete successfully. An email with no valid
scans remains unread and stops intake; invalid email structure or dates and invalid
repair or warranty/OEM combinations also remain blocking errors.

Closeout covers close candidate build, work-item closure, and the CompletedInvoices PO/invoice-side path.

Outside BB/APO, local-market windshield replacements have a separate $150
camera/ADAS recalibration invoice with its own PO and invoice checks. These
invoices do not create another Glass closure or overwrite replacement costs.
BB (3333 Bobby Brown Parkway) and APO (4340 Global Gateway Connector) recalibrate
in-house: vendor recalibration invoices matching those Customer Information
addresses are blocked for review. Missing recalibration customer addresses and
amounts other than $150 are also blocked. Invoice validation does not approve or
pay a PO by itself; the invoice-scoped Glass closeout approves a validated PO
only after Compass closure and the exact FieldPO approval gates below pass.

### Glass Damage Morning Report

After intake is complete, run:

```bash
Run-Glass-Morning-Report.cmd
```

The command reads `GlassClaims` rows whose `Inventory Date` is today, regardless
of the current sheet filter, saves a dated HTML file under `reports/`, and opens
it in the default browser. Review the report, click **Copy report for Outlook**,
and paste it into the body of a manually addressed email. The report workflow is
read-only and never sends email.

See [Docs/GlassMorningReportRequirements.md](Docs/GlassMorningReportRequirements.md)
for the data, rendering, and failure contracts.

### Outstanding PO Invoice Analysis

Search the configured Outlook desktop Exchange mailbox's Inbox and **all**
descendant folders, including Processed, using an outstanding-PO workbook:

```bat
Run-AnalyzeOpenPOs.cmd --input "C:\Users\steeldir\Downloads\COLLEGE PARK_OPEN FPOs (12).xlsx"
```

The workbook must contain the `Open Purchase Orders` worksheet with
`MVA_NUMBER`, `VIN_NO`, `PO_NUMBER`, `PO_STATUS`, `PO_DAYS_OPEN`,
`PO_ESTIMATE_AMOUNT`, and `SUPPLIER_NAME`. Every data row must have
status `OPEN`, a valid VIN, and a unique FPO number; invalid input stops the run.
Only POs with the export's `PO_DAYS_OPEN` **greater than 7** are listed and
searched; exactly 7 days is excluded. Age must be a nonnegative integer.
Configure the exclusive age cutoff with `--older-than-days N` (default: `7`).
For example, `--older-than-days 14` searches only POs older than 14 days.
`PO_DAYS_OPEN` is used only for filtering and is omitted from all PO output
lists and analysis results. The completion manifest records the selected filter.
Age is taken from the export snapshot, not recalculated using today's date.
An export with no eligible POs stops before connecting to Outlook.
All exported vendors are included. Export columns other than `PO_DAYS_OPEN` are retained.
The exact mailbox comes from `outlook/agn_invoices_config.json` `account_name`,
or an explicit `--account`. No default mailbox is guessed.

Each run creates a directory under `reports/` containing:

- `outstanding_pos.csv`: validated source list, written before the mailbox scan.
- `analysis.csv`: one row per PO with status, reason, verified and related-email counts.
- `email_evidence.csv`: PDF PO, VIN, subtotal, and comparison outcome,
  with folder, email subject, received time, Outlook EntryID/StoreID, and attachment.
- `unresolved_invoices.csv`: selected emails/PDFs that could not be read, with errors.
- `completed.json`: completion marker, scan counts, scope, and limitations.

Email selection requires a subject containing `invoice` (case-insensitive) AND
both sender display name `AGN GLASS LLC` and address `noreply@omegaedi.com`
(case-insensitive exact matches after trimming). A PO is not required in the
subject: `[External] Invoice #5459388` is selected, as is
`[External] Invoice #5470706 (PO # FPO1177101)`.
Only selected emails have their PDF attachments downloaded. The existing
`invoice_workflow.invoice_parser.parse_invoice_pdf` parser extracts the exact
PDF PO, VIN, and subtotal; no new PDF parser or OCR is introduced.

`Invoice found` requires the PDF PO, VIN, and subtotal to match the export's
PO, VIN, and estimated amount exactly. Any PO in the subject must also agree
with the PDF. A VIN or amount match alone never confirms an invoice.
`inconclusive` identifies related invoices with mismatched fields, non-AGN
suppliers outside the sender scope, or otherwise unmatched POs when selected
invoices could not be read. Vendor PO/vehicle mix-ups are reported with the
actual PDF values and mismatch fields, never reassigned to another PO.
`Invoice not found` means no related invoice was found in the selected AGN
emails and no selected invoice remained unreadable; it is not proof that no
invoice exists elsewhere. Shared VINs never establish which PO was invoiced.

Selected emails without PDFs, failed attachment access, and unreadable PDFs
are explicitly reported and processing continues, as requested. An unreadable
invoice can obscure any target, so otherwise unmatched POs become inconclusive.
Successfully verified matches remain found. Email bodies are not searched.
No email date or unread filter is applied. Any inaccessible folder/message or
unreadable subject/sender stops the scan with a nonzero exit code and a
precise error in `log/open_po_analysis.log`; no completed analysis is produced
for a failed scan. On a large Inbox this full scan can take time.

This tool never modifies messages, marks them read, approves POs, or closes work
orders. Downloaded PDFs are temporary and removed after inspection.
The terminal displays separate `Invoice found`, `Invoice not found`, and
`inconclusive` lists, including PO, VIN, amount, and reason, plus invoice warnings.

### Distant-Location Glass Import

The importer validates an Excel or CSV source and reads the configured
`GlassClaims` worksheet to block duplicate `Inventory Date` + `MVA` keys. It
does not run FieldPO or Compass. The default is a read-only dry run; use
`--apply` only after reviewing the rows:

```bash
Run-Import-Distant-Glass.cmd --input "path\to\LM_sample.xlsx"
Run-Import-Distant-Glass.cmd --input "path\to\LM_sample.xlsx" --apply
```

The source headers are `Inventory Date`, `MVA`, `Location`, `Area`,
`DamageType`, `GlassSource`, and `ClaimStatus`. When an Excel workbook has
multiple worksheets, specify the exact tab with `--worksheet`. Nine-digit
source MVAs with a leading zero are normalized to the existing eight-digit
GlassClaims representation; duplicate checks compare both representations
canonically.

To review invoices already in Outlook `Inbox/AGN/Invoice/Needs Review` and move
emails whose exact FieldPO PO status is `APPROVED` to `Processed`, run:

```bash
Run-ReviewInvoices.cmd
```

To check the current completed-invoice ledger without changing Outlook or any
workflow state, pass one or more VINs, MVAs, or both:

```bash
Run-InvoiceLookup.cmd --vins "4T1DAACK0SU050843,1HGCY1F43TA029160"
Run-InvoiceLookup.cmd --mvas "55897402 61751340"
Run-InvoiceLookup.cmd --vins "4T1DAACK0SU050843" --mvas "61751340"
Run-InvoiceLookup.cmd --vins "4T1DAACK0SU050843,1HGCY1F43TA029160" --received-only
```

Values may be comma-, space-, or semicolon-separated, and each option may be
repeated. VIN lookup reads `data/completed_invoices.db` directly. MVA lookup
first requires exactly one VIN from the authoritative `GlassClaims` sheet, then
reads that same ledger. `RECEIVED` includes the invoice, PO, amount, received
timestamp, identity status, and Outlook status. `NOT FOUND IN INVOICE LEDGER`
means the current completed-invoice workflow has not captured an invoice for
that VIN. Add `--received-only` to print only targets with a captured invoice.

Close `outlook/invoice_review.csv` in Excel before starting. The command verifies
that the report can be replaced before it opens Outlook or FieldPO; a locked report
stops the run before any email moves.

The command includes only exact new-format subjects such as
`[External] Invoice #5294412 (PO # FPO1131464)`. It rewrites
`outlook/invoice_review.csv` with every matching message, its original recorded
review reason when available, and the current FieldPO approval status and amount.
Every approved PO authorizes moving that exact email to `Processed`, regardless of
whether its amount matches. Amount-match-only emails remain in `Needs Review`.
The command does not approve POs, close work orders, update workflow ledger state,
or move old-format messages, receipts, or unresolved invoices.

### Standalone FieldPO Closer

`Run-FieldPO_Closer.cmd` compares queued POs with Outlook invoices and closes a
FieldPO PO only when the exact invoice and FieldPO checks pass. Choose the input
mode explicitly:

```bash
Run-FieldPO_Closer.cmd --mode po
Run-FieldPO_Closer.cmd --mode vin
```

PO mode reads the local `outlook/FieldPO_Closer/pos.csv` queue with columns
`PO,VIN,Result`. It looks up each PO directly in FieldPO and records the VIN,
even when no invoice is found. A missing invoice remains `Missing Invoice`; the
PO is not closed, and the PO/VIN row is available for manual vendor follow-up.
The closer does not email the vendor. If an invoice is found, its subject PO and
PDF PO must both match the queued PO. If the invoice contains a VIN, it must
match FieldPO's VIN. The closer then checks the exact FieldPO PO, work-order
owner, status, and authorized amount before approving the PO and closing the
work order. Mismatches, ambiguous invoices, and failed checks are marked
`Review` and are not closed; found invoices that need review are moved to
`Needs Review`. Successful invoice emails are moved to `Processed`.

Blank and `Missing Invoice` results are eligible for closeout. `Success` and
`Review` rows are not retried for closeout, although PO mode fills a blank VIN
for those rows without approving or closing anything.

VIN mode reads `outlook/FieldPO_Closer/vins.csv` with columns `VIN,Result` and
rechecks every listed VIN regardless of its prior result. It searches the
`Invoice`, `Processed`, and `Needs Review` folders under `Inbox/AGN`. Invoices
are evaluated independently by their exact PO, so a corrected invoice can pass
even when an earlier invoice for that PO remains in `Needs Review`. The PDF
total is compared with FieldPO's authorized amount. Passing invoices are moved
to `Processed`; failed identity, amount, or FieldPO checks are moved to
`Needs Review`. Matching resend copies for the same PO are processed as one
FieldPO action.

In VIN mode, an already-approved PO is not approved again. If FieldPO shows the
exact `COMPLETED` work-order status, the invoice passes and is moved to
`Processed`. If the PO is approved but the work order is not explicitly shown as
`COMPLETED`, the result is reported for review; the closer does not infer a
closed state or repeat approval.
The closer prints concise, flushed progress for the Outlook search, FieldPO
connection, and each VIN, followed by a result count summary.
This standalone tool is for targeted work and troubleshooting, not the primary
`Run-Glass-Closeout.cmd` workflow.

`Run-Glass-Closeout.cmd` supports two operator modes:

```bash
Run-Glass-Closeout.cmd --mva 058524185
Run-Glass-Closeout.cmd --max-invoices 1
Run-Glass-Closeout.cmd --Max 1
Run-Glass-Closeout.cmd --invoice-number 5485994
Run-Glass-Closeout.cmd
```

Use `--mva` for a single-target work-item close and the default command for batch
closeout. For development/validation, `--max-invoices N` (also `--Max N`)
selects at most N eligible invoice emails, oldest first, **once before changes**.
Only those same invoices can update the Sheet/queue, close associated work items,
pass invoice validation, approve the exact FieldPO PO, close its FieldPO work
order, and enter email finalization. Pre-existing queue rows for
other MVAs are not processed. This is a live run, not a dry run.

Use `--invoice-number N` to select an exact invoice number across all closeout
stages. A PO number in the email subject is optional: the invoice PDF must
contain exactly one PO, FieldPO must return that same PO, and any PO present in
the subject must be valid and match the PDF. Invoice-scoped options cannot be
combined with `--mva`, `--csv`, `--max-rows`, or
`--sweep-terminal-to-needs-review`. Per-invoice problems do not
stop the pass: an invoice whose email/PDF cannot be captured (for example, a
vendor recalibration invoice for BB/APO), fails its Sheet check, or whose Glass
work item fails to close is moved to **Needs Review** and the rest of the
selection continues. For each invoice that passes identity validation after
Compass, FieldPO is rechecked immediately before approval: the PDF PO must match
the FieldPO PO (a subject PO is optional, but must match if present), VIN, Sheet
MVA, and authorized amount must match exactly, PO status must be `IN PROGRESS`,
and Work Order Created By must be `Steele, Dirk`. There is no minimum invoice
age. Approval closes the FieldPO work order; the exact PO and closure must both
be confirmed before the email moves to `Processed`. Any approval gate or
confirmation failure sends only that invoice to **Needs Review**. If a navigation failure stops the remaining work-item
closes, those not-attempted invoices are left in place for the next run.
System failures (Outlook, Sheet connection, Edge launch, or missing/mismatched
work-item results) still stop the run before invoice validation. Reviewed
invoices count toward the `--max-invoices` limit; no other invoice is
substituted. Recalibration and documented history/checkpoint
exclusions do not create an additional Glass work-item target.

Work-item closeout launches Edge with the configured `edge_user_data_dir` and
`edge_profile_directory` (or their `PLAYWRIGHT_EDGE_*` environment overrides).
Other Edge sessions using different user data directories do not block startup.
Closeout never terminates existing browser processes. A launch failure stops the
run and reports the configured profile and the Playwright error. If that user
data directory is already in use, close its Edge session before retrying; no
alternate profile or automatic retry is used.

The older component scripts remain in the repository for reference and troubleshooting, but they are deprecated and no longer the primary daily commands.

`Run-GlassOrchestrator.cmd` bootstraps the runtime by creating `.venv` (if missing),
installing `requirements.txt`, then launching `GlassOrchestrator.py` with the venv interpreter.

`Run-GlassOrchestrator.cmd`, `Run-FieldPOFillNextAction.cmd`, `Run-EnsureGlassWorkItems.cmd`, `Run-BuildCloseQueue.cmd`, `Run-CloseWorkItems.cmd`, `Run-CompletedInvoices.cmd`, and `Run-PayCompletedInvoices.cmd` are still available for reference and targeted troubleshooting, but the primary daily commands are now `Run-Glass-Intake.cmd` and `Run-Glass-Closeout.cmd`.

Or run directly with the virtual environment interpreter:

```bash
.venv\Scripts\python.exe GlassOrchestrator.py
```

### Ensure Glass Complaints and Work Items

Standalone processing requires an explicit target:

```bash
Run-EnsureGlassWorkItems.cmd --inventory-date 2026-09-25
```

Optional modes:

```bash
Run-EnsureGlassWorkItems.cmd --mva 058524185
Run-EnsureGlassWorkItems.cmd --mva 058524185 --dry-run
Run-EnsureGlassWorkItems.cmd --csv WorkItems\create_workitem.csv
```

The workflow creates a missing Glass complaint before creating its work item and skips an MVA only when both already exist. CSV mode is Glass-only and uses the existing `mva,Type,location,action` schema.

Standalone FieldPO reruns also require an explicit target:

```bash
Run-FieldPOFillNextAction.cmd --inventory-date 2026-09-25
```

### Run All Tests (1-click)

```bash
Run-Tests.cmd
```

`Run-Tests.cmd` will:
- create `.venv` automatically if missing,
- install `requirements.txt`,
- run the full pytest suite under `tests/`.

Optional: pass specific targets to run a subset.

```bash
Run-Tests.cmd tests/test_unit.py
```

### Dead-Code Survey (runtime usage coverage)

Find code that exists but is never used in real operation. The survey
instruments every Python process launched from the project venv — no
separate instrumented build is needed.

```bash
Run-CoverageSurvey.cmd start    # enable recording (writes a hook into .venv)
Run-CoverageSurvey.cmd status   # show enabled/disabled and collected data file count
Run-CoverageSurvey.cmd report   # merge data and build htmlcov\index.html
Run-CoverageSurvey.cmd stop     # disable recording; collected data is kept
```

Typical use: `start`, run the normal workflows for a week, then `report`.
Files and functions at 0% in the report never executed during the survey
window — candidates for removal. The report can be rebuilt mid-survey at
any time; data accumulates across runs.

Notes:
- Configuration lives in `.coveragerc`; data files (`.coverage.*`) and the
  HTML report (`htmlcov/`) are gitignored.
- Running `pytest` while the survey is active counts test-exercised code
  as "used" — avoid test runs during the survey window.
- The hook lives inside `.venv`, so rebuilding the venv silently disables
  the survey; run `Run-CoverageSurvey.cmd status` to confirm it is active.

## File Layout

```
GlassOrchestrator.py     # Main 6-phase pipeline
Service_account.json     # Google service account key (not committed)
src/
  GlassDataParser.py    # Phase 3 worker (Selenium scraper)
core/
flows/
pages/
utils/
config/
data/
  GlassDataParser.csv    # Phase 3 input (auto-generated)
GlassResults.txt         # Phase 3 output (worker-produced)
```

## Failure Handling

- Each phase is wrapped in its own `try/except` block.
- **Phase 3 failure aborts the entire pipeline** — no data is persisted or notified.
- An individual MVA reported as not found is not a Phase 3 failure. Phase 3 writes
  `VIN=N/A` and `Desc=MVA Not Found`, then continues with the next MVA.
- When enabled, Phase 6 notification failure is logged but does not lose persisted data.
