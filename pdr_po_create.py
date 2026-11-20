r"""Create FieldPO work orders + purchase orders from Maaco PDR estimate PDFs.

For each estimate PDF in the input folder:
  1. Read claim #, MVA (Stock/RO#), VIN and total from the PDF.
     Grand Total, Net Total and Customer Total must all agree.
  2. Compass: open the vehicle by MVA and read the Wizard Odometer.
  3. FieldPO: Create -> Single Work Order -> search VIN (exactly one
     match) -> Vehicle Repair -> mileage, photo, Unknown/Customer Damage,
     "WOR", Off-lot -> NEXT (this creates the WO, logged at once).
  4. Add Purchase Order: Body Damage, Minor / Paint Repairs, Bumper ->
     MAACO FOREST PARK (exact supplier code; the auto-filled email must
     match) -> amount (read back) -> review page checked line by
     line -> Send PO unchecked -> Submit -> PO# logged.

Safety (never guess):
  - Any mismatch skips that estimate; the others continue.
  - A claim already in the log with a PO is skipped. Before creating a WO the
    vehicle's Active Work Order tab is read: a DRAFT WO by WO_CREATOR with
    no PO is reused (also how a logged wo_created claim resumes); anything
    else there (a PO, another creator, several WOs) stops that estimate.
  - Default is a DRY RUN: a new WO stops before NEXT; on an existing draft
    WO it walks the PO steps and stops at the review page. Nothing is created. ``--live`` asks for confirmation before the
    WO is created and again before the PO is submitted.

Site-specific values (supplier code + email, Compass page URL) live in
config/pdr_po.local.json, which is gitignored; see LOCAL_CONFIG_KEYS.

Run with:
    .venv\Scripts\python.exe pdr_po_create.py [--folder data\pdr_estimates]
        [--stop-after parse|compass] [--claim 13248-1] [--limit N] [--live]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ESTIMATE_DIR = BASE_DIR / "data" / "pdr_estimates"
PO_LOG_PATH = BASE_DIR / "data" / "pdr_po_log.csv"
OUTPUT_DIR = BASE_DIR / "log" / "pdr_po"

FIELDPO_URL = "https://supply-chain.east.prod.sdp.abg.cloud/fieldpo/dashboard"
# Long enough to finish a Microsoft sign-in + Authenticator prompt.
LOGIN_TIMEOUT_MS = 120_000

# Fixed values agreed with the operator (2026-11-19).
EXPENSE_TYPE = "Vehicle Repair"
PARTY_RESPONSIBLE = "Unknown/Customer Damage"
DAMAGE_CAUSE_NOTES = "WOR"  # While On Rent
LOT_LOCATION = "Off-lot"
PO_TYPE = "Body Damage, Minor"
PO_PRODUCT = "Paint Repairs, Bumper"
SUPPLIER_SEARCH = "MAACO FOREST PARK"
SHIP_TO_PREFIX = "3333 BOBBY BROWN PARKWAY"
VENDOR_HEADER = "MAACO FOREST PARK"
WO_CREATOR = "Steele, Dirk"  # only this user's draft WOs are reused
NO_ACTIVE_WO = "No Active Work Order"  # FieldPO text when a vehicle has no WO

# Kept out of the (public) repo: config/pdr_po.local.json
LOCAL_CONFIG_PATH = BASE_DIR / "config" / "pdr_po.local.json"
LOCAL_CONFIG_KEYS = {
    "supplier_code": "FieldPO supplier code shown in brackets, e.g. [094...]",
    "supplier_email": "email FieldPO auto-fills for that supplier",
    "compass_vehicle_url": "Compass vehicle page URL with {mva} placeholder",
}
SUPPLIER_CODE = ""
SUPPLIER_EMAIL = ""
COMPASS_VEHICLE_URL = ""


def load_local_config(path: Path = LOCAL_CONFIG_PATH) -> None:
    """Load the site-specific values into the module constants."""
    global SUPPLIER_CODE, SUPPLIER_EMAIL, COMPASS_VEHICLE_URL
    if not path.exists():
        raise SystemExit(f"Missing {path} with keys: {LOCAL_CONFIG_KEYS}")
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in LOCAL_CONFIG_KEYS if not str(data.get(k, "")).strip()]
    if missing or "{mva}" not in data.get("compass_vehicle_url", ""):
        raise SystemExit(f"{path} is missing/invalid: {missing or ['compass_vehicle_url needs {mva}']}")
    SUPPLIER_CODE = data["supplier_code"].strip()
    SUPPLIER_EMAIL = data["supplier_email"].strip().upper()
    COMPASS_VEHICLE_URL = data["compass_vehicle_url"].strip()


STATUS_WO_CREATED = "wo_created"
STATUS_PO_CREATED = "po_created"
LOG_FIELDS = ["claim", "mva", "vin", "total", "mileage", "wo", "po", "status", "updated_at", "note"]

log = logging.getLogger("pdr_po_create")


class EstimateError(ValueError):
    """An estimate cannot be processed safely; skip it."""


@dataclass(frozen=True, slots=True)
class PdrEstimate:
    path: Path
    claim: str
    mva: str  # 9 digits, leading zero restored
    vin: str
    total: Decimal


# ---------------------------------------------------------------- PDF parsing


def _one(pattern: str, text: str, what: str, flags: int = re.MULTILINE) -> str:
    values = {m.strip() for m in re.findall(pattern, text, flags)}
    if len(values) != 1:
        raise EstimateError(f"Expected exactly one {what}, found {len(values)}")
    return values.pop()


def _money(value: str, what: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise EstimateError(f"Unreadable {what}: {value!r}") from exc


def parse_estimate_text(text: str, path: Path = Path("?")) -> PdrEstimate:
    """Extract the PO fields from Maaco estimate text. Raises EstimateError."""
    if VENDOR_HEADER not in text.upper():
        raise EstimateError(f"Not a {VENDOR_HEADER} document")
    if not re.search(r"(?m)^\s*Estimate\s*$", text):
        raise EstimateError("No 'Estimate' heading")

    claim = _one(r"Claim:\s*(\S+)", text, "claim #")
    job = re.findall(r"Job #:\s*(\S+)", text)
    if job and set(job) != {claim}:
        raise EstimateError(f"Claim {claim} and Job # {job} disagree")

    vin = _one(r"VIN:\s*([A-HJ-NPR-Z0-9]{17})\b", text, "VIN")
    mva = _one(r"Stock/RO#:\s*(\d{8,9})\b", text, "Stock/RO# (MVA)").zfill(9)

    totals = {
        "Grand Total": _money(_one(r"^\s*Grand Total\s+\$?([\d,]+\.\d{2})\s*$", text, "Grand Total"), "Grand Total"),
        "Net Total": _money(_one(r"^\s*Net Total\s+\$?([\d,]+\.\d{2})\s*$", text, "Net Total"), "Net Total"),
        "Customer Total": _money(_one(r"Customer Total \$:\s*([\d,]+\.\d{2})", text, "Customer Total"), "Customer Total"),
    }
    if len(set(totals.values())) != 1:
        raise EstimateError(f"Totals disagree: {totals}")
    total = totals["Grand Total"]
    if total <= 0:
        raise EstimateError(f"Total is not positive: {total}")
    return PdrEstimate(path=path, claim=claim, mva=mva, vin=vin, total=total)


def parse_estimate_pdf(path: Path) -> PdrEstimate:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_estimate_text(text, path)


# ------------------------------------------------------------- pure helpers


def normalize_mva(value: str) -> str:
    """Compare MVAs as numbers: FieldPO's review page drops the leading zero."""
    return str(value).strip().lstrip("0")


def parse_odometer(text: str) -> int:
    match = re.search(r"Wizard Odometer\s*([\d,]+)\s*mi\b", text)
    if not match:
        raise EstimateError("Compass page shows no Wizard Odometer reading")
    miles = int(match.group(1).replace(",", ""))
    if miles <= 0:
        raise EstimateError(f"Wizard Odometer is not positive: {miles}")
    return miles


def extract_number(text: str, prefix: str) -> str:
    """Return the single '#WO123' / '#FPO123' number shown in a popup."""
    values = set(re.findall(rf"#\s*({prefix}\d+)\b", text))
    if len(values) != 1:
        raise EstimateError(f"Expected one #{prefix} number on screen, found {sorted(values)}")
    return values.pop()


def verify_vehicle_details(text: str, estimate: PdrEstimate) -> list[str]:
    """FieldPO vehicle page ('MVA#  060767383', 'VIN#  3GK...') must match the PDF."""
    problems: list[str] = []
    mva = re.search(r"MVA#\s*(\d{8,9})\b", text)
    if not mva or normalize_mva(mva.group(1)) != normalize_mva(estimate.mva):
        problems.append(f"MVA# is {mva.group(1) if mva else None}, expected {estimate.mva}")
    vin = re.search(r"VIN#\s*([A-HJ-NPR-Z0-9]{17})\b", text)
    if not vin or vin.group(1) != estimate.vin:
        problems.append(f"VIN# is {vin.group(1) if vin else None}, expected {estimate.vin}")
    return problems


@dataclass(frozen=True, slots=True)
class ActiveWorkOrder:
    wos: tuple[str, ...]
    draft: bool
    created_by: str
    has_po: bool


def parse_active_work_order(text: str) -> ActiveWorkOrder | None:
    """Read the vehicle's 'Active Work Order' tab. None when no WO is shown."""
    wos = tuple(sorted(set(re.findall(r"\bWO\d{5,}\b", text))))
    if not wos:
        return None
    return ActiveWorkOrder(
        wos=wos,
        draft=bool(re.search(r"\bDRAFT\b", text, re.IGNORECASE)),
        created_by=_review_value(text, "Created By") or "",
        # Anything but an explicit "No Purchase Orders" counts as having a PO.
        has_po=bool(re.search(r"\bFPO\d+", text)) or "No Purchase Orders" not in text,
    )


def _same_person(a: str, b: str) -> bool:
    return re.sub(r"\s+", "", a).casefold() == re.sub(r"\s+", "", b).casefold()


def decide_existing_wo(active: ActiveWorkOrder | None, logged_wo: str = "") -> str | None:
    """Return the draft WO# to reuse, None to create a new WO, or raise."""
    if active is None:
        if logged_wo:
            raise EstimateError(f"Log says {logged_wo}, but FieldPO shows no active work order for this vehicle")
        return None
    if len(active.wos) != 1:
        raise EstimateError(f"Vehicle shows several work orders {list(active.wos)} - check by hand")
    wo = active.wos[0]
    if logged_wo and wo != logged_wo:
        raise EstimateError(f"Log says {logged_wo}, but the vehicle's active work order is {wo}")
    if active.has_po:
        raise EstimateError(f"{wo} already has a purchase order - check by hand")
    if not active.draft:
        raise EstimateError(f"{wo} is not a DRAFT - check by hand")
    if not _same_person(active.created_by, WO_CREATOR):
        raise EstimateError(f"{wo} was created by {active.created_by or 'unknown'}, not {WO_CREATOR}")
    return wo


def _review_value(text: str, label: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(label)}:[ \t]*\n?[ \t]*(.+?)\s*$", text)
    return match.group(1).strip() if match else None


def verify_review(text: str, estimate: PdrEstimate, wo: str, remark: str) -> list[str]:
    """Compare the PO review page with what we intended. Returns problems."""
    problems: list[str] = []
    wo_seen = re.search(r"WO#\s*(WO\d+)", text)
    if not wo_seen or wo_seen.group(1) != wo:
        problems.append(f"WO# is {wo_seen.group(1) if wo_seen else None}, expected {wo}")

    def check(label: str, ok, expected: str) -> None:
        value = _review_value(text, label)
        if value is None or not ok(value):
            problems.append(f"{label} is {value!r}, expected {expected}")

    check("MVA", lambda v: normalize_mva(v) == normalize_mva(estimate.mva), estimate.mva)
    check("VIN", lambda v: v.upper() == estimate.vin, estimate.vin)
    check("Type", lambda v: v == PO_TYPE, PO_TYPE)
    check("Product", lambda v: v == PO_PRODUCT, PO_PRODUCT)
    check("Supplier", lambda v: v.upper() == SUPPLIER_SEARCH, SUPPLIER_SEARCH)
    check("Supplier Email", lambda v: v.upper() == SUPPLIER_EMAIL, SUPPLIER_EMAIL)
    check("Ship To", lambda v: v.upper().startswith(SHIP_TO_PREFIX), SHIP_TO_PREFIX)
    check("Remark", lambda v: v == remark, remark)

    def amount_ok(v: str) -> bool:
        try:
            return _money(v, "amount") == estimate.total
        except EstimateError:
            return False

    check("Authorized Amount", amount_ok, f"${estimate.total}")
    return problems


# ---------------------------------------------------------------------- log


def load_log(path: Path = PO_LOG_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["claim"]: row for row in csv.DictReader(handle)}


def write_log_row(row: dict[str, str], path: Path = PO_LOG_PATH) -> None:
    """Insert or replace the row for row['claim'] and save immediately."""
    rows = load_log(path)
    rows[row["claim"]] = {**{f: "" for f in LOG_FIELDS}, **row, "updated_at": datetime.now().isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows.values())
    tmp.replace(path)


def skip_reason(claim: str, logged: dict[str, dict[str, str]]) -> str | None:
    row = logged.get(claim)
    if not row:
        return None
    if row.get("status") == STATUS_PO_CREATED:
        return f"already done: {row.get('wo')} / {row.get('po')}"
    return None  # wo_created: resumed on its existing draft WO


def logged_draft_wo(claim: str, logged: dict[str, dict[str, str]]) -> str:
    row = logged.get(claim) or {}
    return row.get("wo", "") if row.get("status") == STATUS_WO_CREATED else ""


# ----------------------------------------------------------------- browser


def _confirm(prompt: str) -> bool:
    return input(f"\n{prompt} Type y to continue: ").strip().lower() == "y"


def _screenshot(page, name: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = OUTPUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
    try:  # diagnostics only: picture + the page's HTML (to see real elements)
        page.screenshot(path=f"{stem}.png", full_page=True)
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        log.debug("screenshot failed", exc_info=True)


def _body(page) -> str:
    return page.locator("body").inner_text()


def _field_after(page, label: str):
    """The first input/textarea that follows the visible label text."""
    return page.get_by_text(label).first.locator(
        "xpath=(following::input[not(@type='hidden')] | following::textarea | following::select)[1]"
    )


def _wait_for_spinner(page, timeout_ms: int = 30_000) -> None:
    """Wait until FieldPO's loading overlay is gone (it blocks clicks)."""
    try:
        page.locator(".ngx-spinner-overlay").first.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:  # absent or detached already
        pass


def _dismiss_attention_popup(page, wait_ms: int = 8_000) -> None:
    """Click 'Resume Work' on the ATTENTION PLEASE popup ('N FPOs open more
    than 8 days'). It appears only after the dashboard finishes loading, so
    wait for the spinner, then give the popup time to show up."""
    _wait_for_spinner(page)
    resume = page.get_by_role("button", name="Resume Work")
    try:
        resume.first.wait_for(state="visible", timeout=wait_ms)
    except Exception:
        return  # no popup this time
    resume.first.click()
    resume.first.wait_for(state="hidden", timeout=10_000)
    _wait_for_spinner(page)


SIGN_IN_HOST = "login.microsoftonline.com"


def _announce_sign_in(page, site: str, already: bool) -> bool:
    """Log once when a Microsoft sign-in / Authenticator prompt is showing."""
    if not already and SIGN_IN_HOST in page.url:
        log.warning(
            "%s is asking you to sign in - complete it in Edge (approve the Authenticator "
            "prompt). Waiting up to %ds...", site, LOGIN_TIMEOUT_MS // 1000,
        )
        print(f"\n>>> {site} sign-in needed: approve the Microsoft Authenticator prompt in Edge.\n")
        return True
    return already


def read_compass_mileage(page, estimate: PdrEstimate) -> int:
    page.goto(COMPASS_VEHICLE_URL.format(mva=estimate.mva))
    deadline = datetime.now().timestamp() + LOGIN_TIMEOUT_MS / 1000
    last_error = "page did not load"
    announced = False
    while datetime.now().timestamp() < deadline:
        page.wait_for_timeout(2000)
        announced = _announce_sign_in(page, "Compass", announced)
        text = _body(page)
        if estimate.mva not in text:
            last_error = f"Compass page does not show MVA {estimate.mva}"
            continue
        try:
            return parse_odometer(text)
        except EstimateError as exc:
            last_error = str(exc)
    _screenshot(page, f"{estimate.claim}_compass_fail")
    raise EstimateError(last_error)


def _choose_mat_select(page, index: int, value: str) -> None:
    """Pick ``value`` in the index-th Angular Material <mat-select> of PO step 1
    (0 = Select Type, 1 = Choose Product) and confirm it shows as selected.
    Choose Product stays disabled until a Type is picked."""
    select = page.locator("app-product-details mat-select").nth(index)
    deadline = datetime.now().timestamp() + 15
    while select.get_attribute("aria-disabled") == "true":
        if datetime.now().timestamp() > deadline:
            raise EstimateError(f"Dropdown {index + 1} on PO step 1 stayed disabled")
        page.wait_for_timeout(500)
    select.click()
    option = page.get_by_role("option", name=value, exact=True)
    try:
        option.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        listed = page.get_by_role("option").all_inner_texts()
        page.keyboard.press("Escape")
        raise EstimateError(f"'{value}' is not in the list; options: {listed}") from None
    option.first.click()
    _wait_for_spinner(page)
    shown = select.locator(".mat-select-value").inner_text().strip()
    if shown != value:
        raise EstimateError(f"Dropdown shows {shown!r} after choosing {value!r}")


def open_vehicle_page(page, estimate: PdrEstimate) -> None:
    page.goto(FIELDPO_URL)
    try:
        page.wait_for_url("**/fieldpo/dashboard**", timeout=5_000)
    except Exception:
        _announce_sign_in(page, "FieldPO", False)
        page.wait_for_url("**/fieldpo/dashboard**", timeout=LOGIN_TIMEOUT_MS)
    _dismiss_attention_popup(page)
    page.get_by_text("Create", exact=True).click()
    _wait_for_spinner(page)
    page.get_by_text("Single Work Order", exact=True).click()
    page.wait_for_url("**/fieldpo/workOrder**")
    _dismiss_attention_popup(page, wait_ms=2_000)

    # Type key by key: FieldPO only searches on real keystrokes (fill() sets the
    # value silently and no results appear).
    search_box = page.get_by_text("OR", exact=True).locator("xpath=following::input[1]")
    search_box.click()
    search_box.press_sequentially(estimate.vin, delay=60)

    # Result rows read "VIN# <17 chars>"; the "MVA# or VIN#" heading does not match.
    any_result = page.get_by_text(re.compile(r"VIN#\s*[A-HJ-NPR-Z0-9]{17}"))
    try:
        any_result.first.wait_for(timeout=15_000)
    except Exception:
        _screenshot(page, f"{estimate.claim}_vin_search")
        raise EstimateError("VIN search showed no results") from None
    page.wait_for_timeout(1500)  # let the list settle
    count = any_result.count()
    exact = page.get_by_text(re.compile(rf"VIN#\s*{estimate.vin}\b"))
    if count != 1 or exact.count() != 1:
        _screenshot(page, f"{estimate.claim}_vin_search")
        raise EstimateError(f"VIN search returned {count} result(s); need exactly one exact match")
    # The status badge (e.g. ACTIVE) is ignored: some vehicles show none, and
    # the exact single VIN match is the identity check (operator, 2026-11-20).
    exact.first.click()
    _wait_for_spinner(page)

    # Observed order (13248-3, 2026-11-20): VIN result -> "Select Vehicle
    # Expense Type" popup -> Vehicle Repair -> vehicle page (MVA#/VIN#/Odometer)
    # -> "Create Work Order" button -> createWO form.
    # A vehicle that already has a WO may skip the popup: accept either.
    popup = page.get_by_text("Select Vehicle Expense Type")
    details_tab = page.get_by_text("Vehicle Details", exact=True)
    try:
        popup.or_(details_tab).first.wait_for(state="visible", timeout=20_000)
    except Exception:
        _screenshot(page, f"{estimate.claim}_after_vin_click")
        raise EstimateError("Neither the expense-type popup nor the vehicle page appeared") from None
    if popup.count() and popup.first.is_visible():
        log.info("%s - choosing %s", estimate.claim, EXPENSE_TYPE)
        page.get_by_text(EXPENSE_TYPE, exact=True).last.click()
        _wait_for_spinner(page)
    else:
        log.info("%s - no expense-type popup; vehicle page opened directly", estimate.claim)

    try:
        details_tab.first.wait_for(state="visible", timeout=20_000)
    except Exception:
        _screenshot(page, f"{estimate.claim}_vehicle_page")
        raise EstimateError("Vehicle page did not appear") from None
    details_tab.first.click()
    _wait_for_spinner(page)
    page.wait_for_timeout(1000)
    problems = verify_vehicle_details(_body(page), estimate)
    if problems:
        _screenshot(page, f"{estimate.claim}_vehicle_details")
        raise EstimateError("Vehicle page mismatch: " + "; ".join(problems))
    log.info("%s - vehicle page MVA/VIN match", estimate.claim)


def read_active_work_order(page, estimate: PdrEstimate) -> ActiveWorkOrder | None:
    """Open the vehicle's Active Work Order tab and read it."""
    page.get_by_text("Active Work Order", exact=True).first.click()
    _wait_for_spinner(page)
    # Wait until the tab shows either a WO number or FieldPO's explicit
    # "No Active Work Order" - never treat a not-yet-loaded tab as "none".
    deadline = datetime.now().timestamp() + 15
    text = ""
    while datetime.now().timestamp() < deadline:
        page.wait_for_timeout(1000)
        text = _body(page)
        if re.search(r"\bWO\d{5,}\b", text) or NO_ACTIVE_WO in text:
            break
    _screenshot(page, f"{estimate.claim}_active_work_order")
    if not re.search(r"\bWO\d{5,}\b", text) and NO_ACTIVE_WO not in text:
        raise EstimateError("Active Work Order tab showed neither a WO nor 'No Active Work Order'")
    active = parse_active_work_order(text)
    log.info("%s - active work order: %s", estimate.claim, active)
    return active


def open_work_order_form(page, estimate: PdrEstimate) -> None:
    """From the vehicle page: the visible Create Work Order button -> form.

    Both tabs carry a Create Work Order button (the Active Work Order tab's
    "No Active Work Order" state has one too), so use the single visible one.
    """
    create_wo = page.get_by_role("button", name="Create Work Order", exact=True).filter(visible=True)
    try:
        create_wo.first.wait_for(state="visible", timeout=20_000)
    except Exception:
        _screenshot(page, f"{estimate.claim}_vehicle_page")
        raise EstimateError("'Create Work Order' button did not appear") from None
    if create_wo.count() != 1:
        _screenshot(page, f"{estimate.claim}_vehicle_page")
        raise EstimateError(f"Found {create_wo.count()} visible 'Create Work Order' buttons, expected 1")
    log.info("%s - clicking Create Work Order", estimate.claim)
    create_wo.click()
    try:
        page.wait_for_url("**/fieldpo/createWO**", timeout=20_000)
    except Exception:
        _screenshot(page, f"{estimate.claim}_create_wo")
        raise EstimateError("'Create Work Order' did not open the work-order form") from None
    log.info("%s - on the work-order form", estimate.claim)
    _dismiss_attention_popup(page, wait_ms=2_000)


def fill_work_order_form(page, estimate: PdrEstimate, mileage: int, photo: Path) -> None:
    # Selectors from the saved createWO page (2026-11-20). Labels carry a red
    # "*" in the same element, so exact label-text matching does not work.
    def radio(label: str):
        return page.locator("label", has_text=re.compile(rf"^\s*{re.escape(label)}\s*$")).locator(
            "input[type=radio]"
        )

    mileage_box = page.locator("input.odometerInputVal")
    mileage_box.fill(str(mileage))
    log.info("%s - mileage %s entered", estimate.claim, mileage)

    with page.expect_file_chooser() as chooser:
        page.get_by_text("Add Photo", exact=True).click()
    chooser.value.set_files(str(photo))
    page.wait_for_timeout(2000)
    log.info("%s - photo attached", estimate.claim)

    radio(PARTY_RESPONSIBLE).check()
    notes_box = page.locator("input.ROInputVal")
    notes_box.fill(DAMAGE_CAUSE_NOTES)
    radio(LOT_LOCATION).check()
    page.wait_for_timeout(500)

    problems = []
    if mileage_box.input_value().strip() != str(mileage):
        problems.append(f"mileage shows {mileage_box.input_value()!r}")
    if not radio(PARTY_RESPONSIBLE).is_checked():
        problems.append(f"{PARTY_RESPONSIBLE} not selected")
    if notes_box.input_value() != DAMAGE_CAUSE_NOTES:
        problems.append(f"notes show {notes_box.input_value()!r}")
    if not radio(LOT_LOCATION).is_checked():
        problems.append(f"{LOT_LOCATION} not selected")
    if problems:
        _screenshot(page, f"{estimate.claim}_work_order_form")
        raise EstimateError("Work-order form check failed: " + "; ".join(problems))
    log.info("%s - form filled and checked (party, notes, lot)", estimate.claim)


def create_work_order(page) -> str:
    page.get_by_role("button", name="NEXT", exact=True).click()
    page.get_by_text("Work Order Created").wait_for(timeout=30_000)
    return extract_number(_body(page), "WO")


def start_po_from_created_popup(page) -> None:
    page.get_by_text("Add Purchase Order", exact=True).click()
    page.wait_for_url("**/fieldpo/createPO**", timeout=20_000)


def start_po_on_existing_wo(page) -> None:
    """On the vehicle's Active Work Order tab: Create Purchase Order."""
    page.get_by_role("button", name="Create Purchase Order", exact=True).click()
    page.wait_for_url("**/fieldpo/createPO**", timeout=20_000)
    _wait_for_spinner(page)


def create_purchase_order(
    page, estimate: PdrEstimate, wo: str, remark: str, live: bool = True, ask: bool = True
) -> str | None:
    """Fill PO steps 1-4 (already on createPO). Dry run stops at the review page."""
    log.info("%s - PO step 1: %s / %s", estimate.claim, PO_TYPE, PO_PRODUCT)

    # Step 1 - product
    _choose_mat_select(page, 0, PO_TYPE)
    _choose_mat_select(page, 1, PO_PRODUCT)
    page.get_by_role("button", name="Next", exact=True).click()

    # Step 2 - supplier: exactly one option carrying the Forest Park code
    log.info("%s - PO step 2: supplier", estimate.claim)
    _field_after(page, "Choose Supplier").fill(SUPPLIER_SEARCH)
    page.wait_for_timeout(2000)
    code = page.get_by_text(SUPPLIER_CODE, exact=False)
    if code.count() != 1:
        raise EstimateError(f"Supplier search shows {code.count()} entries with {SUPPLIER_CODE}")
    code.first.click()
    page.wait_for_timeout(1500)
    email = _field_after(page, "Supplier Email").input_value().strip()
    if email.upper() != SUPPLIER_EMAIL:
        raise EstimateError(f"Supplier email auto-filled {email!r}, expected {SUPPLIER_EMAIL} - wrong supplier?")
    _field_after(page, "Remark").fill(remark)
    page.get_by_role("button", name="Next", exact=True).click()

    # Step 3 - amount, read back
    log.info("%s - PO step 3: amount %s", estimate.claim, estimate.total)
    amount_box = _field_after(page, "Enter amount")
    amount_box.fill(f"{estimate.total:.2f}")
    page.wait_for_timeout(500)
    shown = amount_box.input_value()
    try:
        shown_amount = _money(shown, "amount")
    except EstimateError:
        shown_amount = None
    if shown_amount != estimate.total:
        raise EstimateError(f"Amount field shows {shown!r} after typing {estimate.total}")
    page.get_by_role("button", name="Next", exact=True).click()

    # Step 4 - review
    page.get_by_text("Purchase Order Review").wait_for(timeout=15_000)
    page.wait_for_timeout(1000)
    problems = verify_review(_body(page), estimate, wo, remark)
    send_box = page.get_by_role("checkbox").first
    if send_box.is_checked():
        send_box.uncheck()
    if send_box.is_checked():
        problems.append("Could not uncheck 'Send PO details to supplier'")
    _screenshot(page, f"{estimate.claim}_review")
    if problems:
        raise EstimateError("Review page mismatch: " + "; ".join(problems))
    log.info("%s - PO step 4: review page verified, Send PO unchecked", estimate.claim)
    if not live:
        return None

    if ask and not _confirm(
        f"Review page verified for claim {estimate.claim}: {wo}, MVA {estimate.mva}, "
        f"VIN {estimate.vin}, {SUPPLIER_SEARCH}, ${estimate.total}, Send PO unchecked. SUBMIT the PO?"
    ):
        return _manual_submit(estimate, wo)
    page.get_by_role("button", name="Submit", exact=True).click()
    page.get_by_text("Purchase Order Created").wait_for(timeout=30_000)
    po = extract_number(_body(page), "FPO")
    _screenshot(page, f"{estimate.claim}_po_created")
    return po


def _manual_submit(estimate: PdrEstimate, wo: str) -> str:
    """Operator declined Submit: leave the review page open for them.

    If they submit it themselves, they type the PO# so it is logged.
    """
    while True:
        answer = input(
            f"\nNot submitted. The browser stays on the review page for {wo}.\n"
            "If you click Submit yourself, type the FPO# shown (e.g. FPO1208449) to log it;\n"
            "otherwise just press Enter (the WO stays a draft and is logged as such): "
        ).strip().upper()
        if not answer:
            raise EstimateError("Operator declined Submit; WO left as draft")
        if re.fullmatch(r"FPO\d{7,}", answer):
            return answer
        print(f"{answer!r} is not a PO number like FPO1208449.")


def render_photo(estimate: PdrEstimate) -> Path:
    """Use page 1 of the estimate as the damage image (any photo is accepted)."""
    import pdfplumber

    target = OUTPUT_DIR / "photos" / f"{estimate.claim}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    with pdfplumber.open(estimate.path) as pdf:
        pdf.pages[0].to_image(resolution=110).save(str(target))
    return target


# --------------------------------------------------------------------- main


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", type=Path, default=DEFAULT_ESTIMATE_DIR, help="folder of estimate PDFs")
    parser.add_argument("--stop-after", choices=["parse", "compass"], help="stop after this stage (no FieldPO)")
    parser.add_argument("--claim", help="process only this claim #")
    parser.add_argument("--limit", type=int, help="process at most N estimates in FieldPO")
    parser.add_argument("--photo", type=Path, help="damage photo to attach (default: page 1 of the estimate)")
    parser.add_argument("--live", action="store_true", help="really create WOs/POs (asks before each)")
    parser.add_argument(
        "--approve-batch", action="store_true",
        help="with --live: approve the listed estimates once up front instead of two prompts per estimate "
             "(all checks still run; any mismatch stops that estimate)",
    )
    return parser.parse_args(argv)


def _print_table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.approve_batch and not args.live:
        raise SystemExit("--approve-batch only applies with --live")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(BASE_DIR / "PdrPoCreate.log", encoding="utf-8")],
    )
    load_local_config()
    pdfs = sorted(args.folder.glob("*.pdf"))
    if not pdfs:
        log.error("No PDFs in %s", args.folder)
        return 2

    logged = load_log()
    estimates: list[PdrEstimate] = []
    summary: list[tuple[str, ...]] = [("FILE", "CLAIM", "MVA", "VIN", "TOTAL", "STATUS")]
    seen_claims: dict[str, Path] = {}
    for pdf in pdfs:
        try:
            est = parse_estimate_pdf(pdf)
        except Exception as exc:  # noqa: BLE001 - report every bad file, keep going
            summary.append((pdf.name, "-", "-", "-", "-", f"SKIP: {exc}"))
            continue
        if args.claim and est.claim != args.claim:
            continue
        if est.claim in seen_claims:
            summary.append((pdf.name, est.claim, est.mva, est.vin, f"{est.total}", f"SKIP: duplicate of {seen_claims[est.claim].name}"))
            continue
        seen_claims[est.claim] = pdf
        reason = skip_reason(est.claim, logged)
        draft = logged_draft_wo(est.claim, logged)
        status = f"SKIP: {reason}" if reason else (f"resume draft {draft}" if draft else "to do")
        summary.append((pdf.name, est.claim, est.mva, est.vin, f"{est.total}", status))
        if not reason:
            estimates.append(est)

    print("\nEstimates found:")
    _print_table(summary)
    if args.stop_after == "parse" or not estimates:
        return 0

    from playwright.sync_api import sync_playwright

    mode = "LIVE" if args.live else (
        "DRY RUN (new WO: stops before NEXT; existing draft WO: stops at the PO review page)"
    )
    print(f"\nMode: {mode}")
    results: list[tuple[str, ...]] = [("CLAIM", "MILEAGE", "WO", "PO", "RESULT")]
    with sync_playwright() as p:
        # Edge, with the same project Edge profile work-item closure uses
        # (.edge-playwright-profile by default), so Compass sign-in is shared.
        from playwright_prototype.config import resolve_edge_profile_directory, resolve_edge_user_data_dir

        context = p.chromium.launch_persistent_context(
            user_data_dir=str(resolve_edge_user_data_dir()),
            channel="msedge",
            headless=False,
            no_viewport=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--profile-directory={resolve_edge_profile_directory()}",
                "--start-maximized",
            ],
        )
        compass = context.new_page()
        mileage: dict[str, int] = {}
        for est in estimates:
            try:
                mileage[est.claim] = read_compass_mileage(compass, est)
                log.info("%s - MVA %s Wizard Odometer %s", est.claim, est.mva, mileage[est.claim])
            except Exception as exc:  # noqa: BLE001
                results.append((est.claim, "-", "-", "-", f"SKIP (Compass): {exc}"))
        compass.close()

        if args.stop_after == "compass":
            for est in estimates:
                if est.claim in mileage:
                    results.append((est.claim, str(mileage[est.claim]), "-", "-", "mileage read"))
        else:
            page = context.pages[0] if context.pages else context.new_page()
            todo = [e for e in estimates if e.claim in mileage][: args.limit]
            ask_each = True
            if args.live and args.approve_batch and todo:
                print("\nThese work orders / POs will be created (supplier MAACO FOREST PARK, Send PO unchecked):")
                _print_table([("CLAIM", "MVA", "VIN", "MILEAGE", "AMOUNT")] + [
                    (e.claim, e.mva, e.vin, str(mileage[e.claim]), f"${e.total}") for e in todo
                ])
                total = sum((e.total for e in todo), Decimal("0"))
                answer = input(f"\nApprove these {len(todo)} POs totalling ${total}? Type YES to run without "
                               "further prompts (anything else cancels): ").strip()
                if answer != "YES":
                    print("Cancelled - nothing was created.")
                    todo = []
                else:
                    ask_each = False
                    log.info("Batch approved by operator: %s ($%s)", [e.claim for e in todo], total)
            for est in todo:
                miles = mileage[est.claim]
                wo = ""
                remark = f"Maaco estimate {est.claim}"

                def log_row(est=est, miles=miles, **extra: str) -> None:
                    write_log_row({"claim": est.claim, "mva": est.mva, "vin": est.vin, "total": f"{est.total}",
                                   "mileage": str(miles), **extra})

                try:
                    open_vehicle_page(page, est)
                    existing = decide_existing_wo(
                        read_active_work_order(page, est), logged_draft_wo(est.claim, load_log())
                    )
                    if existing:
                        # Reuse the draft WO (made earlier by hand or by a run that stopped).
                        wo = existing
                        log.info("%s - reusing draft %s", est.claim, wo)
                        if args.live:
                            log_row(wo=wo, status=STATUS_WO_CREATED, note="reusing existing draft WO")
                        start_po_on_existing_wo(page)
                    else:
                        photo = args.photo or render_photo(est)
                        open_work_order_form(page, est)
                        fill_work_order_form(page, est, miles, photo)
                        _screenshot(page, f"{est.claim}_work_order_form")
                        if not args.live:
                            input(f"\nDRY RUN {est.claim}: form filled, NOT submitted. Check the browser, then press Enter...")
                            results.append((est.claim, str(miles), "-", "-", "dry run OK (WO form)"))
                            continue
                        if ask_each and not _confirm(
                            f"No active WO on this vehicle. Create WORK ORDER for claim {est.claim}: MVA {est.mva}, "
                            f"VIN {est.vin}, mileage {miles}, total ${est.total}? (Do not click NEXT yourself.)"
                        ):
                            results.append((est.claim, str(miles), "-", "-", "declined"))
                            continue
                        wo = create_work_order(page)
                        log_row(wo=wo, status=STATUS_WO_CREATED)
                        log.info("%s - created %s", est.claim, wo)
                        start_po_from_created_popup(page)

                    po = create_purchase_order(page, est, wo, remark, live=args.live, ask=ask_each)
                    if po is None:
                        input(f"\nDRY RUN {est.claim}: PO review page for {wo} verified, NOT submitted. "
                              "Check the browser, then press Enter...")
                        results.append((est.claim, str(miles), wo, "-", "dry run OK (PO review)"))
                        continue
                    log_row(wo=wo, po=po, status=STATUS_PO_CREATED)
                    log.info("%s - created %s for $%s", est.claim, po, est.total)
                    results.append((est.claim, str(miles), wo, po, "CREATED"))
                except Exception as exc:  # noqa: BLE001 - one bad estimate must not stop the rest
                    _screenshot(page, f"{est.claim}_error")
                    log.exception("%s - failed", est.claim)
                    if wo and args.live:
                        log_row(wo=wo, status=STATUS_WO_CREATED, note=" ".join(str(exc).split())[:200])
                    results.append((est.claim, str(miles), wo or "-", "-", f"FAILED: {exc}"))
            input("\nDone. Press Enter to close the browser...")
        context.close()

    print("\nResults:")
    _print_table(results)
    print(f"\nLog: {PO_LOG_PATH}\nScreenshots: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
