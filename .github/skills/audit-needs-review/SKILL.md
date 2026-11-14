---
name: audit-needs-review
description: 'Read-only batch audit of invoices currently in Outlook Needs Review; report fully verified candidates without moving them.'
argument-hint: ''
---

# Audit invoices in Needs Review

Use this skill when the user asks to review the current Needs Review folder and
identify invoices that may be eligible for Processed. The audit is read-only;
the user decides whether to move any messages.

## Procedure

1. From the repository root, run the project audit:

   ```powershell
   .venv\Scripts\python.exe -m invoice_workflow.review_audit
   ```

2. The audit reads the existing Outlook Needs Review folder, parses each
   invoice, and compares invoice identity with the GlassClaims Sheet and live
   FieldPO data. Allow the user to finish sign-in in the configured browser
   profile if prompted. Do not select another profile or retry a failed step.
3. Report the audit output faithfully. Only invoices labeled `CANDIDATE` have
   current identity checks, required PO/creator checks, and recorded workflow
   approvals/closures all confirmed. Keep identity-pass invoices with any
   remaining gate or missing ledger evidence separate; do not describe them as
   ready for Processed.
4. Report every capture failure as not cleared. On a command failure, report
   the failed step and reason; do not present partial results as a complete
   audit.

## Boundaries

- Never move, modify, or send an Outlook message; update Sheets, FieldPO, or
  workflow records; or invoke invoice closeout.
- The audit reads Outlook, Sheets, FieldPO, and the local ledger. Temporary PDF
  captures are removed when the command exits.
- Do not infer missing data or treat an unverified gate as passing.
