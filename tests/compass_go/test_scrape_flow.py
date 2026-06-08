"""Unit tests for ScrapeFlow's per-MVA outcome handling."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.compass_go.outcomes import MVANotFoundError
from src.compass_go import scrape_flow as scrape_flow_module
from src.compass_go.scrape_flow import (
    BACK_BUTTON_SELECTOR,
    MISSING_VIN,
    NOT_FOUND_DESC,
    ScrapeFlow,
)


@pytest.fixture(autouse=True)
def _no_capture_failure(monkeypatch):
    """Stub capture_failure so unit tests don't touch the filesystem."""
    monkeypatch.setattr(scrape_flow_module, "capture_failure", lambda *a, **kw: None)


def _make_scan_stub(submit_side_effect):
    scan = MagicMock()
    scan.submit.side_effect = submit_side_effect
    scan.page = MagicMock()
    # _scan_nav is consulted by the generic-exception recovery helper;
    # return None so the recovery is a no-op in unit tests.
    scan._scan_nav.return_value = None
    return scan


def test_scrape_not_found_writes_mva_not_found_row():
    scan = _make_scan_stub(MVANotFoundError("12345678"))
    writer = MagicMock()

    count = ScrapeFlow(scan, writer).run(["12345678"])

    assert count == 1
    writer.append.assert_called_once()
    record = writer.append.call_args.args[0]
    assert record.mva == "12345678"
    assert record.vin == MISSING_VIN
    assert record.desc == NOT_FOUND_DESC


def test_scrape_not_found_recovers_via_back_arrow():
    scan = _make_scan_stub(MVANotFoundError("12345678"))
    writer = MagicMock()

    ScrapeFlow(scan, writer).run(["12345678"])

    scan.page.locator.assert_called_with(BACK_BUTTON_SELECTOR)
    scan.page.locator.return_value.first.click.assert_called_once()
    # The back-arrow path must NOT fall back to the bottom-nav Scan tab
    # when the click succeeds.
    scan._scan_nav.assert_not_called()


def test_scrape_not_found_falls_back_to_scan_tab_if_back_arrow_fails():
    scan = _make_scan_stub(MVANotFoundError("12345678"))
    scan.page.locator.return_value.first.click.side_effect = RuntimeError("no back arrow")
    writer = MagicMock()

    ScrapeFlow(scan, writer).run(["12345678"])

    scan._scan_nav.assert_called_once()


def test_scrape_generic_exception_writes_empty_row_and_continues():
    scan = _make_scan_stub(RuntimeError("boom"))
    writer = MagicMock()

    count = ScrapeFlow(scan, writer).run(["12345678"])

    assert count == 1
    record = writer.append.call_args.args[0]
    assert record.mva == "12345678"
    assert record.vin == ""
    assert record.desc == ""


def test_scrape_loop_continues_past_not_found():
    found_details = MagicMock()
    found_details.read.side_effect = ["VIN123456789ABCDE", "MAKE_MODEL"]
    scan = _make_scan_stub([MVANotFoundError("11111111"), found_details])
    writer = MagicMock()

    count = ScrapeFlow(scan, writer).run(["11111111", "22222222"])

    assert count == 2
    assert writer.append.call_count == 2
    first, second = writer.append.call_args_list
    assert first.args[0].vin == MISSING_VIN
    assert first.args[0].desc == NOT_FOUND_DESC
    assert second.args[0].mva == "22222222"
    assert second.args[0].vin == "VIN123456789ABCDE"
    assert second.args[0].desc == "MAKE_MODEL"
