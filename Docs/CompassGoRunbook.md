# Compass GO Runbook

Purpose: quick triage and recovery steps for Compass GO browser/session failures in Phase 3.

## Typical Symptoms

- Browser opens but sits on about:blank.
- Browser appears to close by itself during SSO.
- Script keeps running but never reaches scan input actions.
- Worker exits with non-zero status and pipeline aborts.

## Key Logs To Check

Primary log:
- log/compass_go.log

Orchestrator log:
- GlassOrchestrator.log

Important signatures:
- "Page event: closed"
- "Context event: closed"
- "Browser event: disconnected"
- "Auth: no usable page after close"
- "BrowserType.launch_persistent_context: Timeout"
- "DevTools remote debugging requires a non-default data directory"

## What The Signatures Usually Mean

1. "Page event: closed" then context/browser close
- SSO flow likely closed the active tab/window.
- Recovery should switch to another tab or recreate one.

2. launch_persistent_context timeout with DevTools non-default data directory message
- Edge rejected automation attach against the default user-data-dir.
- Fallback launch mode should be used.

3. Browser opens but no navigation logs after launch
- Session is stuck in launch/attach path, not in auth page detection.

## Current Mitigations In Code

- Auth page lifecycle guards:
  - Switch to another open page when current page closes.
  - Recreate page if context is alive and no pages remain.
  - Safe visibility probes in auth page objects.

- Session resilience:
  - Keepalive tab to prevent single-tab browser shutdown after SSO window.close.
  - Event logging for page/context/browser close transitions.
  - Non-persistent fallback when default user-data-dir cannot support persistent attach.
  - Default launch now uses a dedicated persistent profile directory
    (.edge-playwright-profile) to avoid default-profile DevTools attach failures.

- Parser retry:
  - One auth-session retry for page-closed-before-scan runtime failure.

## Fast Triage Procedure

1. Confirm latest run in log/compass_go.log.
2. Find the first failure signature above.
3. Classify it:
   - SSO tab-close chain (page -> context -> browser)
   - Persistent attach timeout/non-default-data-dir error
   - Auth timeout/state mismatch
4. Apply matching action:
   - If attach timeout: rerun and confirm fallback mode is used.
   - If tab-close chain: verify keepalive + auth page switching logs are present.
   - If auth timeout: inspect current URL/state logs and capture screenshot artifacts.
5. Re-run orchestrator once and confirm Phase 3 reaches "ScanPage ready" then MVA submission.

## Confirmed Resolution (2026-07-07)

- Observed issue: browser closed right after Microsoft SSO username/password.
- Confirmed fix: dedicated persistent profile directory
  (.edge-playwright-profile) stopped post-SSO browser teardown in live run.
- Keep this as the default unless a future Edge policy change requires a different path.

## Environment Flags You Can Use

- COMPASS_GO_ENTRY_URL
  - Override Compass GO entry URL.

- PLAYWRIGHT_EDGE_USER_DATA_DIR
  - Use non-default user-data-dir if persistent profile attach is required.

- PLAYWRIGHT_EDGE_PROFILE_DIRECTORY
  - Edge profile directory name (for example, Default or Profile 1).

- COMPASS_GO_FORCE_PERSISTENT
  - Force persistent mode even when default user-data-dir is detected.
  - Use only when you are sure Edge/Playwright can attach cleanly.

## Escalation Data To Capture

When opening a follow-up issue, include:

- Last 150 lines of log/compass_go.log.
- Last 80 lines of GlassOrchestrator.log.
- Exact first error line and timestamp.
- Whether Edge showed about:blank only or additional tabs.
- Any manual navigation done during the run.

## Known Good Validation Command

Run Compass GO tests (excluding live e2e):

c:/Users/steeldir/Code/GlassOrchestrator/.venv/Scripts/python.exe -m pytest tests/compass_go -k "not e2e"
