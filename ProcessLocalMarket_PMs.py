"""Create a Compass bulk vend request from unread All Purpose Auto PDFs.

Default behavior is a development run: populate the form, leave it open for
review, do not submit, and leave every source email unread. Pass ``--submit``
to submit and mark source messages read after Compass closes the request form.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pdfplumber
import win32com.client
from playwright.sync_api import Locator, Page, sync_playwright

from playwright_prototype.config import (
    resolve_edge_profile_directory,
    resolve_edge_user_data_dir,
)


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "AllPurposeAutoVend.log"
MAILBOX_NAME = "Dirk.Steele@avisbudget.com"
MAIL_FOLDER_PATH = ("Inbox", "AllPurposeAuto")
SENDER_ADDRESS = "allpauto@gmail.com"
COMPASS_VEND_URL = (
    "https://avisbudget.palantirfoundry.com/workspace/module/view/latest/"
    "ri.workshop.main.module.ff050e23-6965-418f-85b9-32ac418c17e8"
)

VEND_REASON = "Not enough resources"
CHECKOUT_LOCATION = "GA4-A ATLANTA ABG MD"
VENDOR_NAME = "ALL PURPOSE AUTOMOTIVE INC"
ESTIMATED_COST = "61"
DAMAGE_CATEGORY = "PM"
SUCCESS_TOAST_TEXT = "Edits successfully applied."
ERROR_REVIEW_DELAY_MS = 30_000
SUBMIT_CAPTURE_DELAYS_MS = (0, 100, 250, 500)

log = logging.getLogger("AllPurposeAutoVend")


@dataclass
class CollectedVend:
    mvas: list[str]
    source_messages: list[Any]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    )


def _normalized_header(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").casefold())


def _normalized_mva(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw if re.fullmatch(r"\d{8,9}", raw) else None


def extract_mvas_from_pdf(pdf_path: Path) -> list[str]:
    """Extract values from the word-coordinate column headed Model | MVA | Mileage."""
    extracted: list[str] = []
    found_mva_column = False

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            mva_headers = [word for word in words if _normalized_header(word["text"]) == "mva"]
            for mva_header in mva_headers:
                same_line = [
                    word
                    for word in words
                    if abs(float(word["top"]) - float(mva_header["top"])) <= 1
                ]
                model_headers = [
                    word for word in same_line if _normalized_header(word["text"]) == "model"
                ]
                mileage_headers = [
                    word for word in same_line if _normalized_header(word["text"]) == "mileage"
                ]
                if len(model_headers) != 1 or len(mileage_headers) != 1:
                    continue

                model_header = model_headers[0]
                mileage_header = mileage_headers[0]
                if not (model_header["x1"] < mva_header["x0"] < mileage_header["x0"]):
                    continue

                found_mva_column = True
                left_bound = (float(model_header["x1"]) + float(mva_header["x0"])) / 2
                right_bound = (float(mva_header["x1"]) + float(mileage_header["x0"])) / 2
                for word in words:
                    if float(word["top"]) <= float(mva_header["bottom"]):
                        continue
                    center = (float(word["x0"]) + float(word["x1"])) / 2
                    if not left_bound < center < right_bound:
                        continue
                    mva = _normalized_mva(word["text"])
                    if mva:
                        extracted.append(mva)

    if not found_mva_column:
        raise RuntimeError(f"No Model | MVA | Mileage column header found in {pdf_path.name}")
    if not extracted:
        raise RuntimeError(f"MVA column contained no valid 8- or 9-digit values in {pdf_path.name}")
    return extracted


def deduplicate_mvas(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _outlook_folder():
    namespace = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    try:
        folder = namespace.Folders.Item(MAILBOX_NAME)
        for folder_name in MAIL_FOLDER_PATH:
            folder = folder.Folders.Item(folder_name)
        return folder
    except Exception as exc:
        path = " > ".join((MAILBOX_NAME, *MAIL_FOLDER_PATH))
        raise RuntimeError(f"Could not open Outlook folder {path}") from exc


def _message_sort_key(message: Any) -> str:
    return str(getattr(message, "ReceivedTime", ""))


def _matching_pdf_attachments(message: Any) -> list[Any]:
    return [
        attachment
        for attachment in message.Attachments
        if str(getattr(attachment, "FileName", "")).casefold().endswith(".pdf")
    ]


def collect_unread_mvas(folder: Any | None = None) -> CollectedVend:
    folder = folder or _outlook_folder()
    messages = sorted(list(folder.Items), key=_message_sort_key)
    all_mvas: list[str] = []
    source_messages: list[Any] = []

    with tempfile.TemporaryDirectory(prefix="all-purpose-auto-vend-") as temp_dir:
        temp_path = Path(temp_dir)
        for message_index, message in enumerate(messages):
            if getattr(message, "Class", None) != 43:
                continue
            if not bool(getattr(message, "UnRead", False)):
                continue
            sender = str(getattr(message, "SenderEmailAddress", "") or "").casefold()
            if sender != SENDER_ADDRESS.casefold():
                continue

            attachments = _matching_pdf_attachments(message)
            if not attachments:
                continue

            message_mvas: list[str] = []
            for attachment_index, attachment in enumerate(attachments):
                pdf_path = temp_path / f"message-{message_index}-attachment-{attachment_index}.pdf"
                attachment.SaveAsFile(str(pdf_path))
                parsed = extract_mvas_from_pdf(pdf_path)
                message_mvas.extend(parsed)
                log.info("Parsed %d MVA(s) from %s", len(parsed), attachment.FileName)

            if message_mvas:
                all_mvas.extend(message_mvas)
                source_messages.append(message)

    return CollectedVend(deduplicate_mvas(all_mvas), source_messages)


def mark_messages_read(messages: Iterable[Any]) -> None:
    for message in messages:
        message.UnRead = False
        message.Save()


def _exactly_one(locator: Locator, description: str) -> Locator:
    try:
        locator.first.wait_for(state="visible", timeout=20_000)
    except Exception as exc:
        raise RuntimeError(f"Timed out waiting for {description}") from exc
    count = locator.count()
    if count != 1:
        raise RuntimeError(f"Expected one {description}, found {count}")
    return locator


def _form_scope(page: Page) -> Locator:
    heading = _exactly_one(
        page.get_by_text("Bulk Create Vendor Request", exact=True),
        "Bulk Create Vendor Request heading",
    )
    return heading.locator("xpath=ancestor::div[contains(@class,'workshop-section')][1]")


def _following_input(scope: Locator, label: str, selector: str, description: str) -> Locator:
    marker = _exactly_one(scope.locator(f'[title="{label}"]'), f"{label} label")
    return _exactly_one(marker.locator(f"xpath=following::{selector}[1]"), description)


def _select_combobox(
    scope: Locator,
    label: str,
    option: str,
    search_placeholder: str = "Search…",
    select_first_result: bool = False,
    search_query: str | None = None,
    selected_text: str | None = None,
) -> None:
    marker = _exactly_one(scope.locator(f'[title="{label}"]'), f"{label} label")
    combo = _exactly_one(
        marker.locator("xpath=following::div[@role='combobox'][1]"),
        f"{label} combobox",
    )
    combo.click()
    search = _exactly_one(
        scope.page.locator("input:focus"),
        f"{label} search input",
    )
    if search.get_attribute("placeholder") != search_placeholder:
        raise RuntimeError(
            f"{label} focused input did not have placeholder {search_placeholder!r}"
        )
    search.fill(search_query or option)
    if select_first_result:
        scope.page.wait_for_timeout(1_000)
        search.press("Enter")
    else:
        choice = _exactly_one(
            scope.page.get_by_text(option, exact=True),
            f"{option} option",
        )
        choice.click()
    try:
        combo.filter(
            has_text=re.compile(re.escape(selected_text or option), re.I)
        ).wait_for(state="visible", timeout=10_000)
    except Exception as exc:
        raise RuntimeError(f"{label} did not select the first result for {option}") from exc
    log.info("Selected %s: %s", label, option)


def _check_labeled_option(scope: Locator, text: str, input_type: str) -> None:
    label = _exactly_one(
        scope.locator("label").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)),
        f"{text} label",
    )
    control = _exactly_one(label.locator(f"input[type='{input_type}']"), f"{text} {input_type}")
    label.click()
    if not control.is_checked():
        raise RuntimeError(f"Could not select {text}")


def populate_bulk_vend_form(page: Page, mvas: list[str]) -> Locator:
    page.goto(COMPASS_VEND_URL, wait_until="domcontentloaded")
    create_vend_tab = _exactly_one(
        page.get_by_role("tab", name="Create Vend", exact=True),
        "Create Vend tab",
    )
    create_vend_tab.click()
    bulk_vend_tab = _exactly_one(
        page.get_by_role("tab", name="Create Bulk Vend Work Item", exact=True),
        "Create Bulk Vend Work Item tab",
    )
    bulk_vend_tab.click()
    scope = _form_scope(page)

    textarea = _exactly_one(
        scope.get_by_label("List of MVAs, one MVA per line", exact=True),
        "MVA textarea",
    )
    expected_mvas = "\n".join(mvas)
    textarea.fill(expected_mvas)
    if textarea.input_value().strip() != expected_mvas:
        raise RuntimeError("MVA textarea did not retain the complete MVA list")
    log.info("Populated MVA textarea with %d unique MVA(s).", len(mvas))

    _check_labeled_option(scope, "External Vendor", "radio")
    log.info("Selected Where to Vend: External Vendor")
    _select_combobox(scope, "Vend Reason*", VEND_REASON, search_placeholder="Search options…")
    _select_combobox(
        scope,
        "Vend Checkout Location*",
        CHECKOUT_LOCATION,
        select_first_result=True,
        search_query="GA4",
        selected_text="GA4",
    )
    _select_combobox(
        scope,
        "Vendor *",
        VENDOR_NAME,
        select_first_result=True,
        search_query="All Purpose",
        selected_text="ALL PURPOSE",
    )

    cost = _following_input(scope, "Estimated Cost*", "input", "Estimated Cost input")
    cost.fill(ESTIMATED_COST)
    if cost.input_value() != ESTIMATED_COST:
        raise RuntimeError("Estimated Cost did not retain the required value")
    log.info("Set Estimated Cost: %s", ESTIMATED_COST)

    _exactly_one(scope.get_by_text(DAMAGE_CATEGORY, exact=True), "PM category").click()
    log.info("Selected Damage Category: %s", DAMAGE_CATEGORY)
    completed_marker = _exactly_one(
        scope.locator('[title="Already Completed?"]'),
        "Already Completed label",
    )
    completed_yes = _exactly_one(
        completed_marker.locator("xpath=following::label[1]"),
        "Already Completed Yes label",
    )
    already_completed = _exactly_one(
        completed_yes.locator("input[type='checkbox']"),
        "Already Completed checkbox",
    )
    completed_yes.click()
    if not already_completed.is_checked():
        raise RuntimeError("Already Completed was not selected")
    log.info("Selected Already Completed: Yes")

    return scope


def run_compass(mvas: list[str], submit: bool) -> None:
    user_data_dir = resolve_edge_user_data_dir()
    if not user_data_dir.is_absolute():
        user_data_dir = BASE_DIR / user_data_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)
    profile_directory = resolve_edge_profile_directory()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            channel="msedge",
            headless=False,
            no_viewport=True,
            args=[f"--profile-directory={profile_directory}"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            scope = populate_bulk_vend_form(page, mvas)
            if not submit:
                log.info("Development run complete. Form populated; Submit was not clicked.")
                input("Review the populated Compass form, then press Enter to close the browser: ")
                return

            submit_button = _exactly_one(
                scope.get_by_role("button", name="Submit", exact=True),
                "Submit button",
            )
            submit_button.click()
            capture_dir = BASE_DIR / "log" / "failures"
            capture_dir.mkdir(parents=True, exist_ok=True)
            capture_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            previous_delay = 0
            for delay_ms in SUBMIT_CAPTURE_DELAYS_MS:
                page.wait_for_timeout(delay_ms - previous_delay)
                screenshot_path = capture_dir / (
                    f"local-market-submit-{capture_stamp}-{delay_ms:03d}ms.png"
                )
                page.screenshot(path=str(screenshot_path), full_page=False)
                previous_delay = delay_ms
            log.info("Captured post-submit UI screenshots in %s", capture_dir)
            toast = page.locator(".bp6-toast:visible")
            try:
                toast.first.wait_for(state="visible", timeout=10_000)
            except Exception as exc:
                raise RuntimeError("Submit did not produce a visible toast within 10 seconds") from exc
            toast_count = toast.count()
            if toast_count != 1:
                raise RuntimeError(f"Expected one visible toast, found {toast_count}")
            toast_text = toast.first.inner_text().strip()
            if SUCCESS_TOAST_TEXT not in toast_text:
                raise RuntimeError(f"Compass rejected submission: {toast_text}")
            log.info("Compass confirmed submission: %s", SUCCESS_TOAST_TEXT)
        except Exception:
            log.error(
                "Compass workflow failed. Keeping the browser open for 30 seconds for review."
            )
            page.wait_for_timeout(ERROR_REVIEW_DELAY_MS)
            raise
        finally:
            context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Populate Compass Bulk Vend from unread All Purpose Auto PDF emails."
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the request and mark source emails read. Default is populate-only development mode.",
    )
    return parser.parse_args()


def run() -> int:
    setup_logging()
    args = parse_args()
    collected = collect_unread_mvas()
    if not collected.mvas:
        log.info("No qualifying unread All Purpose Auto PDF emails were found.")
        return 0

    log.info(
        "Collected %d unique MVA(s) from %d unread email(s).",
        len(collected.mvas),
        len(collected.source_messages),
    )
    run_compass(collected.mvas, submit=args.submit)

    if args.submit:
        mark_messages_read(collected.source_messages)
        log.info("Marked %d source email(s) read.", len(collected.source_messages))
    else:
        log.info("Development mode: all source emails remain unread.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())