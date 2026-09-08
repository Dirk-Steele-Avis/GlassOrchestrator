# core/eligibility.py
# Shared eligibility helpers used by Phase 6 (notification) and Phase 7 (work item creation).

# ----------------------------------------------------------------------------
# AUTHOR:       Dirk Steele <dirk.avis@gmail.com>
# DATE:         2026-04-11
# DESCRIPTION:  Determines whether a pipeline row is eligible for notification
#               and work item creation. Replacement items are eligible; Repair
#               items are not. Supports both internal ('damage_type') and sheet
#               ('Damage Type') key formats. Missing or empty value defaults to
#               eligible (Replacement assumption).
# VERSION:      1.0.0
# NOTES:        Used by Phase 6 notify filter and Phase 7 work item creation.
# ----------------------------------------------------------------------------


def normalize_damage_type(row: dict) -> str:
    """Normalize internal and sheet Action values to a damage type."""
    if "Action" in row:
        raw = row["Action"]
    elif "Damage Type" in row:
        raw = row["Damage Type"]
    else:
        raw = row.get("damage_type")
    if not raw:
        return "Replacement"

    normalized = str(raw).strip().upper()
    if normalized in {"REPLACEMENT", "REPLACE(AGN)", "REPLACE(AVIS)"}:
        return "Replacement"
    if normalized in {"REPAIR", "REPAIR(SUPERGLASS)"}:
        return "Repair"
    return str(raw).strip()


def is_notification_eligible(row: dict) -> bool:
    """
    Return True if this row should trigger notification and work item creation.
    Replacement items are eligible; Repair items are not.
    Supports both 'damage_type' (internal), 'Action' (sheet) and legacy
    'Damage Type' key formats. Missing or empty value defaults to eligible.
    """
    return normalize_damage_type(row) == "Replacement"
