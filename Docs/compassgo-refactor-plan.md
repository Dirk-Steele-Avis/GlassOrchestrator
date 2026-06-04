# Plan: CompassGoParser Vertical Slice (TDD)

Build a validated vertical slice for one MVA lookup against the new Compass GO PWA (`https://go.avisbu...`), replacing the deprecated Compass Mobile scraper. Single MVA in → `MVA,VIN,Desc` row out. Playwright + persistent Edge profile, lightweight POM, TDD-first.

## Branch
- Working branch: `feature/initial` (off `dev`, off `main`)

## Decisions
- **Library:** Playwright (msedge channel).
- **Auth:** profile attach via `chromium.launch_persistent_context(user_data_dir, channel="msedge")`. Auto-handle "Welcome back" Continue page and Location picker as separate pages.
- **File placement:** new `src/compass_go/` package + entrypoint `src/CompassGoParser.py`. Legacy `src/GlassDataParser.py` left in place until slice is validated, then archived.
- **Locator strategy:** prefer stable `data-key` attributes on `<tr>` (React-Aria generated `id`s are NOT stable). Role-based locators for buttons/inputs. No XPath unless required.
- **Output contract:** unchanged from orchestrator — `MVA,VIN,Desc` comma-separated to `GlassResults.txt`, no header, append + flush per row, missing values written as `N/A`.
- **POM scope:** lightweight — 4 pages + 1 component. Each page asserts its readiness anchor.
- **TDD:** each page/flow gets a failing test first using recorded HTML fixtures loaded via `page.set_content()`, then implementation.

## Page Object Model

| Class | Anchor (ready signal) | Public API |
|---|---|---|
| `LoginConfirmPage` | "Welcome back!" heading | `continue_as_current_user()`, `is_displayed()` |
| `LocationPickerPage` | "Now, choose your location" heading | `finish_setup()`, `is_displayed()` |
| `ScanPage` | textbox + "Enter" button | `submit(mva) -> VehicleDetailsPage` |
| `VehicleDetailsPage` | "Vehicle Details" heading | `expand_show_more()`, `read(data_key) -> str`, `back()` |
| `BottomNav` (component) | bottom tab container | `goto_scan()` |

## Module structure

```
src/CompassGoParser.py            # entrypoint: argparse + main loop
src/compass_go/
  __init__.py
  session.py        CompassGoSession      # browser/profile lifecycle, edge-kill
  auth_flow.py      AuthFlow              # composes LoginConfirm + LocationPicker -> ScanPage
  scrape_flow.py    ScrapeFlow            # loop: submit -> expand -> read -> write -> back
  pages/
    login_confirm_page.py
    location_picker_page.py
    scan_page.py
    vehicle_details_page.py
    bottom_nav.py
  records.py        VehicleRecord (dataclass), SearchOutcome (enum)
  writer.py         ResultsWriter         # GlassResults.txt format + flush
  waits.py          ReadinessWaits        # race-pair helpers
tests/compass_go/
  fixtures/                               # captured HTML snippets for page.set_content() tests
  test_results_writer.py
  test_vehicle_details_page.py
  test_scan_page.py
  test_auth_flow.py
  test_scrape_flow_integration.py
```

## Phases

### Phase 1 — Probe & confirm locators (no code commit)
1. Manual DOM inspection in Edge DevTools to capture `data-key` for Description and MVA rows.
2. Capture HTML for: Show More button, Scan textbox, Enter button, Continue button, Finish Setup button, back arrow.
3. Save snippets into `tests/compass_go/fixtures/` as static HTML for unit tests.

### Phase 2 — TDD scaffolding (no live runtime yet)
1. Create empty package + failing tests for `ResultsWriter` (pure unit, no browser). Implement.
2. Create failing test for `VehicleDetailsPage.read(data_key)` using fixture HTML loaded via `page.set_content()`. Implement.
3. Same TDD cycle for `ScanPage`, `LoginConfirmPage`, `LocationPickerPage`, `BottomNav`.

### Phase 3 — Live integration
1. Implement `CompassGoSession` (mirrors profile-attach pattern in `WorkItems/create_workitem.py`).
2. Implement `AuthFlow` — detect Welcome back / Location picker / already-on-Scan; act idempotently.
3. Implement `ScrapeFlow` — single-MVA loop body.
4. Implement `CompassGoParser.py` entrypoint — reads `data/GlassDataParser.csv`, writes `GlassResults.txt`.
5. Dry run with one MVA. Verify output matches contract.

### Phase 4 — Orchestrator wire-up
1. Flip `GlassOrchestrator.py` `WORKER_SCRIPT` constant from `src/GlassDataParser.py` to `src/CompassGoParser.py`.
2. End-to-end run with small MVA set (3-5 entries).
3. Archive legacy `src/GlassDataParser.py` to `archive/` once green.

## Relevant existing files (reuse / reference)
- `WorkItems/create_workitem.py` — profile-attach + edge-kill pattern to mirror in `CompassGoSession`
- `playwright_prototype/config.py` — `resolve_edge_user_data_dir()`, `resolve_edge_profile_directory()`, `resolve_headless()` helpers to reuse
- `core/driver_manager.py` — legacy Selenium driver; not used by new worker but stays for non-orchestrator scripts
- `GlassOrchestrator.py` — `WORKER_SCRIPT` constant (only line that changes in Phase 4)
- `src/GlassDataParser.py` — legacy worker; read-only reference during build, archived after Phase 4

## Verification
1. `pytest tests/compass_go/ -v` — all unit + integration tests green
2. Manual single-MVA dry run: `.venv\Scripts\python.exe src\CompassGoParser.py` with one MVA in `data/GlassDataParser.csv`, asserts `GlassResults.txt` row format
3. Full orchestrator run via `Run-GlassOrchestrator.cmd` after Phase 4

## Open items (gathered during implementation)
- Confirm `data-key` for Description row (likely `desc` or `description`)
- Confirm `data-key` for MVA row (likely `mva` or `mvaNo`)
- Confirm Show More button selector (text-based fallback if no stable attr)
- Confirm whether Location picker appears every session or only on location change
- Confirm whether logout is needed at teardown (default: no; leave session intact for next run)

## Scope boundaries
**Included:** single-MVA lookup end-to-end producing `MVA,VIN,Desc` row.
**Excluded:** complaints scraping, work item creation/closure, mileage, inspections, MPVI walkthrough, On Lot / Off Lot tabs, scanner camera input.
