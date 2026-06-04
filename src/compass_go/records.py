"""Records and outcome types for Compass GO scraping."""
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class VehicleRecord:
    """Single MVA scrape result. Missing fields stored as 'N/A'."""

    mva: str
    vin: str
    desc: str


class SearchOutcome(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"
