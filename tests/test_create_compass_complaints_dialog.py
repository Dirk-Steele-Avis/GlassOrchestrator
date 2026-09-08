from unittest.mock import MagicMock, patch

from create_compass_complaints import (
    _complaint_fields_for_area,
    _set_glass_damage_category,
    _set_glass_damage_subcategory,
    _wait_for_new_glass_complaint,
)


def _form_scope():
    scope = MagicMock()
    regions = {}
    radios = {}

    for heading_name in ("Category", "Sub-Category"):
        heading = MagicMock()
        region = MagicMock()
        label = MagicMock()
        radio = MagicMock()
        radio.is_checked.side_effect = [False, True]

        heading_query = MagicMock()
        heading_query.first = heading
        heading.locator.return_value = region

        label_query = MagicMock()
        label_query.first = label
        radio_query = MagicMock()
        radio_query.first = radio

        def locate(selector, *, has_text=None, label_query=label_query, radio_query=radio_query):
            if selector == "label":
                return label_query
            if selector == "input[type='radio'][value='Glass Damage']":
                return radio_query
            raise AssertionError(f"Unexpected selector: {selector}")

        region.locator.side_effect = locate
        regions[heading_name] = (heading_query, region)
        radios[heading_name] = radio

    def get_by_role(role, *, name):
        assert role == "heading"
        for heading_name, (heading_query, _) in regions.items():
            if name.fullmatch(heading_name):
                return heading_query
        raise AssertionError(f"Unexpected heading pattern: {name.pattern}")

    scope.get_by_role.side_effect = get_by_role
    return scope, regions, radios


def test_category_and_subcategory_select_distinct_glass_damage_radios():
    scope, regions, radios = _form_scope()

    _set_glass_damage_category(scope)
    _set_glass_damage_subcategory(scope)

    radios["Category"].check.assert_called_once_with(force=True)
    radios["Sub-Category"].check.assert_called_once_with(force=True)
    for _, region in regions.values():
        region.locator.assert_any_call("input[type='radio'][value='Glass Damage']")


def test_rvm_uses_mechanical_issue_subcategory_and_rvm_description():
    assert _complaint_fields_for_area("Rear View Mirror") == ("Mechanical Issue", "RVM")
    assert _complaint_fields_for_area("RVM") == ("Mechanical Issue", "RVM")


def test_wait_for_new_glass_complaint_requires_count_increase():
    page = MagicMock()

    with patch(
        "create_compass_complaints._glass_damage_complaint_count",
        side_effect=[0, 1],
    ):
        assert _wait_for_new_glass_complaint(page, baseline_count=0, timeout_ms=1500)

    page.wait_for_timeout.assert_called_once_with(500)


def test_wait_for_new_glass_complaint_rejects_unchanged_count():
    page = MagicMock()

    with patch(
        "create_compass_complaints._glass_damage_complaint_count",
        return_value=1,
    ):
        assert not _wait_for_new_glass_complaint(page, baseline_count=1, timeout_ms=1000)

    assert page.wait_for_timeout.call_count == 2