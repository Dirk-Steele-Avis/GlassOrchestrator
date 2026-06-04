"""Diagnostics: file logging + failure screenshots for the Compass GO worker."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / "log"
FAILURE_DIR = LOG_DIR / "failures"

_FILE_HANDLER_INSTALLED = False


def setup_file_logging(filename: str = "compass_go.log") -> Path:
    """Attach a FileHandler to the root logger. Safe to call repeatedly."""
    global _FILE_HANDLER_INSTALLED
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / filename
    if _FILE_HANDLER_INSTALLED:
        return path
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    _FILE_HANDLER_INSTALLED = True
    log.info("File logging enabled: %s", path)
    return path


def capture_failure(page: "Page", label: str) -> Path | None:
    """Save a screenshot + HTML snapshot under log/failures/. Returns png path."""
    try:
        FAILURE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
        stem = FAILURE_DIR / f"{ts}_{safe}"
        png = stem.with_suffix(".png")
        html = stem.with_suffix(".html")
        page.screenshot(path=str(png), full_page=True)
        html.write_text(page.content(), encoding="utf-8")
        log.error("Failure captured: %s (+ %s)", png, html.name)
        return png
    except Exception:
        log.exception("capture_failure: failed to capture for label=%s", label)
        return None
