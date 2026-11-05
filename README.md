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

The day-to-day Glass flow is split into two commands:

```bash
Run-Glass-Intake.cmd
Run-Glass-Closeout.cmd
```

Before first run (or when credentials change), run the interactive env setup:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup-GlassEnv.ps1
```

If you only need to set the login password, use:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File .\Set-GlassPassword.ps1
```

### Simplified Glass workflow

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

The unused standalone launchers were removed in the dead-code cleanup. The
supported launchers are `Run-Glass-Intake.cmd`, `Run-Glass-Closeout.cmd`,
`Run-Glass-Morning-Report.cmd`, and `Run-CloseWorkItems.cmd` (plus the shared
`Run-GlassBootstrap.cmd` they call).

The legacy single-command pipeline can still be run directly with the virtual
environment interpreter:

```bash
.venv\Scripts\python.exe GlassOrchestrator.py
```

### Ensure Glass Complaints and Work Items

Standalone processing requires an explicit target and runs directly with the
venv interpreter:

```bash
.venv\Scripts\python.exe create_compass_complaints.py --inventory-date 2026-09-25
.venv\Scripts\python.exe create_compass_complaints.py --mva 058524185
.venv\Scripts\python.exe create_compass_complaints.py --mva 058524185 --dry-run
```

The workflow creates a missing Glass complaint before creating its work item and skips an MVA only when both already exist.

Standalone FieldPO reruns also require an explicit target:

```bash
.venv\Scripts\python.exe FieldPOFillNextAction.py --inventory-date 2026-09-25
```

### Run All Tests

```bash
.venv\Scripts\python.exe -m pytest tests\
```

Optional: pass specific targets to run a subset.

```bash
.venv\Scripts\python.exe -m pytest tests/test_unit.py
```

## File Layout

```
GlassOrchestrator.py     # Main 6-phase pipeline
Service_account.json     # Google service account key (not committed)
src/
  CompassGoParser.py     # Phase 3 worker (Compass GO Playwright scraper)
  compass_go/            # Compass GO scrape flow, pages, session
core/
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
