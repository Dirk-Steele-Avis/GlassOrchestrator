"""Compass GO scraper worker — subprocess invoked by GlassOrchestrator.

Contract (preserved from legacy src/GlassDataParser.py):
  - Read 8-digit MVAs from data/GlassDataParser.csv (one per row, optional header)
  - Write CSV rows `MVA,VIN,Desc` to GlassResults.txt (append, no header)
  - Missing fields written as 'N/A'
"""
from __future__ import annotations

import csv
import logging
import re
import sys
from pathlib import Path

# Ensure project root importable when invoked as a subprocess
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.compass_go.auth_flow import AuthFlow  # noqa: E402
from src.compass_go.diagnostics import setup_file_logging  # noqa: E402
from src.compass_go.scrape_flow import ScrapeFlow  # noqa: E402
from src.compass_go.session import CompassGoSession  # noqa: E402
from src.compass_go.writer import ResultsWriter  # noqa: E402

MVA_CSV = PROJECT_ROOT / "data" / "GlassDataParser.csv"
RESULTS_FILE = PROJECT_ROOT / "GlassResults.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
setup_file_logging()
log = logging.getLogger("CompassGoParser")


def _normalize_mva(raw: str) -> str:
    s = raw.strip()
    m = re.match(r"^(\d{8})", s)
    if m:
        return m.group(1)
    return s[:8]


def read_mva_list(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        log.error("MVA CSV not found: %s", csv_path)
        return []

    mvas: list[str] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [row[0] for row in reader if row]
    if rows and (rows[0].startswith("#") or rows[0].lower().startswith("mva")):
        rows = rows[1:]
    for raw in rows:
        if not raw or raw.startswith("#"):
            continue
        mvas.append(_normalize_mva(raw))
    return mvas


def main() -> int:
    mvas = read_mva_list(MVA_CSV)
    if not mvas:
        log.warning("No MVAs to process; exiting cleanly")
        return 0

    writer = ResultsWriter(RESULTS_FILE)
    writer.reset()
    log.info("Processing %d MVAs", len(mvas))

    with CompassGoSession().page() as page:
        scan = AuthFlow(page).ensure_signed_in()
        count = ScrapeFlow(scan, writer).run(mvas)

    log.info("Done: wrote %d rows to %s", count, RESULTS_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
