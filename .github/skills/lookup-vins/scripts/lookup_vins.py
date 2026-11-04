"""Read-only Compass GO lookup with one end-of-list retry for not-found MVAs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
import time
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import Error, Page, expect, sync_playwright

from src.compass_go.pages.login_confirm_page import LoginConfirmPage
from src.compass_go.pages.scan_page import (
    BACK_BUTTON_SELECTOR,
    BEGIN_SCANNING_NAME,
    NOT_FOUND_TEXT,
    SCAN_TAB_SELECTOR,
)
from src.compass_go.pages.vehicle_details_page import DATA_KEY_MVA, DATA_KEY_VIN
from src.compass_go.session import (
    DEFAULT_AUTOMATION_USER_DATA_DIR,
    DEFAULT_EDGE_USER_DATA_DIR,
    DEFAULT_ENTRY_URL,
)

WAIT_MS = 60_000
AUTH_SECONDS = 120
INPUT_ARIA_LABEL = "Or enter MVA/VIN/Plate"
VEHICLE_DETAILS_HEADING = "Vehicle Details Open Help"


class LookupFailure(RuntimeError):
    """A failed boundary that must stop the batch."""


@contextmanager
def step(label: str) -> Iterator[None]:
    try:
        yield
    except (Error, AssertionError, LookupFailure) as exc:
        raise LookupFailure(f"{label}: {exc}") from exc


def validate_mvas(mvas: list[str]) -> list[str]:
    if not mvas:
        raise ValueError("Provide at least one eight-digit MVA.")
    for mva in mvas:
        if re.fullmatch(r"[0-9]{8}", mva) is None:
            raise ValueError(f"Invalid MVA {mva!r}; expected exactly eight ASCII digits.")
    return mvas


def prepare_scan(page: Page) -> None:
    deadline = time.monotonic() + AUTH_SECONDS
    confirmed = False
    nav = page.locator(SCAN_TAB_SELECTOR)
    confirm = LoginConfirmPage(page)
    while not nav.is_visible():
        if page.get_by_role("heading", name="Confirm User", exact=True).is_visible():
            if confirmed:
                raise LookupFailure("Confirm User reappeared after submission.")
            confirm.continue_as_current_user()
            confirmed = True
        if time.monotonic() >= deadline:
            raise LookupFailure(
                f"Scan tab was not ready within {AUTH_SECONDS}s at {page.url}; "
                "complete sign-in/setup in the configured profile before another run."
            )
        page.wait_for_timeout(250)
    nav.click()
    begin = page.get_by_role("button", name=BEGIN_SCANNING_NAME, exact=True)
    entry = page.get_by_label(INPUT_ARIA_LABEL, exact=True)
    deadline = time.monotonic() + WAIT_MS / 1000
    while not entry.is_visible():
        if begin.is_visible():
            begin.click()
            break
        if time.monotonic() >= deadline:
            raise LookupFailure("Scan did not show Begin Scanning or the MVA input.")
        page.wait_for_timeout(250)
    expect(entry).to_be_visible(timeout=WAIT_MS)


def lookup_mvas(page: Page, mvas: list[str]) -> list[tuple[str, str]]:
    validate_mvas(mvas)
    rows = _lookup_pass(page, mvas, retry=False)
    missing_indices = [index for index, (_, vin) in enumerate(rows) if vin == "N/A"]
    if missing_indices:
        retried = _lookup_pass(page, [mvas[index] for index in missing_indices], retry=True)
        for index, row in zip(missing_indices, retried, strict=True):
            rows[index] = row
    return rows


def _lookup_pass(
    page: Page, mvas: list[str], *, retry: bool
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for mva in mvas:
        label = f"MVA {mva}" + (" / end-of-list retry" if retry else "")
        with step(f"{label} / submit"):
            entry = page.get_by_label(INPUT_ARIA_LABEL, exact=True)
            expect(entry).to_be_visible(timeout=WAIT_MS)
            expect(page.get_by_role(
                "heading", name=VEHICLE_DETAILS_HEADING, exact=True
            )).to_be_hidden(timeout=WAIT_MS)
            expect(page.get_by_text(NOT_FOUND_TEXT, exact=True)).to_be_hidden(
                timeout=WAIT_MS
            )
            entry.fill(mva)
            page.get_by_role("button", name="Enter", exact=True).click()
        with step(f"{label} / read result"):
            heading = page.get_by_role(
                "heading", name=VEHICLE_DETAILS_HEADING, exact=True
            )
            expect(heading).to_be_visible(timeout=WAIT_MS)
            not_found = page.get_by_text(NOT_FOUND_TEXT, exact=True)
            mva_cell = page.locator(f'td[data-key="{DATA_KEY_MVA}.1"][role="gridcell"]')
            expect(not_found.or_(mva_cell)).to_have_text(
                re.compile(r"\S"), timeout=WAIT_MS
            )
            if not_found.is_visible():
                vin = "N/A"
                outcome = (
                    "still Vehicle Not Found after one retry; returning N/A."
                    if retry else
                    "Vehicle Not Found; queued for one end-of-list retry."
                )
                print(f"MVA {mva}: {outcome}", file=sys.stderr)
            else:
                expect(mva_cell).to_have_text(f"0{mva}", timeout=WAIT_MS)
                page.get_by_role("button", name="Show More", exact=True).click()
                expect(page.get_by_role(
                    "button", name="Show Less", exact=True
                )).to_be_visible(timeout=WAIT_MS)
                vin_cell = page.locator(
                    f'td[data-key="{DATA_KEY_VIN}.1"][role="gridcell"]'
                )
                expect(vin_cell).to_have_text(
                    re.compile(r"[A-HJ-NPR-Z0-9]{17}"), timeout=WAIT_MS
                )
                vin = vin_cell.inner_text().strip()
                if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) is None:
                    raise LookupFailure(f"Malformed VIN {vin!r}.")
                if retry:
                    print(f"MVA {mva}: VIN found on end-of-list retry.", file=sys.stderr)
        with step(f"{label} / return to Scan"):
            page.locator(BACK_BUTTON_SELECTOR).click()
            expect(heading).to_be_hidden(timeout=WAIT_MS)
            expect(entry).to_be_visible(timeout=WAIT_MS)
        rows.append((mva, vin))
    return rows


def run_lookup(mvas: list[str]) -> list[tuple[str, str]]:
    validate_mvas(mvas)
    profile_value = os.environ.get(
        "PLAYWRIGHT_EDGE_USER_DATA_DIR", str(DEFAULT_AUTOMATION_USER_DATA_DIR)
    )
    if not profile_value.strip():
        raise LookupFailure("PLAYWRIGHT_EDGE_USER_DATA_DIR must not be empty.")
    profile = Path(profile_value)
    if profile.resolve() == DEFAULT_EDGE_USER_DATA_DIR.resolve():
        raise LookupFailure("Use a dedicated automation profile, not Edge's default data directory.")
    profile_name = os.environ.get("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY", "Default")
    if not profile_name.strip():
        raise LookupFailure("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY must not be empty.")
    with sync_playwright() as pw:
        with step(f"launch Edge profile {profile} ({profile_name})"):
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="msedge",
                headless=False,
                args=[f"--profile-directory={profile_name}"],
            )
        try:
            with step("open Compass GO and prepare Scan"):
                page = context.new_page()
                page.goto(os.environ.get("COMPASS_GO_ENTRY_URL", DEFAULT_ENTRY_URL),
                          wait_until="domcontentloaded")
                prepare_scan(page)
            return lookup_mvas(page, mvas)
        finally:
            context.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mvas", nargs="+", help="Eight-digit MVAs in output order.")
    args = parser.parse_args(argv)
    try:
        rows = run_lookup(args.mvas)
    except (LookupFailure, Error, AssertionError, OSError, ValueError) as exc:
        print(f"VIN lookup failed: {exc}", file=sys.stderr)
        return 1
    print("MVA VIN")
    for mva, vin in rows:
        print(f"{mva} {vin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
