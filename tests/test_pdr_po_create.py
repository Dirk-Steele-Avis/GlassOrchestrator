from decimal import Decimal
from pathlib import Path

import pytest

import pdr_po_create as m


@pytest.fixture(autouse=True)
def fake_site_config(monkeypatch):
    """Site values normally come from config/pdr_po.local.json (not in repo)."""
    monkeypatch.setattr(m, "SUPPLIER_CODE", "[000TESTSHOP]")
    monkeypatch.setattr(m, "SUPPLIER_EMAIL", "SHOP@EXAMPLE.COM")
    monkeypatch.setattr(m, "COMPASS_VEHICLE_URL", "https://example.invalid/?mva={mva}")

# Synthetic estimate in the Maaco layout (fake VIN / claim / stock numbers).
ESTIMATE = """MAACO FOREST PARK - M2871 Workfile ID: abc123
445 Forest Pkwy, Forest Park, GA 30297
Estimate
RO Number:
AVIS - PDR AVIS - PDR Phone: Create Date: 11/18/2026
3333 Bobby Brown Parkway Claim: 99999-1
VIN: 1HGCM82633A004352 Interior Color: Mileage In: Vehicle Out:
State: Production Date: Condition: Job #: 99999-1
1 E01 PDR*** 1 90.00 Other
4 E01 TOUCH UP*** 5 75.00 Other
10 E01 Stock/RO#: 12345678
Miscellaneous 165.00
Subtotal 165.00
Sales Tax 0.00
Grand Total 165.00
Net Total 165.00
Customer Total $: 165.00
"""


def test_parses_estimate_and_restores_leading_zero():
    est = m.parse_estimate_text(ESTIMATE)
    assert est.claim == "99999-1"
    assert est.mva == "012345678"
    assert est.vin == "1HGCM82633A004352"
    assert est.total == Decimal("165.00")


@pytest.mark.parametrize(
    "old,new,message",
    [
        ("Net Total 165.00", "Net Total 160.00", "Totals disagree"),
        ("Grand Total 165.00\n", "", "Grand Total"),
        ("MAACO FOREST PARK", "MAACO DALLAS", "Not a MAACO FOREST PARK"),
        ("Job #: 99999-1", "Job #: 88888-1", "disagree"),
        ("Stock/RO#: 12345678", "Stock/RO#:", "Stock/RO#"),
        ("VIN: 1HGCM82633A004352", "VIN:", "VIN"),
    ],
)
def test_rejects_unsafe_estimates(old, new, message):
    with pytest.raises(m.EstimateError, match=message):
        m.parse_estimate_text(ESTIMATE.replace(old, new))


def test_odometer_and_popup_numbers():
    assert m.parse_odometer("Wizard Odometer\n35,632 mi\nWizard Last Movement") == 35632
    with pytest.raises(m.EstimateError):
        m.parse_odometer("Wizard Odometer\n—")
    assert m.extract_number("Purchase Order Created\n#FPO1208449\nView", "FPO") == "FPO1208449"
    assert m.extract_number("Work Order Created\n#WO844355", "WO") == "WO844355"
    with pytest.raises(m.EstimateError):
        m.extract_number("no number", "FPO")


def _review(**overrides):
    values = {
        "MVA": "12345678",  # FieldPO drops the leading zero
        "VIN": "1HGCM82633A004352",
        "Type": m.PO_TYPE,
        "Product": m.PO_PRODUCT,
        "Supplier": "MAACO FOREST PARK",
        "Supplier Email": "shop@example.com",
        "Ship To": "3333 BOBBY BROWN PARKWAY, COLLEGE PARK, GA, US, 30337",
        "Remark": "Maaco estimate 99999-1",
        "Authorized Amount": "$165.00",
    }
    values.update(overrides)
    lines = ["Purchase Order Review", "WO#\tWO844355"] + [f"{k}:\n{v}" for k, v in values.items()]
    return "\n".join(lines)


def test_review_passes_when_everything_matches():
    est = m.parse_estimate_text(ESTIMATE)
    assert m.verify_review(_review(), est, "WO844355", "Maaco estimate 99999-1") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("Authorized Amount", "$654.00"),
        ("Supplier", "Collision Club Inc dba Maaco"),
        ("Supplier Email", "maacophl@gmail.com"),
        ("VIN", "1HGCM82633A004353"),
        ("MVA", "12345679"),
    ],
)
def test_review_catches_wrong_values(field, value):
    est = m.parse_estimate_text(ESTIMATE)
    problems = m.verify_review(_review(**{field: value}), est, "WO844355", "Maaco estimate 99999-1")
    assert any(p.startswith(field) for p in problems)


def test_review_catches_wrong_work_order():
    est = m.parse_estimate_text(ESTIMATE)
    assert m.verify_review(_review(), est, "WO000001", "Maaco estimate 99999-1")


def test_vehicle_details_must_match_estimate():
    est = m.parse_estimate_text(ESTIMATE)
    page = "MVA#\t012345678\nVIN#\t1HGCM82633A004352\nProgram Code:\tRisk (ABG Vehicle)\nOdometer Reading\t13544"
    assert m.verify_vehicle_details(page, est) == []
    assert m.verify_vehicle_details(page.replace("012345678", "012345679"), est)
    assert m.verify_vehicle_details(page.replace("A004352", "A004353"), est)
    assert m.verify_vehicle_details("nothing here", est)


def test_log_skips_done_and_half_done_claims(tmp_path: Path):
    path = tmp_path / "log.csv"
    m.write_log_row({"claim": "A", "wo": "WO1", "po": "FPO1", "status": m.STATUS_PO_CREATED}, path)
    m.write_log_row({"claim": "B", "wo": "WO2", "status": m.STATUS_WO_CREATED}, path)
    logged = m.load_log(path)
    assert "already done" in m.skip_reason("A", logged)
    assert m.skip_reason("B", logged) is None  # resumed, not skipped
    assert m.logged_draft_wo("B", logged) == "WO2"
    assert m.logged_draft_wo("A", logged) == ""
    assert m.skip_reason("C", logged) is None
    m.write_log_row({"claim": "B", "wo": "WO2", "po": "FPO2", "status": m.STATUS_PO_CREATED}, path)
    assert len(m.load_log(path)) == 2


def test_local_config_required_and_loaded(tmp_path: Path):
    with pytest.raises(SystemExit):
        m.load_local_config(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"supplier_code": "[X]", "supplier_email": "a@b.c", "compass_vehicle_url": "no placeholder"}')
    with pytest.raises(SystemExit):
        m.load_local_config(bad)
    good = tmp_path / "good.json"
    good.write_text('{"supplier_code": "[X]", "supplier_email": "a@b.c", "compass_vehicle_url": "https://x/?m={mva}"}')
    m.load_local_config(good)
    assert (m.SUPPLIER_CODE, m.SUPPLIER_EMAIL) == ("[X]", "A@B.C")


ACTIVE_TAB = """GMC TERRAIN FWD
60767383 | XAR751 | Silver
Active Work Order
Vehicle Details
WO844586
DRAFT
Odometer:
13544
Date Created:
2026-11-20
Created By:
Steele, Dirk
Days Open:
0
Images(01)
Void WO
Close WO
Purchase Orders
No Purchase Orders
Create Purchase Order"""


def test_parse_active_work_order_draft_without_po():
    active = m.parse_active_work_order(ACTIVE_TAB)
    assert active == m.ActiveWorkOrder(wos=("WO844586",), draft=True, created_by="Steele, Dirk", has_po=False)
    assert m.parse_active_work_order("Active Work Order\nVehicle Details\nnothing here") is None


def test_reuses_own_draft_wo_without_po():
    active = m.parse_active_work_order(ACTIVE_TAB)
    assert m.decide_existing_wo(active) == "WO844586"
    assert m.decide_existing_wo(active, logged_wo="WO844586") == "WO844586"
    assert m.decide_existing_wo(None) is None  # nothing there: create a new WO


@pytest.mark.parametrize(
    "text,logged,message",
    [
        (ACTIVE_TAB.replace("No Purchase Orders", "FPO1208450\n$120.00"), "", "already has a purchase order"),
        (ACTIVE_TAB.replace("Steele, Dirk", "Doe, Jane"), "", "created by Doe, Jane"),
        (ACTIVE_TAB.replace("DRAFT", "IN PROGRESS"), "", "not a DRAFT"),
        (ACTIVE_TAB + "\nWO844999", "", "several work orders"),
        (ACTIVE_TAB, "WO111111", "Log says WO111111"),
    ],
)
def test_existing_wo_conflicts_stop(text, logged, message):
    with pytest.raises(m.EstimateError, match=message):
        m.decide_existing_wo(m.parse_active_work_order(text), logged_wo=logged)


def test_logged_draft_without_active_wo_stops():
    with pytest.raises(m.EstimateError, match="no active work order"):
        m.decide_existing_wo(None, logged_wo="WO844586")


WO_WITH_PO = """Active Work Order
Vehicle Details
WO844645
IN PROGRESS
Odometer:
24025
Date Created:
2026-11-20
Created By:
Steele, Dirk
Days Open:
0
Images(01)
Void WO
Close WO
Purchase Orders [1]
PO# FPO1208854
IN PROGRESS
Paint Repairs, Bumper
$220.00
MAACO FOREST PARK
Created by Steele, Dirk on 2026-11-20
Create Purchase Order"""


def test_wo_that_already_has_a_po_is_never_reused():
    """Real screen after 13248-5 was finished (2026-11-20)."""
    active = m.parse_active_work_order(WO_WITH_PO)
    assert active.has_po and not active.draft
    with pytest.raises(m.EstimateError, match="already has a purchase order"):
        m.decide_existing_wo(active, logged_wo="WO844645")
