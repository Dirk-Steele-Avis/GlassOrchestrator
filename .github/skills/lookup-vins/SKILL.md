---
name: lookup-vins
description: 'Look up VINs from pasted MVA numbers or a screenshot of an MVA list using the configured Compass GO browser session. Use when asked for VINs by MVA, MVA-to-VIN lookup, or a two-column MVA VIN list.'
argument-hint: '60292282 61004403'
---

# Look up VINs by MVA

Use this project skill for read-only vehicle lookups, not invoice processing or
the glass intake pipeline.

## Procedure

1. Extract the MVA list from the user's message or pasted screenshot. Read a
   screenshot top to bottom; do not edit the attachment. Each MVA must be exactly eight
   ASCII digits. Preserve leading zeros, order, and duplicates. Do not guess or
   correct an ambiguous number; ask the user to clarify it.
2. From the repository root, run the bundled
   [lookup script](./scripts/lookup_vins.py) with the project's Python:

   ```powershell
   .venv\Scripts\python.exe .github\skills\lookup-vins\scripts\lookup_vins.py 60292282 61004403
   ```

   Replace the example arguments with the user's MVAs, each as a separate
   argument. Only pass validated digit strings, never untrusted shell text.
3. The script opens the configured dedicated Compass GO Edge profile. Allow the
   user to finish sign-in in that window if necessary. A normal Confirm User
   dialog is submitted once using the existing project helper. If location setup,
   a quick-fix screen, or another unexpected state prevents Scan from becoming
   ready, stop and report it. Do not reset browser data, switch profiles, retry startup,
   terminate existing browsers, or substitute another lookup source.
4. On exit code zero, return stdout as plain text, preserving its two columns:

   ```text
   MVA VIN
   60292282 1HGCY1F43SA091270
   61004403 5FPYK3F59TB023226
   ```

   After the first pass, the script retries only explicit Vehicle Not Found
   results once, at the end of the list in the same browser session. Output
   retains the original order and duplicates. `N/A` is allowed only when Compass
   GO explicitly reports Vehicle Not Found again on that retry. Report any final
   not-found outcomes briefly; a successful retry should return the retrieved VIN.
5. On a nonzero exit, report the failed step and reason from stderr. Do not
   present a partial batch as successful or invent a VIN.

## Boundaries

- Do not edit pipeline inputs, `GlassResults.txt`, invoice targets, Sheets, or
  email. Browser session/cache updates are the only persistent side effects.
- Use one launch and one initial submission per input occurrence. Only explicit
  Vehicle Not Found permits one additional submission at the end of the list.
  Never retry a timeout, malformed/mismatched result, navigation failure, or
  any other error. A retry error stops the batch without partial stdout.
- Verify the returned vehicle's MVA before accepting its VIN: Compass GO displays
  the eight-digit input with one leading zero (nine digits). Require that exact
  value, while retaining the original eight digits in output. An empty, malformed,
  or mismatched result is a fatal error, not Vehicle Not Found.
- Profile configuration follows `PLAYWRIGHT_EDGE_USER_DATA_DIR` and
  `PLAYWRIGHT_EDGE_PROFILE_DIRECTORY`; otherwise use the project's dedicated
  `.edge-playwright-profile` and `Default` profile. Never use Edge's personal
  default user data directory.
