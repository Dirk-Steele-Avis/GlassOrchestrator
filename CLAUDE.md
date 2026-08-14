# GlassOrchestrator (GPO)

## Project overview
Modular Python pipeline for vehicle glass procurement. Fetches scan data from Gmail via IMAP, parses MVA codes, scrapes VIN data, merges results, persists to Google Sheets, and sends HTML email notifications. 6-phase architecture where Phase 3 failure aborts the entire pipeline.

## FRA Profile

- **name:** GlassOrchestrator
- **repo:** GlassOrchestrator
- **stack:** Python, Selenium, Google Sheets API, Gmail IMAP, CSV
- **deploy:** Manual — Run-GlassOrchestrator.cmd bootstraps venv and launches pipeline
- **datasources:** Gmail IMAP (export@orcascan.com), Google Sheet (ATL_Data 2026 : GlassClaims tab), GlassDataParser.csv (Phase 3 input), GlassResults.txt (Phase 3 output)
- **testing:** pytest suite in `tests/` — unit, integration, failure, config, cycle tracker, and driver manager tests. Run with `.venv\Scripts\python.exe -m pytest tests/`
- **git:** https://github.com/dirkatavis/GPO
- **owners:** Dirk Steele
- **deployNotes:** Requires Service_account.json (Google service account key) in project root — not committed to repo. Environment variables must be set before first run: GLASS_EMAIL_ACCOUNT, GLASS_EMAIL_PASSWORD, GLASS_SENDER, GLASS_NOTIFY_RECIPIENTS, GLASS_LOGIN_USERNAME, GLASS_LOGIN_PASSWORD, GLASS_LOGIN_ID. Local config overrides (orchestrator_config.local.json, config/config.local.json) are gitignored.
- **gotchas:** Phase 3 failure aborts the entire pipeline — no data is persisted or notified. An individual MVA reported as not found is nonfatal: write `VIN=N/A` and `Desc=MVA Not Found`, then continue with the next MVA. Idempotency key for Google Sheet is MVA + Arrival Date — duplicate rows are prevented by this 2-part key. Location is extracted from the Type column suffix in the email HTML table (e.g., '0420APO' → APO, '0420BB' → BB). Missing VIN scraper results write N/A, not null or blank. Suffix rules: r = Repair, c = Claim listed; no suffix defaults to Replacement + Missing. Phase 5 inserts rows above the summary section — row insertion position matters.

## Build and run
- Setup: `Run-Setup-GlassEnv.cmd`
- Run: `Run-GlassOrchestrator.cmd`
- Direct: `.venv\Scripts\python.exe GlassOrchestrator.py`
