"""Append-only writer for the GlassResults.txt contract.

Format: comma-separated `MVA,VIN,Desc`, no header, one row per MVA.
Missing values are coerced to 'N/A' to satisfy the orchestrator merge step.
"""
from pathlib import Path

from .records import VehicleRecord

MISSING = "N/A"


class ResultsWriter:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def reset(self) -> None:
        """Truncate the output file (call once at run start)."""
        self._path.write_text("", encoding="utf-8")

    def append(self, record: VehicleRecord) -> None:
        row = f"{self._coerce(record.mva)},{self._coerce(record.vin)},{self._coerce(record.desc)}\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(row)
            fh.flush()

    @staticmethod
    def _coerce(value: str | None) -> str:
        if value is None:
            return MISSING
        stripped = value.strip()
        return stripped if stripped else MISSING
