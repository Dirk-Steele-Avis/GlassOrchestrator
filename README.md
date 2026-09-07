# GlassOrchestrator (GPO)

A modular Python pipeline for vehicle glass procurement, built on a **6-phase architecture**.

Canonical behavioral requirements are maintained in
[Docs/GlassOrchestratorRequirements.md](Docs/GlassOrchestratorRequirements.md).

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

`Run-GlassOrchestrator.cmd` bootstraps the runtime by creating `.venv` (if missing),
installing `requirements.txt`, then launching `GlassOrchestrator.py` with the venv interpreter.

Or run directly with the virtual environment interpreter:

```bash
.venv\Scripts\python.exe GlassOrchestrator.py
```

### Ensure Glass Complaints and Work Items

The consolidated launcher defaults to valid rows whose `Inventory Date` is today:

```bash
Run-EnsureGlassWorkItems.cmd
```

Optional modes:

```bash
Run-EnsureGlassWorkItems.cmd --dry-run
Run-EnsureGlassWorkItems.cmd --mva 058524185
Run-EnsureGlassWorkItems.cmd --csv WorkItems\create_workitem.csv
```

The workflow creates a missing Glass complaint before creating its work item and skips an MVA only when both already exist. CSV mode is Glass-only and uses the existing `mva,Type,location,action` schema.

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
- When enabled, Phase 6 notification failure is logged but does not lose persisted data.
