---
name: invoice-review-reason
description: 'Find the recorded reason an invoice was sent to Needs Review using its MVA, VIN, or PO.'
argument-hint: 'MVA, VIN, or PO'
---

# Find an invoice review reason

Use this skill to explain why an invoice was sent to Needs Review. This is a
read-only ledger lookup, not a request to rerun invoice validation.

## Procedure

1. Extract one explicitly provided MVA, VIN, or PO. Remove a clear label such
   as `MVA#`, `VIN#`, or `PO#`, but do not guess, correct, or truncate the
   value. Preserve leading zeros. An invoice number alone is not a valid lookup
   key; ask for an MVA, VIN, or PO instead.
2. From the repository root, run the lookup using the project's Python:

   ```powershell
   .venv\Scripts\python.exe -m invoice_workflow.review_lookup --mva IDENTIFIER
   ```

   Use `--mva`, `--vin`, or `--po` to match the supplied identifier type.
   Replace `IDENTIFIER` with the validated value only. Do not pass other
   user-provided text as command arguments.
3. On exit code zero, report the matched records and their recorded review
   stage, code, and exact reason. If several records match, report each
   separately and identify which invoice number and other available details
   belong to it.
4. On a no-match result, tell the user that only the supplied identifier was
   searched and ask for another known MVA, VIN, or PO if they want to try again.
   Do not automatically guess alternate identifiers. Do not infer a reason from
   current invoice data or rerun validation.

## Boundaries

- Never move, modify, or send an email; update Sheets; contact FieldPO; or run
  invoice closeout as part of this lookup.
- Report only ledger evidence. Older records may lack a persisted Needs Review
  event and may show only their earliest recorded blocked/failed attempt.
- Do not expose internal message IDs or invent missing details.
