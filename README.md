# GlassOrchestrator (GPO)

A modular Python pipeline for vehicle glass procurement, built on a **6-phase architecture**.

## Architecture

| Phase | Name | Description |
|-------|------|-------------|
| 1 | **Input** | Fetch scan data from Gmail (`export@orcascan.com`) via IMAP |
| 2 | **Parsing** | Regex triage (`^(\d{8})([rc]*)$`), build session manifest |
| 3 | **Worker** | Write MVAs to CSV, invoke `GlassDataParser.py` subprocess |
| 4 | **Data Merge** | Left-join manifest with scraper results; missing VIN → `N/A` |
| 5 | **Persistence** | Append all parsed rows to Google Sheet (`GlassClaims` tab); carry forward Original Date within incident window |
| 6 | **Notification** | HTML email for Replacement items; red-flagged rows for missing VINs |

## Suffix Rules

| Suffix | Field | Value | Default (no suffix) |
|--------|-------|-------|---------------------|
| `r` | Damage Type | Repair | Replacement |
| `c` | Claim# | Listed | Missing |

## Data Contract — `ATL_Data 2026 : GlassClaims`

The pipeline output maps 1-to-1 with the `GlassClaims` tab in the master workbook.
Phase 5 inserts rows above the summary section and always appends rows (no deduplication).

For returning MVAs, `Original Date` is carried forward from existing sheet rows only when
the prior row's `Inventory Date` is within `incident_window_days` (default: 3 days) of the
new row's `Inventory Date`.

| # | Column | Source | Phase | Notes |
|---|--------|--------|-------|-------|
| 1 | **Inventory Date** | Email Type/date parsing | 2 | `MM/DD/YYYY` |
| 2 | **Original Date** | Inventory Date at first sighting; carried forward for same incident | 2/5 | Uses earliest qualifying prior value within incident window |
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

`Run-GlassOrchestrator.cmd` bootstraps the runtime by creating `.venv` (if missing),
installing `requirements.txt`, then launching `GlassOrchestrator.py` with the venv interpreter.

Or run directly with the virtual environment interpreter:

```bash
.venv\Scripts\python.exe GlassOrchestrator.py
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
- Phase 6 (notification) failure is logged but does not lose persisted data.
