"""Analyze GlassClaims orders from the configured Google Sheet.

This script reads the service account and spreadsheet ID from
orchestrator_project.local.json and loads the GlassClaims worksheet
into pandas for summary reporting.

Usage:
    .venv\Scripts\python.exe analyze_glass_orders.py
    .venv\Scripts\python.exe analyze_glass_orders.py --export summary.csv
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import gspread
import pandas as pd

ROOT = Path(__file__).resolve().parent
LOCAL_CONFIG_PATH = ROOT / "orchestrator_project.local.json"
DEFAULT_SHEET_NAME = "GlassClaims"


def load_local_config(path: Path = LOCAL_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Local config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_worksheet(config: dict[str, Any], sheet_name: str = DEFAULT_SHEET_NAME):
    service_account_json = config.get("service_account_json")
    spreadsheet_id = config.get("spreadsheet_id")
    if not service_account_json:
        raise ValueError("service_account_json is missing from local config")
    if not spreadsheet_id:
        raise ValueError("spreadsheet_id is missing from local config")

    ws = gspread.service_account(filename=str(service_account_json))
    return ws.open_by_key(spreadsheet_id).worksheet(sheet_name)


def worksheet_to_dataframe(ws) -> pd.DataFrame:
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()
    header = values[0]
    data = values[1:]
    return pd.DataFrame(data, columns=header)


def normalize_dates(df: pd.DataFrame) -> pd.DataFrame:
    if "Inventory Date" in df.columns:
        df["Inventory Date"] = pd.to_datetime(df["Inventory Date"], errors="coerce")
    if "Original Date" in df.columns:
        df["Original Date"] = pd.to_datetime(df["Original Date"], errors="coerce")
    return df


def print_section(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def summarize(df: pd.DataFrame) -> None:
    print_section("GlassClaims Summary")
    print(f"Rows loaded: {len(df)}")
    print(f"Columns: {', '.join(df.columns.tolist())}")

    if df.empty:
        print("No data available.")
        return

    if "VIN" in df.columns:
        total = len(df)
        missing = (df["VIN"].fillna("N/A").astype(str) == "N/A").sum()
        print(f"Missing VIN count: {missing} ({missing / total:.1%})")

    for column in ["Action", "Area", "Location", "Claim#", "WorkItem"]:
        if column in df.columns:
            print_section(f"Counts by {column}")
            print(df[column].fillna("(blank)").value_counts(dropna=False).to_string())

    if "Inventory Date" in df.columns:
        print_section("Inventory Date Range")
        valid_dates = df["Inventory Date"].dropna()
        if not valid_dates.empty:
            print(f"Earliest: {valid_dates.min().date()}")
            print(f"Latest:   {valid_dates.max().date()}")
            print_section("Inventory Date by Month")
            print(valid_dates.dt.to_period("M").value_counts().sort_index().to_string())
        else:
            print("No valid Inventory Date values found.")

    if "MVA" in df.columns:
        print_section("Top MVAs with Missing VIN")
        missing_vins = df.loc[df["VIN"].fillna("N/A").astype(str) == "N/A"]
        if missing_vins.empty:
            print("None")
        else:
            print(missing_vins["MVA"].value_counts().head(20).to_string())

    print_section("Sample Rows")
    print(df.head(10).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze GlassClaims spreadsheet data.")
    parser.add_argument("--sheet", default=DEFAULT_SHEET_NAME, help="Worksheet name to load")
    parser.add_argument("--export", help="Optional CSV path to export the loaded sheet")
    args = parser.parse_args()

    config = load_local_config()
    worksheet = get_worksheet(config, sheet_name=args.sheet)
    df = worksheet_to_dataframe(worksheet)
    df = normalize_dates(df)
    summarize(df)

    if args.export:
        output_path = Path(args.export)
        df.to_csv(output_path, index=False)
        print(f"\nExported sheet data to: {output_path}")


if __name__ == "__main__":
    main()
