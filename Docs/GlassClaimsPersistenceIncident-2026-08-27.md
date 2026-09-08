# GlassClaims Persistence Incident - 2026-08-27

## Summary

On 2026-08-27, new GlassOrchestrator rows were written below the GlassClaims summary instead of immediately after the last table entry. The last existing claim was row 2589. The expected insertion row was 2590.

No persistence code change occurred between the successful 2026-08-26 runs and the incident.

## Observed Timeline

- 2026-08-26 08:24:09: 9 rows requested at row 2571.
- 2026-08-26 08:25:34: 9 rows requested at row 2580.
- 2026-08-27 07:46:24: 8 rows requested at row 2592.
- The misplaced entries were deleted manually and the same source message was intentionally made available for another run.
- 2026-08-27 07:50:54: the rerun requested 8 rows at row 2594.
- Both 2026-08-27 runs processed Gmail UID 4544. This was expected for the intentional rerun and was not the cause of the placement error.
- 2026-08-27 08:27:54: corrected persistence detected summary row 2592 and last data row 2589, then inserted 6 rows at row 2590.
- 2026-08-27 08:29:37: corrected persistence detected summary row 2598 and last data row 2595, then inserted 8 rows at row 2596.
- Visual inspection confirmed that both corrected writes landed at the intended table boundary.

## Confirmed Code Defects

The previous `_find_insert_row()` implementation did not identify the table summary as a boundary. It scanned every row returned by Google Sheets and selected the row after the last non-empty value in the chosen MVA column.

The helper derived the MVA column from the first sheet row and silently fell back to column B when that row did not contain the exact `MVA` value. The table header originally occupied frozen row 1. At some point, an extra row was inserted above it, shifting the header to row 2 and making the new blank row 1 frozen. The old implementation then failed to find `MVA` in row 1. Column B contains both data and `Avg Repair Days` summary values, so summary content affected the selected insertion row.

The installed `gspread 6.2.1` implementation of `insert_rows()` performed two operations:

1. Insert row dimensions at the requested row.
2. Write values with the Google Sheets logical-table append API.

The second operation allowed Google Sheets to determine the value destination from its logical-table interpretation instead of writing to a fixed range.

## Confirmed Sheet-Side Trigger

The table header was previously frozen row 1. An extra row was later inserted above it, leaving blank frozen row 1 and moving the actual header to row 2. This structural change exposed the script's row-1-header assumption and caused it to use column B as its fallback. The summary values in column B then moved the calculated insertion point below the table summary.

The exact time and source of the sheet row insertion are not known. Based on the last successful run and the first affected run, the likely investigation window is between 2026-08-26 08:25 and 2026-08-27 07:43.

GlassOrchestrator is not a plausible source of the row inserted above the header:

- Historical logs show its row insertions occurred near the bottom of the data table, not at row 1.
- The old insertion helper could not select a row above row 2.
- The corrected helper also cannot select row 1.
- No other current repository code inserts Google Sheet row dimensions.

Possible sources include a manual `Insert 1 row above` operation, an external Google Sheets automation, or another bulk sheet operation outside this repository. Google Sheets version history should be used to locate the first revision where the header moved from row 1 to row 2 and, where available, identify the editor.

Additional visual inspection found that the `Inventory Date` header cell had been overwritten with a VIN-like value. This is not a value GlassOrchestrator writes into the Inventory Date column and is consistent with an accidental manual copy/paste or nearby sheet edit. The current leading explanation is that the blank row insertion, Inventory Date header overwrite/rename, and header shift were part of the same human editing sequence. This attribution is strongly supported by the sheet state but is not proven without Google Sheets version history.

The correction removes both dependencies: it uses the configured MVA column instead of a row-1 header lookup, and it writes to an exact range instead of relying on logical-table append behavior.

## Correction

GlassClaims persistence now follows one strict path:

1. Use the configured GlassClaims column contract to select the MVA column (column C); never fall back to another sheet column.
2. Locate exactly one summary row containing the expected markers in their configured positions: `Avg Repair Days`, `Repairs`, `Claims`, and `Total`.
3. Scan upward from that summary to the nearest non-empty MVA row.
4. Insert immediately after that row. Blank separator rows do not affect the result.
5. Insert row dimensions with formatting inherited from the preceding data row.
6. Write values to the exact inserted `A:K` range instead of using logical-table append behavior.
7. Abort persistence with a clear error if the unique summary boundary is unavailable.

For the incident layout, with data ending at row 2589, blank rows 2590-2591, and the summary at row 2592, the selected insertion row is 2590.

## Regression Coverage

Integration tests cover:

- Data separated from the summary by blank rows.
- Configured MVA-column selection when the live sheet's first row is blank.
- Missing summary rejection.
- Duplicate summary rejection.
- A valid empty table inserting at row 2.
- Exact row-dimension insertion and exact-range value updates.
- Existing Original Date and row serialization behavior.

No live Google Sheet write is performed by the automated tests.
