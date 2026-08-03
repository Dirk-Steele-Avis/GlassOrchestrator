# GlassClaims Replacement Volume Report

Generated: 2026-07-02

## Scope

This report summarizes replacement glass volume in the `GlassClaims` Google Sheet for May and June 2026.

## Methodology

- Loaded the `GlassClaims` worksheet from the configured spreadsheet.
- Parsed `Inventory Date` values into calendar months.
- Counted rows whose `Action` field contains the text `replace`.
- Focused on replacement volume for May and June 2026.

## Summary

- **May 2026 replacement rows:** 366
- **June 2026 replacement rows:** 239

## Notes

- The count includes rows where the `Action` field values match `Replacement`, `Replace(AGN)`, or other replacement-related strings.
- The report is based on the current live Google Sheet data at the time of generation.
