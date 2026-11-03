"""Run one strict Type-date intake workflow for each unread Orca email."""

from __future__ import annotations

import logging
import sys
from datetime import date

import FieldPOFillNextAction
import create_compass_complaints
from GlassOrchestrator import run_pipeline


log = logging.getLogger("GlassOrchestrator")


def _process_inventory_date(target_inventory_date: date) -> int:
    log.info(
        "Intake downstream processing started for Inventory Date %s",
        target_inventory_date.isoformat(),
    )
    FieldPOFillNextAction.run(target_inventory_date=target_inventory_date)
    compass_result = create_compass_complaints.run(
        target_inventory_date=target_inventory_date,
    )
    if compass_result != 0:
        raise RuntimeError(
            f"Compass processing failed for Inventory Date {target_inventory_date.isoformat()}"
        )
    log.info(
        "Intake downstream processing completed for Inventory Date %s",
        target_inventory_date.isoformat(),
    )
    return 0


def run() -> int:
    return run_pipeline(after_batch=_process_inventory_date)


if __name__ == "__main__":
    sys.exit(run())