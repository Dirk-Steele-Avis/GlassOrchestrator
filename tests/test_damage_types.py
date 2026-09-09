from core.damage_types import to_sheet_action_label


def test_sheet_action_uses_catalog_default_label_when_no_runtime_override():
    assert to_sheet_action_label("Replacement", vendor_labels={}) == "Replace(AGN)"


def test_sheet_action_uses_runtime_turnback_label_when_avis_forced():
    labels = {"Turnback": "Replace(AVIS-CUSTOM)", "Replacement": "Replace(AGN-CUSTOM)"}
    assert to_sheet_action_label("Replacement", vendor_labels=labels, force_avis_order=True) == "Replace(AVIS-CUSTOM)"


def test_turnback_rule_forces_avis_label_without_explicit_force_flag():
    labels = {"Turnback": "Replace(AVIS-CUSTOM)"}
    assert to_sheet_action_label("Turnback", vendor_labels=labels, force_avis_order=False) == "Replace(AVIS-CUSTOM)"
