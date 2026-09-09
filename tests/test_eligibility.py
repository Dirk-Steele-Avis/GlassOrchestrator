"""
Unit Tests for is_notification_eligible() — Phase 7, core/eligibility.py.

ELIG-1: Replacement rows are eligible
ELIG-2: Repair rows are not eligible
ELIG-3: Case-insensitive matching
ELIG-4: Missing damage_type defaults to eligible (Replacement)
ELIG-5: Sheet-style key "Damage Type" support
"""

import pytest
from core.eligibility import is_notification_eligible


# ─── ELIG-1/2: Basic Replacement / Repair eligibility ────────────────────────


class TestElig1_ReplacementEligible:
    """Replacement damage type rows are notification-eligible."""

    def test_replacement_is_eligible(self):
        assert is_notification_eligible({"damage_type": "Replacement"}) is True

    def test_replacement_uppercase_is_eligible(self):
        assert is_notification_eligible({"damage_type": "REPLACEMENT"}) is True

    def test_replacement_mixed_case_is_eligible(self):
        assert is_notification_eligible({"damage_type": "rEpLaCeMeNt"}) is True

    def test_turnback_is_eligible(self):
        assert is_notification_eligible({"damage_type": "Turnback"}) is True

    def test_tbk_code_is_eligible(self):
        assert is_notification_eligible({"damage_type": "TBK"}) is True


class TestElig2_RepairNotEligible:
    """Repair damage type rows are NOT notification-eligible."""

    def test_repair_is_not_eligible(self):
        assert is_notification_eligible({"damage_type": "Repair"}) is False

    def test_repair_lowercase_is_not_eligible(self):
        assert is_notification_eligible({"damage_type": "repair"}) is False

    def test_repair_uppercase_is_not_eligible(self):
        assert is_notification_eligible({"damage_type": "REPAIR"}) is False


# ─── ELIG-3: Case-insensitive matching (explicit parametrize) ─────────────────


class TestElig3_CaseInsensitive:
    """Eligibility check is case-insensitive for all inputs."""

    @pytest.mark.parametrize(
        "value",
        ["Replacement", "replacement", "REPLACEMENT", "Replacement ", "Turnback", "TBK"],
    )
    def test_replacement_variants_are_eligible(self, value):
        # Pass raw value — the SUT is responsible for stripping whitespace
        assert is_notification_eligible({"damage_type": value}) is True

    @pytest.mark.parametrize(
        "value",
        ["Repair", "repair", "REPAIR"],
    )
    def test_repair_variants_are_not_eligible(self, value):
        assert is_notification_eligible({"damage_type": value}) is False


# ─── ELIG-4: Missing key defaults to eligible ─────────────────────────────────


class TestElig4_MissingDamageType:
    """When damage_type is absent the function defaults to eligible (Replacement)."""

    def test_empty_dict_is_eligible(self):
        assert is_notification_eligible({}) is True

    def test_none_value_is_eligible(self):
        assert is_notification_eligible({"damage_type": None}) is True

    def test_empty_string_is_eligible(self):
        assert is_notification_eligible({"damage_type": ""}) is True


# ─── ELIG-5: Sheet-style key "Damage Type" ────────────────────────────────────


class TestElig5_SheetStyleKey:
    """Row dicts may use the Google Sheet column name 'Damage Type' (title case
    with space) instead of the snake_case 'damage_type' pipeline key.
    Both forms must be accepted."""

    def test_sheet_key_replacement_is_eligible(self):
        assert is_notification_eligible({"Damage Type": "Replacement"}) is True

    def test_sheet_key_repair_is_not_eligible(self):
        assert is_notification_eligible({"Damage Type": "Repair"}) is False

    def test_sheet_key_uppercase_replacement_is_eligible(self):
        assert is_notification_eligible({"Damage Type": "REPLACEMENT"}) is True

    def test_sheet_key_missing_falls_back_to_eligible(self):
        """Neither key present → defaults to eligible."""
        assert is_notification_eligible({"MVA": "12345678"}) is True

    def test_sheet_style_key_takes_precedence_over_snake_case(self):
        """'Damage Type' key takes precedence over 'damage_type' when both are present."""
        row = {"Damage Type": "Repair", "damage_type": "Replacement"}
        assert is_notification_eligible(row) is False  # Repair wins because "Damage Type" key takes precedence

    @pytest.mark.parametrize("value", ["Replace(AGN)", "Replace(AVIS)"])
    def test_sheet_vendor_replacement_labels_are_eligible(self, value):
        assert is_notification_eligible({"Action": value}) is True

    def test_sheet_action_tbk_is_eligible(self):
        assert is_notification_eligible({"Action": "TBK"}) is True

    def test_sheet_vendor_repair_label_is_not_eligible(self):
        assert is_notification_eligible({"Action": "Repair(SuperGlass)"}) is False
