"""Read-only search of outstanding purchase orders across an Outlook Inbox."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from openpyxl import load_workbook

from invoice_workflow.invoice_parser import parse_invoice_pdf

ROOT = Path(__file__).resolve().parents[1]
PO_PATTERN = re.compile(r"\bFPO\d{7,}\b", re.IGNORECASE)
VIN_PATTERN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
REQUIRED_COLUMNS = (
    "MVA_NUMBER", "VIN_NO", "PO_NUMBER", "PO_STATUS", "PO_DAYS_OPEN",
    "PO_ESTIMATE_AMOUNT", "SUPPLIER_NAME",
)
SENDER_NAME = "AGN GLASS LLC"
SENDER_ADDRESS = "noreply@omegaedi.com"
EVIDENCE_COLUMNS = (
    "PO_NUMBER", "VIN_NO", "MATCH_TYPE", "SOURCE", "FOLDER", "SUBJECT",
    "RECEIVED", "ENTRY_ID", "STORE_ID", "ATTACHMENT", "PDF_PO", "PDF_VIN",
    "PDF_AMOUNT", "DETAIL",
)
FAILURE_COLUMNS = (
    "FOLDER", "SUBJECT", "RECEIVED", "ENTRY_ID", "STORE_ID", "ATTACHMENT", "DETAIL",
)


@dataclass(frozen=True)
class SearchResult:
    evidence: tuple[dict[str, str], ...]
    folders: int
    messages: int
    selected_messages: int
    pdfs: int
    failures: tuple[dict[str, str], ...]


def parse_amount(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid PO_ESTIMATE_AMOUNT: {value!r}") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"PO_ESTIMATE_AMOUNT must be a nonnegative amount in cents: {value!r}")
    return amount


def load_open_pos(path: Path, *, older_than_days: int = 7) -> list[dict[str, str]]:
    """Validate the explicit export contract; never infer statuses or headers."""
    if older_than_days < 0:
        raise ValueError("older_than_days must be nonnegative")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["Open Purchase Orders"]
        values = iter(sheet.values)
        headers = tuple(str(value or "").strip() for value in next(values))
        if len(set(headers)) != len(headers) or not all(headers):
            raise ValueError("Export contains blank or duplicate column names")
        missing = set(REQUIRED_COLUMNS) - set(headers)
        if missing:
            raise ValueError(f"Export missing columns: {', '.join(sorted(missing))}")
        orders = []
        seen = set()
        for row_number, values_row in enumerate(values, start=2):
            if all(value is None for value in values_row):
                continue
            row = dict(zip(headers, (str(v).strip() if v is not None else "" for v in values_row)))
            if row["PO_STATUS"] != "OPEN":
                raise ValueError(f"Row {row_number}: expected PO_STATUS=OPEN")
            if not re.fullmatch(r"\d+", row["PO_DAYS_OPEN"]):
                raise ValueError(f"Row {row_number}: PO_DAYS_OPEN must be a nonnegative integer")
            po = row["PO_NUMBER"].upper()
            vin = row["VIN_NO"].upper()
            if PO_PATTERN.fullmatch(po) is None or VIN_PATTERN.fullmatch(vin) is None:
                raise ValueError(f"Row {row_number}: invalid PO number or VIN")
            if not row["MVA_NUMBER"].isdigit():
                raise ValueError(f"Row {row_number}: invalid MVA")
            parse_amount(row["PO_ESTIMATE_AMOUNT"])
            if not row["SUPPLIER_NAME"]:
                raise ValueError(f"Row {row_number}: missing SUPPLIER_NAME")
            if po in seen:
                raise ValueError(f"Row {row_number}: duplicate PO {po}")
            seen.add(po)
            row.update(PO_NUMBER=po, VIN_NO=vin)
            if int(row.pop("PO_DAYS_OPEN")) > older_than_days:
                orders.append(row)
        if not orders:
            raise ValueError(f"Export contains no open POs older than {older_than_days} days")
        return orders
    finally:
        workbook.close()


def get_inbox(namespace: Any, account_name: str) -> Any:
    accounts = [folder for folder in namespace.Folders if folder.Name == account_name]
    if len(accounts) != 1:
        raise ValueError(f"Expected exactly one Outlook mailbox named {account_name!r}")
    return accounts[0].Folders.Item("Inbox")


def scan_inbox(
    inbox: Any,
    orders: list[dict[str, str]],
    logger: logging.Logger,
) -> SearchResult:
    """Select AGN invoice emails, then compare PDF PO, VIN, and subtotal."""
    by_po = {row["PO_NUMBER"]: row for row in orders}
    by_vin: dict[str, list[str]] = {}
    for row in orders:
        by_vin.setdefault(row["VIN_NO"], []).append(row["PO_NUMBER"])
    evidence: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    folders = messages = selected_messages = pdfs = 0

    def report_failure(metadata: dict[str, str], exc: Exception) -> None:
        failure = {**metadata, "DETAIL": f"{type(exc).__name__}: {exc}"}
        failures.append(failure)
        logger.warning("Unresolved invoice: %s", failure)

    with TemporaryDirectory(prefix="open_po_analysis_") as temporary:
        stack = [inbox]
        while stack:
            folder = stack.pop()
            folder_path = str(folder.FolderPath)
            folders += 1
            logger.info("Scanning folder %s", folder_path)
            try:
                for message in folder.Items:
                    if message.Class != 43:
                        continue
                    subject = str(message.Subject or "")
                    messages += 1
                    if "invoice" not in subject.casefold():
                        continue
                    if (
                        str(message.SenderName or "").strip().casefold() != SENDER_NAME.casefold()
                        or str(message.SenderEmailAddress or "").strip().casefold() != SENDER_ADDRESS
                    ):
                        continue
                    selected_messages += 1
                    metadata = {
                        "FOLDER": folder_path, "SUBJECT": subject,
                        "RECEIVED": str(message.ReceivedTime), "ENTRY_ID": str(message.EntryID),
                        "STORE_ID": str(folder.StoreID), "ATTACHMENT": "",
                    }
                    try:
                        attachments = message.Attachments
                        selected_pdfs = []
                        for index in range(1, attachments.Count + 1):
                            attachment = attachments.Item(index)
                            name = str(attachment.FileName)
                            if Path(name).suffix.lower() == ".pdf":
                                selected_pdfs.append((attachment, name))
                        if not selected_pdfs:
                            raise ValueError("Selected AGN invoice email has no PDF attachment")
                    except Exception as exc:
                        report_failure(metadata, exc)
                        continue
                    for attachment, name in selected_pdfs:
                        pdfs += 1
                        attachment_metadata = {**metadata, "ATTACHMENT": name}
                        pdf_path = Path(temporary) / "invoice.pdf"
                        try:
                            attachment.SaveAsFile(str(pdf_path))
                            invoice = parse_invoice_pdf(pdf_path)
                        except Exception as exc:
                            report_failure(attachment_metadata, exc)
                            continue
                        finally:
                            pdf_path.unlink(missing_ok=True)
                        subject_pos = set(PO_PATTERN.findall(subject.upper()))
                        targets = (
                            ({invoice.po_number} | subject_pos) & by_po.keys()
                        ) | set(by_vin.get(invoice.vin, ()))
                        for po in sorted(targets):
                            order = by_po[po]
                            checks = {
                                "PO": invoice.po_number == po,
                                "VIN": invoice.vin == order["VIN_NO"],
                                "amount": invoice.amount == parse_amount(order["PO_ESTIMATE_AMOUNT"]),
                                "supplier": order["SUPPLIER_NAME"].casefold() == SENDER_NAME.casefold(),
                                "subject PO": not subject_pos or subject_pos == {invoice.po_number},
                            }
                            matched = all(checks.values())
                            evidence.append({
                                **attachment_metadata, "PO_NUMBER": po, "VIN_NO": order["VIN_NO"],
                                "MATCH_TYPE": "VERIFIED_MATCH" if matched else "MISMATCH",
                                "SOURCE": "pdf", "PDF_PO": invoice.po_number,
                                "PDF_VIN": invoice.vin, "PDF_AMOUNT": str(invoice.amount),
                                "DETAIL": "PO, VIN, and amount match" if matched else
                                "Mismatch: " + ", ".join(key for key, passed in checks.items() if not passed),
                            })
                stack.extend(folder.Folders)
            except Exception as exc:
                raise RuntimeError(f"Inbox scan failed in {folder_path!r}: {exc}") from exc
    return SearchResult(tuple(evidence), folders, messages, selected_messages, pdfs, tuple(failures))


def summarize(
    orders: list[dict[str, str]], result: SearchResult,
) -> list[dict[str, str]]:
    matches: dict[str, list[dict[str, str]]] = {}
    for item in result.evidence:
        matches.setdefault(item["PO_NUMBER"], []).append(item)
    rows = []
    for order in orders:
        hits = matches.get(order["PO_NUMBER"], [])
        candidates = [hit for hit in hits if hit["MATCH_TYPE"] == "VERIFIED_MATCH"]
        outside_scope = order["SUPPLIER_NAME"].casefold() != SENDER_NAME.casefold()
        rows.append({
            **order,
            "ANALYSIS_STATUS": (
                "Invoice found" if candidates else
                "inconclusive" if hits or result.failures or outside_scope else "Invoice not found"
            ),
            "ANALYSIS_REASON": (
                "PO, VIN, and amount match" if candidates else
                "Supplier outside AGN sender search scope" if outside_scope else
                "Related invoice has mismatched fields" if hits else
                "Selected invoices could not be read; search incomplete" if result.failures else
                "No matching invoice in selected AGN emails"
            ),
            "CANDIDATE_EMAIL_COUNT": str(len({
                (hit["STORE_ID"], hit["ENTRY_ID"]) for hit in candidates
            })),
            "RELATED_EMAIL_COUNT": str(len({
                (hit["STORE_ID"], hit["ENTRY_ID"]) for hit in hits
            })),
        })
    return rows


def write_csv(path: Path, rows: list[dict[str, str]], columns: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            # Email text is untrusted when opened in spreadsheet software.
            writer.writerow({
                key: "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
                for key, value in row.items()
            })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Open Purchase Orders XLSX export")
    parser.add_argument("--account", help="Exact Outlook mailbox name; otherwise configured account_name")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument(
        "--older-than-days", type=int, default=7,
        help="Only search POs older than this many days in the export (default: 7; exclusive)",
    )
    args = parser.parse_args(argv)
    if args.older_than_days < 0:
        parser.error("--older-than-days must be nonnegative")
    log_directory = ROOT / "log"
    log_directory.mkdir(exist_ok=True)
    logger = logging.getLogger("open_po_analysis")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_directory / "open_po_analysis.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    step = "validate export"
    try:
        logger.info("Session started %s", datetime.now().isoformat(timespec="seconds"))
        orders = load_open_pos(args.input, older_than_days=args.older_than_days)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        run_directory = args.output_dir / f"open_po_analysis_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_directory.mkdir()
        write_csv(run_directory / "outstanding_pos.csv", orders, tuple(orders[0]))
        for order in orders:
            logger.info("******** MVA %s | PO %s ********", order["MVA_NUMBER"], order["PO_NUMBER"])
        step = "connect to configured Outlook Inbox"
        account = args.account
        if account is None:
            config = json.loads((ROOT / "outlook" / "agn_invoices_config.json").read_text(encoding="utf-8"))
            account = config["account_name"]
        if not isinstance(account, str) or not account.strip():
            raise ValueError("account_name must be a nonempty string")
        import win32com.client

        inbox = get_inbox(win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI"), account)
        step = "search Inbox and all subfolders"
        result = scan_inbox(inbox, orders, logger)
        step = "write completed analysis"
        summary = summarize(orders, result)
        write_csv(run_directory / "analysis.csv", summary, tuple(summary[0]))
        write_csv(run_directory / "email_evidence.csv", list(result.evidence), EVIDENCE_COLUMNS)
        write_csv(run_directory / "unresolved_invoices.csv", list(result.failures), FAILURE_COLUMNS)
        manifest = {
            "completed_at": datetime.now().isoformat(), "input": str(args.input.resolve()),
            "account": account, "inbox": str(inbox.FolderPath), "orders": len(orders),
            "age_filter": f"PO_DAYS_OPEN > {args.older_than_days} in the supplied export",
            "folders": result.folders, "messages": result.messages,
            "selected_messages": result.selected_messages, "pdfs": result.pdfs,
            "unresolved_invoices": len(result.failures),
            "search_scope": f"Inbox and descendants; subject contains invoice; sender {SENDER_NAME} <{SENDER_ADDRESS}>; PDF PO/VIN/subtotal must match export",
            "limitations": "AGN sender only. No OCR. Unreadable selected invoices make otherwise unmatched POs inconclusive. Non-AGN suppliers are inconclusive.",
            "statuses": dict(Counter(row["ANALYSIS_STATUS"] for row in summary)),
        }
        (run_directory / "completed.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        logger.info("Completed: %s", manifest)
        print(json.dumps(manifest, indent=2))
        for status in ("Invoice found", "Invoice not found", "inconclusive"):
            grouped = [row for row in summary if row["ANALYSIS_STATUS"] == status]
            print(f"\n{status} ({len(grouped)})")
            print("PO_NUMBER | VIN_NO | PO_ESTIMATE_AMOUNT | REASON")
            for row in grouped:
                print(
                    f"{row['PO_NUMBER']} | {row['VIN_NO']} | "
                    f"{row['PO_ESTIMATE_AMOUNT']} | {row['ANALYSIS_REASON']}"
                )
        for failure in result.failures:
            print(
                f"WARNING - unresolved invoice: {failure['FOLDER']} | "
                f"{failure['SUBJECT']} | {failure['ATTACHMENT']} | {failure['DETAIL']}"
            )
        print(f"Reports: {run_directory}")
        return 0
    except Exception as exc:
        logger.exception("Failed step: %s: %s", step, exc)
        print(f"ERROR - {step}: {exc}")
        return 1
    finally:
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())
