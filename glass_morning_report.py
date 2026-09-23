"""Generate a copy-ready Glass Damage Morning Report from GlassClaims."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import gspread


ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"
CONFIG_PATHS = (
    ROOT / "orchestrator_config.json",
    ROOT / "orchestrator_project.json",
    ROOT / "orchestrator_project.local.json",
    ROOT / "orchestrator_config.local.json",
    ROOT / "config" / "config.local.json",
)
REQUIRED_HEADERS = (
    "Inventory Date", "Original Date", "MVA", "VIN", "Make",
    "Location", "Action", "Area", "Claim#",
)
OPTIONAL_HEADERS = ("Next Action", "WorkItemCreated")
HOME_LOCATIONS = frozenset({"BB", "APO"})
SECTION_ORDER = (
    "AGN (Replacements)", "AGN Repair", "Super Glass (Repairs)",
    "AVIS (TBK)", "Local Market", "Other",
)
STALE_DAYS = 14
RED = "#d4002a"
BLACK = "#111111"
FONT = "'Segoe UI',Calibri,Arial,sans-serif"
REPORT_COLUMNS = (
    ("Inv", 44), ("Orig", 44), ("MVA", 76), ("Next action", 116),
    ("VIN", 221), ("Make", 78), ("Location", 68), ("Action", 108),
    ("Area", 96), ("Claim #", 72), ("Work item", 75), ("Age", 52),
)


@dataclass(frozen=True)
class ReportRow:
    inventory_date: str
    original_date: str
    original_key: date | None
    age: int | None
    mva: str
    next_action: str
    vin: str
    make: str
    location: str
    verb: str
    vendor: str
    area: str
    claim: str
    work_item: str

    @property
    def is_local(self) -> bool:
        return bool(self.location) and self.location.upper() not in HOME_LOCATIONS

    @property
    def needs_photo(self) -> bool:
        return bool(re.search(r"pic|photo", self.next_action, re.IGNORECASE))


@dataclass(frozen=True)
class ReportSection:
    title: str
    rows: tuple[ReportRow, ...]

    @property
    def missing(self) -> int:
        return sum("missing" in row.claim.lower() for row in self.rows)

    @property
    def listed(self) -> int:
        return sum("listed" in row.claim.lower() for row in self.rows)


def _parse_date(value: str) -> date | None:
    for pattern in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            pass
    return None


def _short_date(value: str) -> str:
    parsed = _parse_date(value)
    return f"{parsed.month}/{parsed.day}" if parsed else value.strip()


def _parse_action(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"\s*([A-Za-z]+)\s*\(\s*([^)]*)\s*\)\s*", value)
    if match:
        return match.group(1).capitalize(), match.group(2).strip()
    cleaned = value.strip()
    return (cleaned.capitalize(), "") if cleaned else ("", "")


def build_rows(values: list[list[str]], *, today: date) -> list[ReportRow]:
    """Build report rows whose Inventory Date equals today.

    Next Action and WorkItemCreated are optional enrichment columns; a sheet
    built strictly from the documented canonical schema still generates a
    report, just without that enrichment.
    """
    if not values:
        raise RuntimeError("GlassClaims is empty; expected a header row")
    header = values[0]
    missing = [name for name in REQUIRED_HEADERS if name not in header]
    if missing:
        raise RuntimeError(f"GlassClaims is missing required column(s): {', '.join(missing)}")
    indexes = {name: header.index(name) for name in REQUIRED_HEADERS}
    optional_indexes = {name: header.index(name) for name in OPTIONAL_HEADERS if name in header}
    rows: list[ReportRow] = []
    for source in values[1:]:
        cells = source + [""] * max(0, len(header) - len(source))
        inventory = cells[indexes["Inventory Date"]].strip()
        if _parse_date(inventory) != today:
            continue
        vin = cells[indexes["VIN"]].strip()
        if not vin:
            continue
        original = cells[indexes["Original Date"]].strip()
        original_key = _parse_date(original)
        verb, vendor = _parse_action(cells[indexes["Action"]])
        next_action_idx = optional_indexes.get("Next Action")
        work_item_idx = optional_indexes.get("WorkItemCreated")
        rows.append(ReportRow(
            inventory_date=_short_date(inventory),
            original_date=_short_date(original), original_key=original_key,
            age=max(0, (today - original_key).days) if original_key else None,
            mva=cells[indexes["MVA"]].strip(),
            next_action=cells[next_action_idx].strip() if next_action_idx is not None else "",
            vin=vin,
            make=cells[indexes["Make"]].strip(),
            location=cells[indexes["Location"]].strip(), verb=verb, vendor=vendor,
            area=cells[indexes["Area"]].strip(), claim=cells[indexes["Claim#"]].strip(),
            work_item=cells[work_item_idx].strip() if work_item_idx is not None else "",
        ))
    return rows


def build_sections(rows: list[ReportRow]) -> list[ReportSection]:
    groups: dict[str, list[ReportRow]] = {title: [] for title in SECTION_ORDER}
    for row in rows:
        compact_vendor = row.vendor.replace(" ", "").lower()
        if row.is_local:
            title = "Local Market"
        elif row.vendor.upper() == "AGN" and row.verb == "Replace":
            title = "AGN (Replacements)"
        elif row.vendor.upper() == "AGN" and row.verb == "Repair":
            title = "AGN Repair"
        elif compact_vendor == "superglass":
            title = "Super Glass (Repairs)"
        elif row.vendor.upper() == "AVIS":
            title = "AVIS (TBK)"
        else:
            title = "Other"
        groups[title].append(row)
    return [
        ReportSection(title, tuple(sorted(groups[title], key=lambda row: row.age or 0, reverse=True)))
        for title in SECTION_ORDER if groups[title]
    ]


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _td(content: str, extra: str = "") -> str:
    return f'<td style="padding:7px 5px;border-bottom:1px solid #ececec;font-family:{FONT};font-size:12px;color:#111;vertical-align:middle;overflow:hidden;{extra}">{content}</td>'


def _report_row_html(row: ReportRow) -> str:
    if row.needs_photo:
        next_action = f'<span style="background:{RED};color:#fff;font-size:11px;font-weight:700;">&nbsp;NEEDS PHOTO&nbsp;</span>'
    elif row.next_action:
        next_action = f'<span style="font-family:Consolas,monospace;font-size:12px;">{_esc(row.next_action)}</span>'
    else:
        next_action = '<span style="color:#999;">&mdash;</span>'
    action = (f'<b>{_esc(row.verb)}</b> <span style="color:#555;font-size:12px;">{_esc(row.vendor)}</span>'
              if row.verb else f'<span style="color:{RED};font-weight:700;">TBD</span>')
    if "missing" in row.claim.lower():
        claim = '<span style="background:#fde8ec;color:#a1001f;font-size:12px;font-weight:700;">&nbsp;Missing&nbsp;</span>'
    elif "listed" in row.claim.lower():
        claim = '<span style="background:#e8e8e8;color:#333;font-size:12px;font-weight:700;">&nbsp;Listed&nbsp;</span>'
    else:
        claim = _esc(row.claim)
    area = _esc(row.area) if row.area and row.area != "??" else f'<span style="color:{RED};font-weight:700;">TBD</span>'
    age_style = f"color:{RED};font-weight:800;" if row.age is not None and row.age >= STALE_DAYS else "color:#555;"
    age = "" if row.age is None else f'<span style="{age_style}">{row.age}d</span>'
    cells = (
        _td(_esc(row.inventory_date), "padding-left:14px;color:#555;"), _td(_esc(row.original_date)),
        _td(f'<span style="font-family:Consolas,monospace;font-size:12px;">{_esc(row.mva)}</span>'),
        _td(next_action), _td(f'<span style="font-family:Consolas,monospace;font-size:11px;white-space:nowrap;">{_esc(row.vin)}</span>', "white-space:nowrap;"),
        _td(_esc(row.make)), _td(_esc(row.location)), _td(action), _td(area), _td(claim),
        _td(_esc(row.work_item), "color:#555;"), _td(age, "text-align:right;padding-right:14px;"),
    )
    return "<tr>" + "".join(cells) + "</tr>"


def _mobile_row_html(row: ReportRow) -> str:
    age_class = " mobile-age-stale" if row.age is not None and row.age >= STALE_DAYS else ""
    age = "" if row.age is None else f'<span class="mobile-age{age_class}">{row.age}d</span>'
    fields = (
        ("VIN", row.vin, " mobile-vin"), ("Next action", row.next_action or "—", ""),
        ("Make", row.make, ""), ("Location", row.location, ""),
        ("Action", f"{row.verb} {row.vendor}".strip() or "TBD", ""),
        ("Area", row.area or "TBD", ""), ("Claim #", row.claim, ""),
        ("Work item", row.work_item, ""), ("Inventory", row.inventory_date, ""),
        ("Original", row.original_date, ""),
    )
    content = "".join(
        f'<div class="mobile-field{extra}"><span>{_esc(label)}</span><b>{_esc(value)}</b></div>'
        for label, value, extra in fields
    )
    return f'<article class="mobile-unit"><div class="mobile-unit-title"><strong>MVA {_esc(row.mva)}</strong>{age}</div><div class="mobile-fields">{content}</div></article>'


def render_report(rows: list[ReportRow], *, today: date) -> str:
    """Render a standalone preview whose report table can be pasted into Outlook."""
    sections = build_sections(rows)
    photo = [row for row in rows if row.needs_photo or not row.verb]
    stale = sorted(
        [row for row in rows if row.vendor.upper() == "AVIS" and row.age is not None and row.age >= STALE_DAYS],
        key=lambda row: row.age or 0, reverse=True,
    )
    missing = [row for row in rows if "missing" in row.claim.lower()]
    fresh = [row for row in rows if row.original_key == today]

    def details(items: list[ReportRow], formatter) -> str:
        text = [formatter(row) for row in items[:3]]
        if len(items) > 3:
            text.append(f"+{len(items) - 3} more")
        return " &middot; ".join(text) if text else "&mdash;"

    attention = (
        (len(photo), "Needs photo", details(photo, lambda row: _esc(f"{row.location} {row.make} - MVA {row.mva}"))),
        (len(stale), f"Avis units {STALE_DAYS}+ days old", details(stale, lambda row: _esc(f"MVA {row.mva} ({row.age}d)"))),
        (len(missing), "Claim # still missing", f"of {len(rows)} open units"),
        (len(fresh), "New today", f"Original date {today.month}/{today.day}"),
    )
    parts = [
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Glass Damage Report</title>',
        (
            "<style>*{box-sizing:border-box}body{margin:0;background:#ddd;overflow-x:hidden}"
            ".toolbar{padding:12px;text-align:center;background:#fff;position:sticky;top:0;z-index:2}"
            ".toolbar button{min-height:38px;padding:9px 18px;border:0;background:#111;color:#fff;font:600 14px 'Segoe UI',sans-serif;cursor:pointer}"
            ".page{padding:18px 12px 40px}.report-stage{position:relative;margin:0 auto}.report-canvas{position:absolute;top:0;left:0;width:1100px;transform-origin:top left}.mobile-report{display:none}"
            "@media(max-width:600px){body{background:#f1f1f1}.toolbar{padding:8px}.toolbar button{width:100%;max-width:280px}.page{padding:10px 8px 24px}"
            ".report-stage{position:absolute!important;left:-10000px!important;top:0}.mobile-report{display:block;max-width:560px;margin:0 auto;font-family:'Segoe UI',Calibri,Arial,sans-serif;color:#111}"
            ".mobile-hero{background:#d4002a;color:#fff;padding:20px 18px}.mobile-kicker{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase}.mobile-hero h1{margin:4px 0 2px;font-size:25px;line-height:1.05;text-transform:uppercase}.mobile-hero p{margin:0;font-size:13px;color:#ffe3e8}.mobile-date{margin-top:12px;font-size:12px;font-weight:700;text-transform:uppercase}.mobile-summary{background:#111;color:#fff;padding:11px 16px;font-size:14px}"
            ".mobile-attention{padding:16px;background:#fff;border-top:3px solid #d4002a}.mobile-attention h2{margin:0 0 12px;font-size:15px;text-transform:uppercase}.mobile-metrics{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#ddd}.mobile-metric{min-width:0;background:#fff;padding:12px}.mobile-metric strong{display:block;font-size:25px;line-height:1;color:#d4002a}.mobile-metric span{display:block;margin-top:4px;font-size:12px;font-weight:700}.mobile-metric small{display:block;margin-top:3px;color:#555;line-height:1.3}"
            ".mobile-section{margin-top:14px}.mobile-section-head{display:flex;justify-content:space-between;gap:12px;background:#111;border-bottom:3px solid #d4002a;color:#fff;padding:11px 12px}.mobile-section-head h2{margin:0;font-size:15px;text-transform:uppercase}.mobile-section-head span{font-size:12px;white-space:nowrap}.mobile-unit{background:#fff;border-bottom:1px solid #ddd;padding:13px 12px}.mobile-unit-title{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}.mobile-unit-title strong{font-family:Consolas,monospace;font-size:14px}.mobile-age{color:#555;font-size:12px}.mobile-age-stale{color:#d4002a;font-weight:800}.mobile-fields{display:grid;grid-template-columns:1fr 1fr;gap:9px 14px}.mobile-field{min-width:0}.mobile-field span{display:block;color:#666;font-size:10px;font-weight:700;text-transform:uppercase}.mobile-field b{display:block;margin-top:2px;font-size:13px;font-weight:600;overflow-wrap:anywhere}.mobile-vin{grid-column:1/-1}.mobile-vin b{font-family:Consolas,monospace}.mobile-footer{padding:18px 4px;color:#555;font-size:12px}}"
            "@media print{.toolbar,.mobile-report{display:none}.page{padding:0}.report-stage{position:relative!important;left:auto!important;width:1100px!important;height:auto!important}.report-canvas{position:static;transform:none!important}body{background:#fff;overflow:visible}}</style></head><body>"
        ),
        '<div class="toolbar"><button onclick="copyReport()">Copy report for Outlook</button></div><div class="page"><div class="report-stage" id="reportStage"><div class="report-canvas" id="reportCanvas">',
        f'<table id="report" role="presentation" width="1100" align="center" cellpadding="0" cellspacing="0" border="0" bgcolor="#f4f4f4" style="width:1100px;background:#f4f4f4;font-family:{FONT};color:{BLACK};border-collapse:collapse;">',
        f'<tr><td bgcolor="{RED}" style="background:{RED};padding:26px 32px 22px;color:#fff;"><table role="presentation" width="100%"><tr><td valign="bottom"><div style="font-size:12px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:#ffe3e8;">Supply Chain Damage and Maintenance &middot; Atlanta</div><div style="font-size:34px;font-weight:800;text-transform:uppercase;line-height:40px;">Glass Damage Report</div><div style="font-size:15px;color:#ffe3e8;">Bobby Brown, APO &amp; Local Market</div></td><td valign="bottom" align="right"><div style="font-size:12px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:#ffe3e8;">{today.strftime("%A")}</div><div style="font-size:30px;font-weight:800;text-transform:uppercase;line-height:36px;">{today.strftime("%b %d, %Y")}</div></td></tr></table></td></tr>',
        f'<tr><td bgcolor="{BLACK}" style="background:{BLACK};padding:10px 32px;font-size:14px;color:#fff;"><b style="font-size:17px;">{len(rows)}</b>&nbsp; units open',
    ]
    for section in sections:
        parts.append(f'&nbsp;&nbsp; | &nbsp;&nbsp;<b style="font-size:17px;">{len(section.rows)}</b>&nbsp; {_esc(section.title)}')
    parts.append('</td></tr><tr><td height="22">&nbsp;</td></tr>')
    parts.append(f'<tr><td bgcolor="#fff" style="background:#fff;border-top:4px solid {RED};padding:16px 20px 18px;"><div style="font-size:16px;font-weight:800;text-transform:uppercase;padding-bottom:10px;">Needs attention today</div><table role="presentation" width="100%"><tr>')
    for index, (count, title, detail) in enumerate(attention):
        border = "border-right:1px solid #e0e0e0;" if index < 3 else ""
        color = RED if count and index < 3 else BLACK
        parts.append(f'<td width="25%" valign="top" style="{border}padding:0 16px;"><div style="font-size:32px;font-weight:800;line-height:36px;color:{color};">{count}</div><div style="font-size:14px;font-weight:700;">{_esc(title)}</div><div style="font-size:12px;color:#555;">{detail}</div></td>')
    parts.append("</tr></table></td></tr>")
    for section in sections:
        parts.append('<tr><td height="22">&nbsp;</td></tr><tr><td bgcolor="#fff"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;"><colgroup>')
        for _, width in REPORT_COLUMNS:
            parts.append(f'<col width="{width}" style="width:{width}px;">')
        parts.append("</colgroup>")
        parts.append(f'<tr><td colspan="7" bgcolor="{BLACK}" style="background:{BLACK};border-bottom:3px solid {RED};padding:11px 14px;font-size:18px;font-weight:800;text-transform:uppercase;color:#ffffff;">{_esc(section.title)}</td><td colspan="5" align="right" bgcolor="{BLACK}" style="background:{BLACK};border-bottom:3px solid {RED};padding:11px 14px;font-size:13px;color:#ffffff;"><b style="color:#ff8095;">{section.missing}</b> missing&nbsp;&nbsp; <b style="color:#ffffff;">{section.listed}</b> listed&nbsp;&nbsp; <b style="color:#ffffff;">{len(section.rows)}</b> total</td></tr><tr>')
        for label, width in REPORT_COLUMNS:
            parts.append(f'<td width="{width}" bgcolor="#eee" style="width:{width}px;background:#eee;padding:7px 5px;font-size:11px;font-weight:700;color:#444;text-transform:uppercase;white-space:nowrap;overflow:hidden;">{label}</td>')
        parts.append("</tr>" + "".join(_report_row_html(row) for row in section.rows) + "</table></td></tr>")
    parts.extend((
        '<tr><td height="20">&nbsp;</td></tr><tr><td style="padding:0 4px 24px;"><table role="presentation" width="100%"><tr><td style="font-size:13px;color:#555;"><b style="color:#111;font-size:14px;">Dirk Steele</b><br>Supply Chain Damage and Maintenance Manager &middot; Atlanta, GA</td>',
        f'<td align="right" style="font-size:12px;color:#555;">Age = days since original date &middot; red at {STALE_DAYS}+</td></tr></table></td></tr></table></div></div>',
        f'<div class="mobile-report"><header class="mobile-hero"><div class="mobile-kicker">Supply Chain Damage and Maintenance &middot; Atlanta</div><h1>Glass Damage Report</h1><p>Bobby Brown, APO &amp; Local Market</p><div class="mobile-date">{today.strftime("%A, %b %d, %Y")}</div></header><div class="mobile-summary"><b>{len(rows)}</b> units open</div><section class="mobile-attention"><h2>Needs attention today</h2><div class="mobile-metrics">',
    ))
    for count, title, detail in attention:
        parts.append(f'<div class="mobile-metric"><strong>{count}</strong><span>{_esc(title)}</span><small>{detail}</small></div>')
    parts.append("</div></section>")
    for section in sections:
        parts.append(f'<section class="mobile-section"><div class="mobile-section-head"><h2>{_esc(section.title)}</h2><span>{len(section.rows)} total</span></div>')
        parts.extend(_mobile_row_html(row) for row in section.rows)
        parts.append("</section>")
    parts.extend((
        '<footer class="mobile-footer"><b>Dirk Steele</b><br>Supply Chain Damage and Maintenance Manager &middot; Atlanta, GA</footer></div></div>',
        "<script>function fitReport(){const stage=document.getElementById('reportStage');const canvas=document.getElementById('reportCanvas');const report=document.getElementById('report');const available=Math.max(280,document.documentElement.clientWidth-24);const scale=Math.min(1,available/1100);canvas.style.transform='scale('+scale+')';stage.style.width=(1100*scale)+'px';stage.style.height=(report.offsetHeight*scale)+'px'}function copyReport(){const r=document.createRange();r.selectNode(document.getElementById('report'));const s=getSelection();s.removeAllRanges();s.addRange(r);const ok=document.execCommand('copy');s.removeAllRanges();if(!ok)alert('Copy failed. Select the report and copy it manually.')}addEventListener('resize',fitReport);addEventListener('load',fitReport);</script></body></html>",
    ))
    return "".join(parts)


def _load_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for path in CONFIG_PATHS:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to load report configuration {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Report configuration must be a JSON object: {path}")
        config.update(value)
    return config


def read_sheet_rows(config: dict[str, Any]) -> list[list[str]]:
    spreadsheet_id = os.getenv("GLASS_SPREADSHEET_ID", str(config.get("spreadsheet_id", ""))).strip()
    if not spreadsheet_id or spreadsheet_id == "YOUR_SPREADSHEET_ID_HERE":
        raise RuntimeError("GLASS_SPREADSHEET_ID or spreadsheet_id must identify the report workbook")
    sheet_name = str(config.get("sheet_name", "GlassClaims")).strip()
    service_path = Path(str(config.get("service_account_json", "Service_account.json")))
    service_path = service_path if service_path.is_absolute() else ROOT / service_path
    if not service_path.exists():
        raise RuntimeError(f"Google service account file not found: {service_path}")
    client = gspread.service_account(filename=str(service_path))
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(sheet_name)
    return worksheet.get_all_values()


def _default_output_path(today: date) -> Path:
    return REPORTS_DIR / f"glass_morning_report_{today:%Y-%m-%d}.html"


def generate_report(*, output_path: Path | None = None, open_browser: bool = True) -> Path:
    today = date.today()
    rows = build_rows(read_sheet_rows(_load_config()), today=today)
    if not rows:
        raise RuntimeError("No GlassClaims rows have an Inventory Date equal to today")
    output_path = output_path or _default_output_path(today)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(rows, today=today), encoding="utf-8")
    if open_browser:
        webbrowser.open(output_path.resolve().as_uri())
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        output = generate_report(output_path=args.output, open_browser=not args.no_open)
    except Exception as exc:
        print(f"[ERROR] Glass morning report failed: {exc}", file=sys.stderr)
        return 1
    print(f"Glass morning report created: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())