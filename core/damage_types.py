"""Central damage-type catalog utilities loaded from data/damage_types.csv."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class DamageTypeRule:
    code: str
    canonical_action: str
    sheet_action_label: str
    is_replacement_eligible: bool
    forces_avis_order: bool
    aliases: tuple[str, ...]


def _to_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def _default_catalog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "damage_types.csv"


def _fallback_rules() -> dict[str, DamageTypeRule]:
    rules = [
        DamageTypeRule(
            code="REPLACEMENT",
            canonical_action="Replacement",
            sheet_action_label="Replace(AGN)",
            is_replacement_eligible=True,
            forces_avis_order=False,
            aliases=("REPLACEMENT", "REPLACE", "REPLACE(AGN)", "REPLACE(AVIS)", "CRACK"),
        ),
        DamageTypeRule(
            code="REPAIR",
            canonical_action="Repair",
            sheet_action_label="Repair(SuperGlass)",
            is_replacement_eligible=False,
            forces_avis_order=False,
            aliases=("REPAIR", "REPAIR(SUPERGLASS)", "CHIP"),
        ),
        DamageTypeRule(
            code="WARRANTY",
            canonical_action="Warranty",
            sheet_action_label="Warranty",
            is_replacement_eligible=False,
            forces_avis_order=False,
            aliases=("WARRANTY", "WAR"),
        ),
        DamageTypeRule(
            code="TURNBACK",
            canonical_action="Turnback",
            sheet_action_label="Replace(AVIS)",
            is_replacement_eligible=True,
            forces_avis_order=True,
            aliases=("TURNBACK", "TBK"),
        ),
    ]
    return {rule.code: rule for rule in rules}


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, DamageTypeRule]:
    path = _default_catalog_path()
    if not path.exists():
        return _fallback_rules()

    rules: dict[str, DamageTypeRule] = {}
    with path.open("r", encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            code = str(row.get("code") or "").strip().upper()
            if not code:
                continue

            aliases_raw = str(row.get("aliases") or "")
            aliases = tuple(
                token.strip().upper() for token in aliases_raw.split("|") if token.strip()
            )
            rules[code] = DamageTypeRule(
                code=code,
                canonical_action=str(row.get("canonical_action") or code.title()).strip(),
                sheet_action_label=str(row.get("sheet_action_label") or "").strip(),
                is_replacement_eligible=_to_bool(str(row.get("is_replacement_eligible") or "")),
                forces_avis_order=_to_bool(str(row.get("forces_avis_order") or "")),
                aliases=aliases,
            )

    return rules or _fallback_rules()


def get_catalog() -> dict[str, DamageTypeRule]:
    """Return loaded catalog keyed by damage type code."""
    return _load_catalog().copy()


def resolve_damage_type(raw_value: str | None) -> DamageTypeRule | None:
    """Resolve free-form labels/codes to a configured damage type rule."""
    if raw_value is None:
        return None

    candidate = str(raw_value).strip().upper()
    if not candidate:
        return None

    catalog = _load_catalog()

    if candidate in catalog:
        return catalog[candidate]

    for rule in catalog.values():
        if candidate in rule.aliases:
            return rule

    return None


def normalize_action_label(raw_value: str | None, default: str = "Replacement") -> str:
    """Normalize an action label using catalog aliases and codes."""
    rule = resolve_damage_type(raw_value)
    if rule is None:
        return default if raw_value is None or str(raw_value).strip() == "" else str(raw_value).strip()
    return rule.canonical_action


def is_replacement_eligible(raw_value: str | None, default_if_missing: bool = True) -> bool:
    """Return eligibility using the catalog when possible."""
    rule = resolve_damage_type(raw_value)
    if rule is None:
        return default_if_missing if raw_value is None or str(raw_value).strip() == "" else False
    return rule.is_replacement_eligible


def to_sheet_action_label(
    raw_value: str | None,
    vendor_labels: Mapping[str, str] | None = None,
    force_avis_order: bool = False,
) -> str:
    """Resolve a sheet Action label using catalog semantics and runtime overrides.

    Precedence:
    1) When AVIS ordering is forced (or the rule itself forces AVIS), use the
       Turnback catalog/runtime label.
    2) Use runtime vendor_labels override for the canonical action when present.
    3) Fall back to the catalog sheet_action_label.
    4) Fall back to canonical action.
    """
    labels = vendor_labels or {}
    rule = resolve_damage_type(raw_value)
    if rule is None:
        return "" if raw_value is None else str(raw_value).strip()

    if force_avis_order or rule.forces_avis_order:
        turnback_rule = resolve_damage_type("TURNBACK")
        if turnback_rule is not None:
            return labels.get(turnback_rule.canonical_action, turnback_rule.sheet_action_label)

    if rule.canonical_action in labels:
        return labels[rule.canonical_action]
    if rule.sheet_action_label:
        return rule.sheet_action_label
    return rule.canonical_action
