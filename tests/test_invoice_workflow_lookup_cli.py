from datetime import UTC, datetime
from decimal import Decimal

import pytest

from invoice_workflow import lookup_cli
from invoice_workflow.contracts import ParsedInvoice
from invoice_workflow.identity_adapters import GoogleSheetIdentityReader
from invoice_workflow.store import InvoiceRepository
from vendor_tracking.sheet_updater import VendorSheetUpdater


def _repository(tmp_path):
    repository = InvoiceRepository(tmp_path / "invoices.db")
    repository.initialize()
    repository.upsert_invoice(ParsedInvoice(
        internet_message_id="message-1",
        entry_id="entry-1",
        invoice_number="5307104",
        subject="[External] Invoice #5307104 (PO # FPO1134141)",
        received_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        subject_po_number="FPO1134141",
        pdf_po_number="FPO1134141",
        vin="4T1DAACK0SU050843",
        invoice_amount=Decimal("340.00"),
        pdf_path="invoice.pdf",
    ))
    return repository


def test_main_finds_invoice_by_vin(monkeypatch, tmp_path, capsys):
    repository = _repository(tmp_path)
    monkeypatch.setattr(lookup_cli, "DATABASE_PATH", repository.database_path)

    assert lookup_cli.main(["--vins", "4T1DAACK0SU050843"]) == 0

    output = capsys.readouterr().out
    assert output.startswith("VIN 4T1DAACK0SU050843 | RECEIVED")
    assert output.count("4T1DAACK0SU050843") == 1
    assert "RECEIVED" in output
    assert "invoice=5307104" in output
    assert "po=FPO1134141" in output


def test_main_resolves_mva_to_vin(monkeypatch, tmp_path, capsys):
    repository = _repository(tmp_path)
    resolver = type(
        "Resolver",
        (),
        {"find_vins_by_mva": lambda self, _mva: ("4T1DAACK0SU050843",)},
    )()
    monkeypatch.setattr(lookup_cli, "DATABASE_PATH", repository.database_path)
    monkeypatch.setattr(lookup_cli, "_build_mva_resolver", lambda: resolver)

    assert lookup_cli.main(["--mvas", "55897402"]) == 0

    output = capsys.readouterr().out
    assert "MVA 055897402" in output
    assert "VIN 4T1DAACK0SU050843 | RECEIVED" in output


def test_main_reports_target_not_found(monkeypatch, tmp_path, capsys):
    repository = _repository(tmp_path)
    monkeypatch.setattr(lookup_cli, "DATABASE_PATH", repository.database_path)

    assert lookup_cli.main(["--vins", "1HGCY1F43TA029160"]) == 0

    assert "NOT FOUND IN INVOICE LEDGER" in capsys.readouterr().out


def test_received_only_suppresses_targets_without_invoice(monkeypatch, tmp_path, capsys):
    repository = _repository(tmp_path)
    monkeypatch.setattr(lookup_cli, "DATABASE_PATH", repository.database_path)

    assert lookup_cli.main([
        "--vins",
        "4T1DAACK0SU050843,1HGCY1F43TA029160",
        "--received-only",
    ]) == 0

    output = capsys.readouterr().out
    assert "VIN 4T1DAACK0SU050843 | RECEIVED" in output
    assert "1HGCY1F43TA010304" not in output
    assert "NOT FOUND" not in output


def test_parse_args_rejects_invalid_or_missing_targets():
    with pytest.raises(SystemExit):
        lookup_cli._parse_args(["--vins", "bad"])
    with pytest.raises(SystemExit):
        lookup_cli._parse_args([])


def test_sheet_updater_finds_exact_normalized_mva_rows():
    updater = VendorSheetUpdater.__new__(VendorSheetUpdater)
    updater._headers = ["MVA", "VIN"]
    updater._all_values = [
        updater._headers,
        ["55897402", "4T1DAACK0SU050843"],
        ["055897402", "4T1DAACK0SU050843"],
        ["55897403", "1HGCY1F43TA029160"],
    ]

    assert updater.find_rows_by_mva("055897402") == [2, 3]
    assert updater.find_rows_by_mva("invalid") == []


def test_identity_reader_deduplicates_vins_for_mva():
    updater = VendorSheetUpdater.__new__(VendorSheetUpdater)
    updater._headers = ["MVA", "VIN"]
    updater._all_values = [
        updater._headers,
        ["55897402", "4T1DAACK0SU050843"],
        ["055897402", "4T1DAACK0SU050843"],
    ]
    reader = GoogleSheetIdentityReader.__new__(GoogleSheetIdentityReader)
    reader._updater = updater

    assert reader.find_vins_by_mva("55897402") == ("4T1DAACK0SU050843",)