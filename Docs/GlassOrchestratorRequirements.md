# GlassOrchestrator Requirements

## Purpose

GlassOrchestrator processes vehicle-glass scan exports from Gmail, retrieves vehicle data, writes claim rows to the `GlassClaims` Google Sheet, and optionally sends replacement notifications.

## Pipeline Requirements

1. The pipeline shall retrieve unread Orca Scan emails from the configured sender one at a time, newest received first (highest numeric IMAP UID).
2. The pipeline shall validate the email structure and dates as one batch. A scan that does not match the scan format shall be logged and skipped without rejecting valid sibling scans. The pipeline shall not guess a correction to an unrecognized scan.
3. The pipeline shall invoke the vehicle-data worker for every valid MVA.
4. A worker failure or stale worker output shall abort the run before persistence.
5. The pipeline shall merge worker results into the parsed manifest. Missing results shall use `VIN=N/A`.
6. The pipeline shall insert all output rows above the Google Sheet summary section.
7. After successful persistence and notification handling, the pipeline shall run FieldPO and then Compass for all sheet rows matching that email's Type date.
8. A FieldPO failure shall stop the batch before Compass starts.
9. The pipeline shall mark the source email as read only after FieldPO and Compass succeed.
10. Any fatal failure shall return a nonzero process result, leave the current source email unread, and stop before the next unread email. Skipped malformed scans are nonfatal when valid scans remain; the source email shall be marked read after those valid scans complete the normal workflow. An email with no valid scans shall fail before worker execution and remain unread.

## Scan Format

The permanent scan format is:

```text
<MVA><AREA>[r|war|tbk][c][ OEM]
```

Warranty also supports the area-free form `<MVA>war[c]`.

- `MVA` is exactly eight digits.
- `AREA` is required and must be a configured area code.
- Directional area codes use `<side><orientation><area>` order; for example, `LFD` means Left Front Door.
- Configured `legacy_area_aliases` temporarily normalize older orientation-first codes to canonical directional codes.
- `r` means Repair when valid for the area.
- `war` means Warranty.
- `tbk` means Turnback and shall route the glass order to AVIS.
- `c` means the claim is Listed.
- ` OEM` is an optional terminal OEM marker preceded by one space.
- Matching is case-insensitive.

Examples:

| Scan | Result |
|---|---|
| `62155855WS` | AGN windshield replacement, claim Missing |
| `62155855WSc` | AGN windshield replacement, claim Listed |
| `62155855WSr` | SuperGlass windshield repair, claim Missing |
| `62155855WSwar` | Warranty windshield claim, claim Missing |
| `62155855WS OEM` | AVIS OEM windshield replacement, claim Missing |
| `62155855WSc OEM` | AVIS OEM windshield replacement, claim Listed |
| `62155855LFD OEM` | AVIS OEM left-front-door replacement, claim Missing |

Bare OEM scans such as `62155855OEM` are invalid because they do not identify the glass area. Forms without the separating space, including `62155855WSOEM`, are also invalid.

An area-free warranty scan such as `59187380war` shall write `Action=Warranty`, `Area=Warranty`, and `Claim#=Missing`. The `warc` suffix shall write `Claim#=Listed`. This is the only damage type that may omit the physical glass area.

## Combined Location Email

1. A single Orca export may contain both BB and APO scans.
2. The Description value `break`, matched case-insensitively after trimming, shall delimit the two groups.
3. Scans before `break` shall use location BB; scans after `break` shall use location APO.
4. The delimiter shall not be processed as a scan.
5. A combined export shall contain exactly one delimiter and at least one scan overall. The delimiter may be the first row when BB has no scans or the last row when APO has no scans.
6. Type shall contain only the inventory date as numeric `MDD` or `MMDD`; it shall not contain a location suffix.
7. Every row shall use the same Type date. A three-digit value shall be left-padded to `MMDD` before date parsing.
8. The Type date shall equal the local processing date or the next calendar date. This permits current-day and night-before inventory, including year rollover.
9. Type shall be the authoritative Inventory Date for persistence, FieldPO, and Compass. The email header date and wall-clock date shall not replace it.
10. An email without a delimiter shall assign every scan to location APO. When BB has no scans, a delimiter at the top shall also assign every scan to APO.
11. Invalid email structure, delimiters, or Type dates shall abort parsing with a precise error and shall persist no rows from that email. Individual malformed scan values shall be logged and excluded while valid scans retain their original location assignment. Invalid repair or warranty/OEM combinations remain fatal validation errors.

## Downstream Date Scope

1. FieldPO and Compass shall receive the email Type date explicitly; neither stage shall infer `date.today()` or the latest sheet date.
2. Each stage shall process all eligible Google Sheet rows whose Inventory Date equals the explicit Type date.
3. If multiple unread emails share a Type date, each email shall trigger its own FieldPO-then-Compass pass after that email is persisted.
4. Standalone FieldPO processing shall require `--inventory-date YYYY-MM-DD` or an explicit rollback mode.
5. Standalone Compass processing shall require `--inventory-date YYYY-MM-DD`, `--mva`, or explicit CSV mode through its launcher.
6. No receipt-file, maximum-date, today, or tomorrow fallback shall be used to select downstream rows.

## OEM Requirements

1. OEM sourcing shall be supported for every configured glass area.
2. OEM shall describe sourcing and shall not be treated as a new damage type.
3. An OEM scan shall use internal Action `Replacement`.
4. An OEM scan containing `r` shall be normalized to Replacement and logged as a warning.
5. The Google Sheet Action shall be `Replace(AVIS)` for OEM rows.
6. The Google Sheet Area shall be the physical area with `(OEM)` appended, such as `Windshield(OEM)`.
7. Ordinary replacements shall continue to write `Replace(AGN)`.
8. Repairs shall continue to write `Repair(SuperGlass)`.
9. OEM rows shall remain eligible for the standard Replacement Compass work-item workflow.

## Google Sheet Contract

The orchestrator shall write the existing eleven columns in this order:

1. Inventory Date
2. Original Date
3. MVA
4. FPO#
5. VIN
6. Make
7. Location
8. Action
9. Area
10. Claim#
11. WorkItem

OEM does not add a sheet column. It is represented by `Action=Replace(AVIS)` and `Area=<physical area>(OEM)`.

## Vendor Recalibration Invoice Requirements

1. Local-market windshield replacements outside BB and APO have a separate vendor camera/ADAS recalibration invoice, always for exactly $150.00.
2. Replacement and recalibration invoices may share an MVA and VIN but shall be validated independently against their own invoice number and exact PO. The PDF must contain exactly one valid PO, which must match FieldPO. An email subject PO is optional; when present, it must be valid and match the PDF PO. Recalibration shall still pass the PDF/FieldPO PO, VIN, MVA, and authorized-amount checks.
3. BB (3333 Bobby Brown Parkway) and APO (4340 Global Gateway Connector) perform recalibration in-house. A vendor recalibration invoice for either location shall be blocked and reported for review, never automatically approved or marked Processed.
4. The invoice Customer Information street address, not its billing/remittance address or Sheet inventory location, shall determine BB/APO for this rule. Missing or ambiguous customer addresses on recalibration invoices shall block processing.
5. Recalibration shall be identified from its invoice line-item description, not from a $150 subtotal or warranty text.
6. A recalibration invoice shall not create another Glass work-item close candidate or overwrite the replacement Sheet cost. Existing MVA-level closure history shall not prevent independent invoice/PO validation.
7. Invoice validation does not itself approve or pay a PO. Invalid recalibration amounts shall be reported for review.
8. Close-candidate reporting shall explain every invoice that does not produce a new candidate. It shall distinguish recalibration, an existing queue entry, close-history membership, a same-day checkpoint, a duplicate MVA selected within the batch, explicit selection limits, and source/Sheet validation failures. Every explanation shall identify the invoice; no review explanations shall be truncated. A work-item exclusion shall not imply that its invoice or PO was approved or paid.

## Invoice-Scoped Closeout

### Processed Invoice Archive

1. A normal invoice search shall ensure `Inbox/AGN/Invoice/Invoice_Archive` exists and archive eligible messages from the `Processed` child folder before searching.
2. Only Outlook mail items directly in `Processed` whose `SentOn` timestamp is strictly more than 30 days before the archive run time shall be moved to `Invoice_Archive`. An item exactly 30 days old shall remain in `Processed`.
3. `Invoice` and `Needs Review` shall not be changed by the archive sweep. Archiving shall move, not delete, messages.
4. Invoice searches shall select only `Invoice`, `Processed`, and `Needs Review`; `Invoice_Archive` shall never be searched.

1. `Run-Glass-Closeout.cmd --max-invoices N` (alias `--Max N`) shall select at most N eligible invoice emails, oldest received first, once before Sheet, queue, or work-item changes. `--invoice-number N` shall scope the same stages to that exact invoice number.
2. Every closeout stage shall use that captured selection, including Sheet/queue updates, associated Glass work-item closure, invoice validation, exact FieldPO PO approval, FieldPO work-order closure, and Outlook finalization. Existing queue entries for unrelated MVAs shall not widen the run.
3. Per-invoice failures shall not stop the pass. A selected invoice that fails capture, fails its Sheet check, or whose Glass work-item closure ends in error, timeout, or navigation failure shall be moved to Needs Review with the failed step and reason, and excluded from later stages of that run. A Sheet-check failure shall not update that invoice's Sheet row or queue entry. Targets not attempted because a navigation failure stopped the work-item batch shall be left unchanged in Outlook for a later run. System failures (Outlook, Sheet connection, Edge launch, or missing or mismatched work-item results) shall stop before invoice validation. Reviewed invoices count toward the selection limit; the workflow shall not substitute another invoice after a failure or a documented work-item exclusion.
4. Recalibration and documented history/checkpoint outcomes shall retain their nonfatal work-item exclusions without excluding the selected invoice from its independent invoice validation.
5. Invoice-scoped closeout shall reject combinations with `--mva`, `--csv`, `--max-rows`, or `--sweep-terminal-to-needs-review` before external work. The limit shall be a positive integer. This mode performs live changes; it is not a dry run.
6. Work-item closeout shall launch Edge once with the configured user data directory and profile. It shall not block on unrelated Edge processes or terminate existing browsers. Launch failure shall stop closure before any work-item actions, report the configured directory/profile and original error, and shall not retry or select an alternate profile.
7. After Compass work-item closeout, each invoice that passed exact invoice, PDF PO, FieldPO PO, VIN, Sheet MVA, and authorized-amount checks shall be rechecked in FieldPO before action. The PDF PO shall equal the FieldPO PO; if the email subject contains a PO, that PO shall equal the PDF PO. The live FieldPO MVA shall equal the Sheet MVA, the invoice VIN shall equal the FieldPO VIN, the live authorized amount shall exactly equal the invoice amount, the PO shall be `IN PROGRESS`, and the work-order creator shall be `Steele, Dirk`. No minimum invoice-age gate applies.
8. FieldPO approval shall close the associated FieldPO work order. The exact PO approval and work-order closure must both be confirmed before the email moves to Processed. A failed or ambiguous gate/action shall move only that invoice to Needs Review, leave it out of later stages, and continue the batch. Approval is not attempted for invoices excluded from the captured selection or already sent to Needs Review.

## Notification Requirements

1. Outbound replacement notification email shall be controlled by `notifications_enabled`.
2. `notifications_enabled` shall be a JSON boolean, not a string.
3. The default value shall be `false`.
4. When disabled, the pipeline shall skip notification without affecting persistence or source-email acknowledgement.
5. When enabled, the existing replacement notification behavior shall run after persistence.

## Manifest and Duplicate Behavior

1. The in-memory manifest shall contain at most one item per MVA per run.
2. If the same MVA appears more than once in one email, the last valid scan shall replace the earlier scan.
3. Multiple simultaneous glass pieces for one MVA are not supported by the current manifest model.
4. Sheet persistence shall retain the existing incident-date behavior and shall not change because of OEM routing.

## Work-Item Compatibility

Sheet Actions `Replacement`, `Replace(AGN)`, and `Replace(AVIS)` shall normalize to the internal damage type `Replacement`. `Repair` and `Repair(SuperGlass)` shall normalize to `Repair` and remain excluded from replacement-only work-item processing.

## Logging Requirements

The pipeline shall log:

- each malformed or unknown scan that was skipped, plus a skipped-count summary directing the operator to correct and resubmit those scans;
- OEM repair normalization;
- worker and persistence failures;
- whether notifications were sent or disabled;
- source-email acknowledgement; and
- successful pipeline completion.

The operational log is `GlassOrchestrator.log` in the repository root.
