# GlassOrchestrator Agent Instructions

## Start Here

- Treat [Docs/GlassOrchestratorRequirements.md](Docs/GlassOrchestratorRequirements.md) as the canonical behavioral specification.
- Use [README.md](README.md) for architecture, setup, runtime entry points, and configuration precedence.
- Follow [Docs/LogfileStandards.md](Docs/LogfileStandards.md) for logging changes.

## Strict Workflow Rule

- Never introduce or preserve a fallback design unless the user explicitly requests that exact fallback.
- Implement one explicit workflow path with strict selectors, contracts, and success criteria.
- Do not add alternate selectors, backup APIs, degraded modes, silent retries, guessed defaults, skipped steps, or catch-and-continue behavior to recover from a failed primary path.
- Existing fallback code is not authorization to extend or retain it when modifying its owning flow. Surface the behavior and require explicit user direction.
- On failure, stop that workflow path and report the precise failed step and reason. Preserve a documented nonfatal business outcome only when the canonical requirements explicitly define it.
- Test both the successful path and visible failure at the exact boundary being changed.

## Validation

- Run focused tests first: `.venv\Scripts\python.exe -m pytest <test-path> -q`.
- Run the full suite when the change has broader impact: `.venv\Scripts\python.exe -m pytest tests\`.
- Some integration tests require credentials and live services; distinguish environment failures from code regressions.