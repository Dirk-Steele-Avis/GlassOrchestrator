# Compass Complaint Automation Requirements (Draft)

## Purpose
Create a batch process that reads MVAs from the Gmail spreadsheet for the current day and creates Compass glass complaints and work items when needed.

## Source Pattern
- Mirror the existing FieldPO/FillNextAction and run-batch patterns in this repository.
- Do not introduce a new workflow style unless required.

## Data Source
- Read from this Google Sheet:
  - https://docs.google.com/spreadsheets/d/1eltlDO-nt-rBicbz_h3CmPc4g0TJNR9wFcsAw2ngNvs/edit?gid=889235386#gid=889235386
- Read values from the `MVA` column.
- Process only rows for the current day (local system date on the machine running the script).
- Current-day filtering uses `Inventory Date`.
- Process every row occurrence (no row-level dedupe).

## High-Level Workflow
1. Read spreadsheet rows.
2. Keep rows where Inventory Date matches the run day (following existing FPO date-handling pattern).
3. Validate MVA.
4. Use MVA to look up the vehicle in Compass.
5. Check for an existing complaint.
6. If matching complaint exists, skip.
7. If no matching complaint exists, create complaint and work item in one pass.
8. Continue until all qualifying rows are processed.
9. Print and log end-of-run summary counts.

## Existing Complaint Rule
Skip creation when all are true:
- Same MVA
- Complaint type is Glass Repair/Replace
- Complaint is open (current assumption: status OPEN)

## Create Rule
If no matching open glass complaint exists:
- Create a new complaint
- (Next phase) create a work item after complaint creation is verified stable

Complaint create fields (confirmed):
- Click `Create Complaint`
- `Is Vehicle Drivable?` -> `Yes`
- `Category` -> `Glass Damage`
- `Complaint Description` -> `Glass Damage`
- Click `Submit Complaint`
- If `Submit Complaint` remains disabled, select a glass sub-category (default fallback: `Windshield Crack`) and submit.

Latest validation status:
- Complaint creation flow has succeeded in LIVE mode for test MVA `058524185`.

## UI Assumptions
- Adding a new complaint starts by clicking the New Complaint button.
- Similar low-level button/navigation details may be assumed when they are standard and already present in the existing Compass UI pattern.

## Captured Selectors
- HomePage / Vehicles button: `[data-test-id='workshop-inline-button']` with text `Vehicles`
- HomePage / vehicle scan input: `[data-testid='mva-vin-input']`
- HomePage / submit button: `[data-testid='mva-vin-submit']`
- HomePage / keyword search input: `input[type='search'][placeholder='Keyword Search (other fields)']`
- Complaint/workshop table container: `[data-test-id='workshop-object-table']`
- Complaint/workshop row title cell: `[data-test-id='workshop-object-title']`

## Observed Row Structure
- Table rows are rendered as bp6-table cells with `bp6-table-cell-row-*` / `bp6-table-cell-col-*` classes.
- The title text is exposed in the row title cell, and adjacent cells show other row fields.
- The prototype should read row text/field values directly from the table, not open or select rows.
- If the matching complaint does not exist, the row structure will not be present; treat that as a clean "no complaint exists" result.
- Real glass complaint example:
  - Title: `Glass Damage` (exact match required)
  - Damage Part: `Windshield Crack`
  - Created By: `Dirk Steele`
  - Date: `Aug 3, 2026, 6:12 PM`

## Error and Skip Behavior
- Missing/blank/invalid `Inventory Date`: skip.
- Missing/invalid `MVA`: skip and log.
- MVA lookup failure: log error and skip.
- Complaint/work item creation failure: log error and skip.
- Continue batch processing after errors.

## Logging and Reporting
- Keep a simple append log file.
- Mirror existing naming/location patterns used by current FPO flow.
- Include DRY-RUN entries in the same append log with clear DRY-RUN markers.
- Include end-of-run totals such as:
  - total rows read
  - rows for day
  - skipped existing complaint
  - created
  - failed
  - skipped invalid

## Run Modes
- Normal run: performs create actions (complaint creation first).
- Dry run: performs checks and logs what would be created, without creating complaints/work items.

## Thin Prototype Boundary
- Lookup/skip behavior is validated and running against the sheet-driven flow.
- Process all qualifying rows for the current day (1-MVA cap removed).
- Immediate implementation focus: create complaint when no existing `Glass Damage` complaint is present.
- Do not select or click complaint rows for the prototype; confirm existence by reading the table/list state directly.
- Sample validation MVA: 909174550.
- MVA normalization: if an MVA is 8 digits long, prepend a leading 0 so the lookup uses 9 digits.

## Spreadsheet Writeback
- Not required for V1.
- For now, track outcomes in local log only.
- Potential future enhancement: write results back to spreadsheet.

## New Entrypoint (Approved)
- Python script: create_compass_complaints.py
- CMD wrapper: Run-CreateCompassComplaints.cmd

## Deferred to Implementation
- Final status mapping for what counts as open vs closed beyond current OPEN assumption.
- Exact field-level values for complaint/work item forms.
- Final selector-level details and navigation reliability tuning.

## Implementation Checklist (V1)
1. Scaffold files
- Create create_compass_complaints.py using the same structure pattern as FillNextAction flow scripts.
- Create Run-CreateCompassComplaints.cmd using existing run-batch wrapper conventions.

2. Load input data
- Reuse existing spreadsheet access/config pattern used by the current FPO process.
- Read rows and normalize Inventory Date and MVA values.
- Read MVA values from the `MVA` column only.

3. Filter rows for run day
- Apply Inventory Date-only filtering using the same date handling pattern as current FPO code.
- Keep all matching rows for the day (including repeated MVAs).

4. Validate row-level prerequisites
- If Inventory Date missing/invalid, log and skip.
- If MVA missing/invalid/non-numeric, log and skip.

5. Lookup and evaluate complaint state
- Use MVA to locate vehicle in Compass.
- Detect existing complaint match on: MVA + type Glass Repair/Replace + OPEN status.
- If match found, log as skipped existing and continue.

6. Create when eligible
- Create complaint when no matching open complaint exists.
- After complaint creation is stable, add work item creation in the same row flow.
- If creation step fails, log error and continue.

7. Add dry-run mode
- Support dry-run flag that performs read/filter/lookup/check and logs intended actions.
- Prevent complaint/work item creation in dry-run.
- Mark all dry-run records clearly in the same append log.

8. Logging and summary
- Append to a single run log file following existing FPO naming/location pattern.
- Include per-row outcome entries and end-of-run summary counts.

9. Validation run sequence
- Dry-run test against a day with known MVAs.
- Normal run test on a small safe sample day.
- Confirm skip behavior for existing OPEN Glass Repair/Replace complaint.
- Confirm error handling continues processing remaining rows.

10. Documentation touch-up
- Add quick usage notes to README or script header for normal and dry-run execution.
