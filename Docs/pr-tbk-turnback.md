## Summary
- Add TBK (turnback) damage type support for leased vehicles where AVIS must order glass.
- Introduce centralized damage type catalog in data/damage_types.csv and loader utilities in core/damage_types.py.
- Wire parsing, normalization, eligibility, and work-item mapping so TBK is replacement-eligible and routes to Replace(AVIS).
- Extend close-workitem and complaint expectation normalization to recognize TBK and Turnback.
- Add targeted tests for TBK parsing, eligibility, handler mapping, and persistence routing.

## Key Changes
- New catalog: data/damage_types.csv
- New resolver module: core/damage_types.py
- Parser and sheet action routing updates: GlassOrchestrator.py
- Eligibility normalization updates: core/eligibility.py
- Work item handler mapping updates: flows/work_item_handler.py
- Sheet type normalization updates: close_workitem.py and WorkItems/close_workitem.py
- Complaint baseline normalization update: create_compass_complaints.py
- Test updates: tests/test_unit.py, tests/test_eligibility.py, tests/test_work_item_handler.py, tests/test_close_workitem.py, tests/test_integration.py

## Validation
- Ran targeted suite:
  - .\.venv\Scripts\python.exe -m pytest tests\test_unit.py tests\test_eligibility.py tests\test_work_item_handler.py tests\test_close_workitem.py tests\test_integration.py -q
- Result: 262 passed, 41 skipped, 0 failed

## Commits Included
- a7f2701: Add UI fallback when Compass complaint API lookup fails
- 45ad584: Add TBK turnback damage type with CSV-backed catalog
- d5d43ea: Update orchestrator config defaults for TBK turnback

## Risk and Rollback
- Risk is low to moderate and centered on scan parsing and action normalization behavior changes.
- Rollback path is straightforward by reverting the TBK catalog commits if unexpected production behavior appears.
