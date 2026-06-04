"""End-to-end test for the Compass GO scraper.

Runs against the live Compass GO app. Skipped by default — opt in via:
    set COMPASS_GO_RUN_E2E_TESTS=1
    pytest -m e2e tests/compass_go/test_e2e_compass_go.py

Prerequisites:
  - Edge installed and signed in to your corporate account on the Default profile
  - All Edge windows closed (the session will kill running msedge.exe)
  - Optional env: COMPASS_GO_E2E_MVA (defaults to 058883134)
                  COMPASS_GO_ENTRY_URL (overrides default Foundry PWA URL)
"""
import os
from pathlib import Path

import pytest

_RUN_E2E = os.getenv("COMPASS_GO_RUN_E2E_TESTS", "").strip().lower() in {"1", "true", "yes"}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _RUN_E2E,
        reason="Live E2E disabled \u2014 set COMPASS_GO_RUN_E2E_TESTS=1 to enable "
               "(this test kills running Edge processes).",
    ),
]

pytest.importorskip("playwright.sync_api")

from src.compass_go.auth_flow import AuthFlow
from src.compass_go.scrape_flow import ScrapeFlow
from src.compass_go.session import CompassGoSession
from src.compass_go.writer import ResultsWriter


def test_single_mva_produces_results_row(tmp_path: Path):
    mva = os.getenv("COMPASS_GO_E2E_MVA", "058883134")
    out = tmp_path / "GlassResults.txt"
    writer = ResultsWriter(out)
    writer.reset()

    with CompassGoSession().page() as page:
        scan = AuthFlow(page).ensure_signed_in()
        count = ScrapeFlow(scan, writer).run([mva])

    assert count == 1
    contents = out.read_text(encoding="utf-8").strip()
    assert contents, "GlassResults.txt should not be empty"
    parts = contents.split(",")
    assert len(parts) == 3, f"Expected MVA,VIN,Desc; got: {contents!r}"
    assert parts[0] == mva
    assert parts[1] not in ("", "N/A"), f"VIN missing for {mva}: {contents!r}"
    assert parts[2] not in ("", "N/A"), f"Desc missing for {mva}: {contents!r}"
