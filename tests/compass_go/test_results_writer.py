from pathlib import Path

from src.compass_go.records import VehicleRecord
from src.compass_go.writer import MISSING, ResultsWriter


def test_append_writes_csv_row(tmp_path: Path):
    out = tmp_path / "GlassResults.txt"
    writer = ResultsWriter(out)
    writer.reset()

    writer.append(VehicleRecord(mva="058883134", vin="5XYP64GC1SG682257", desc="KIA TEL2"))

    assert out.read_text(encoding="utf-8") == "058883134,5XYP64GC1SG682257,KIA TEL2\n"


def test_append_coerces_missing_fields_to_na(tmp_path: Path):
    out = tmp_path / "GlassResults.txt"
    writer = ResultsWriter(out)
    writer.reset()

    writer.append(VehicleRecord(mva="058883134", vin="", desc="   "))

    assert out.read_text(encoding="utf-8") == f"058883134,{MISSING},{MISSING}\n"


def test_append_is_appendish_not_truncating(tmp_path: Path):
    out = tmp_path / "GlassResults.txt"
    writer = ResultsWriter(out)
    writer.reset()

    writer.append(VehicleRecord(mva="111", vin="VIN1", desc="A"))
    writer.append(VehicleRecord(mva="222", vin="VIN2", desc="B"))

    assert out.read_text(encoding="utf-8") == "111,VIN1,A\n222,VIN2,B\n"


def test_reset_truncates_existing_file(tmp_path: Path):
    out = tmp_path / "GlassResults.txt"
    out.write_text("stale\n", encoding="utf-8")
    writer = ResultsWriter(out)

    writer.reset()

    assert out.read_text(encoding="utf-8") == ""


def test_writer_creates_parent_directory(tmp_path: Path):
    out = tmp_path / "nested" / "dir" / "GlassResults.txt"
    ResultsWriter(out)

    assert out.parent.exists()
