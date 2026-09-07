# Completed Invoices Decision: Remove Dry-Run From Final Runtime

Date: 2026-09-06
Status: Accepted
Scope: `invoice_workflow` completed-invoices runtime

## Decision

Dry-run is removed from the final completed-invoices runtime path.

The shipped workflow has one execution path: live processing through strict identity validation and stage gating.

## Why

1. Dry-run was useful during development to validate parsing, identity rules, and logging safely.
2. In final production code, dry-run introduced a second behavior path that increased maintenance and review burden.
3. Parallel mode behavior risked drift between test expectations and real mutation behavior.
4. Operator intent is clearer with one runtime path and explicit live safeguards.

## Safety Model In Final Code

1. No dry-run branch in completed-invoices CLI/coordinator.
2. Real processing remains strictly gated by identity validation and terminal-ID checks.
3. Only eligible invoices move to Outlook `Processed` after successful validation and stage success.
4. Terminal message IDs continue to prevent reprocessing by exact `InternetMessageID`.

## Non-Goal Clarification

This decision applies to completed-invoices runtime behavior only. It does not remove development test strategies, mocks, or isolated validation tests in the test suite.

## Consequences

1. Lower complexity in production orchestration and incident analysis.
2. Fewer opportunities for mode-specific regressions.
3. Stronger guarantee that tested runtime flow matches shipped runtime flow.

## Rollout Note

Operational guidance and runbooks should avoid instructing `Run-CompletedInvoices.cmd --dry-run` and use capped live canary execution instead.
