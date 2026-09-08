# GlassOrchestrator Requirements

## Purpose

GlassOrchestrator processes vehicle-glass scan exports from Gmail, retrieves vehicle data, writes claim rows to the `GlassClaims` Google Sheet, and optionally sends replacement notifications.

## Pipeline Requirements

1. The pipeline shall retrieve one unread Orca Scan email from the configured sender.
2. The pipeline shall parse all valid scan descriptions from that email.
3. The pipeline shall invoke the vehicle-data worker for every valid MVA.
4. A worker failure or stale worker output shall abort the run before persistence.
5. The pipeline shall merge worker results into the parsed manifest. Missing results shall use `VIN=N/A`.
6. The pipeline shall insert all output rows above the Google Sheet summary section.
7. After successful persistence, the pipeline shall mark the source email as read.
8. Failures before successful persistence shall leave the source email unread.

## Scan Format

The permanent scan format is:

```text
<MVA><AREA>[r|war][c][ OEM]
```

- `MVA` is exactly eight digits.
- `AREA` is required and must be a configured area code.
- Directional area codes use `<side><orientation><area>` order; for example, `LFD` means Left Front Door.
- Configured `legacy_area_aliases` temporarily normalize older orientation-first codes to canonical directional codes.
- `r` means Repair when valid for the area.
- `war` means Warranty.
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

- malformed or unknown scans;
- OEM repair normalization;
- worker and persistence failures;
- whether notifications were sent or disabled;
- source-email acknowledgement; and
- successful pipeline completion.

The operational log is `GlassOrchestrator.log` in the repository root.
