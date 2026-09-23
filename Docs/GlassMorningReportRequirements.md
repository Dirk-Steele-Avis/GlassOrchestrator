# Glass Damage Morning Report Requirements

## Purpose

The morning report is a standalone, read-only workflow run manually after
`Run-Glass-Intake.cmd`. It creates a polished HTML snapshot for the operator to
paste into an Outlook email. It does not create or send an email.

## Operator Workflow

1. Complete `Run-Glass-Intake.cmd`.
2. Run `Run-Glass-Morning-Report.cmd`.
3. Review the report opened in the default browser.
4. Click **Copy report for Outlook** and paste it into the email body.
5. Address, review, and send the email manually.

## Data Contract

- The source is the configured Google workbook and `GlassClaims` tab.
- Required headers are `Inventory Date`, `Original Date`, `MVA`, `Next Action`,
  `VIN`, `Make`, `Location`, `Action`, `Area`, `Claim#`, and
  `WorkItemCreated`.
- A row is included only when its `Inventory Date` equals the current local
  date. Sheet filters and manually hidden rows do not control eligibility.
- Rows without a VIN are excluded so blank and summary rows do not appear.
- The workflow shall not modify the workbook.
- Failure to read values, configuration, or any required header shall stop
  report generation with a visible error.

## Report Contract

- Reports are saved under the repository-root `reports/` folder.
- The default filename is dated: `glass_morning_report_YYYY-MM-DD.html`.
- Running the report again on the same day replaces that day's report; reports
  from prior dates remain available.
- The browser preview shall match the approved red, black, and white report
  design and use inline table styling for Outlook compatibility.
- The browser preview shall show the fixed Outlook report on desktop and a
  readable card layout on phone-width screens. The responsive presentation
  shall not change the fixed table markup copied into Outlook.
- Sections appear in this order when nonempty: AGN (Replacements), AGN Repair,
  Super Glass (Repairs), AVIS (TBK), Local Market, Other.
- Rows are ordered oldest first within each section.
- The attention summary reports photo/action issues, units open at least 14
  days, missing claims, and units new today.
- The report shall HTML-escape all values read from the sheet.
- Report generation shall never send email automatically.

## Configuration

The script uses the existing orchestrator configuration precedence and accepts
`GLASS_SPREADSHEET_ID` as the spreadsheet ID override. The configured Google
service account must have read access to the workbook.