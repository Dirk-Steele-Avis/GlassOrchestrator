"""Per-MVA scrape loop: submit -> expand -> read -> write -> back."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from .diagnostics import capture_failure
from .pages.scan_page import ScanPage
from .pages.vehicle_details_page import (
    DATA_KEY_DESC,
    DATA_KEY_VIN,
    VehicleDetailsPage,
)
from .records import VehicleRecord
from .writer import ResultsWriter

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class ScrapeFlow:
    def __init__(self, scan: ScanPage, writer: ResultsWriter):
        self._scan = scan
        self._writer = writer

    def run(self, mvas: Iterable[str]) -> int:
        """Process each MVA. Returns the count of rows written.

        Each non-empty input MVA produces exactly one output row (with empty
        VIN/Desc coerced to N/A on failure). Errors on individual MVAs are
        logged and do not abort the loop — this matches the legacy worker
        contract, where Phase 3 only aborts on global failures (auth,
        missing worker, etc.).
        """
        count = 0
        for mva in mvas:
            mva = mva.strip()
            if not mva:
                continue
            record = self._scrape_one(mva)
            self._writer.append(record)
            count += 1
        return count

    def _scrape_one(self, mva: str) -> VehicleRecord:
        log.info("Scraping MVA %s", mva)
        try:
            details = self._scan.submit(mva)
            details.expand_show_more()
            # Read VIN first — it's hidden behind Show More and loads slower
            # than the always-visible Description / MVA cells.
            vin = details.read(DATA_KEY_VIN)
            desc = details.read(DATA_KEY_DESC)
            if not vin or not desc:
                log.warning(
                    "Empty read for MVA %s (vin=%r desc=%r) — capturing DOM",
                    mva, vin, desc,
                )
                try:
                    capture_failure(self._scan.page, f"empty_read_{mva}")
                except Exception:
                    log.exception("capture_failure unavailable for MVA %s", mva)
            details.back()
            return VehicleRecord(mva=mva, vin=vin, desc=desc)
        except Exception as exc:  # noqa: BLE001 — per-row resilience
            log.exception("Scrape failed for MVA %s: %s", mva, exc)
            try:
                capture_failure(self._scan.page, f"scrape_{mva}")
            except Exception:
                log.exception("capture_failure unavailable for MVA %s", mva)
            return VehicleRecord(mva=mva, vin="", desc="")
