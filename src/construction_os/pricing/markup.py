"""Markup as ordered components by category, each applied once to its own base. Never compounded."""
from dataclasses import dataclass
from decimal import Decimal

from .evidence import company_evidence, contract_evidence
from .flags import Flag
from .money import ZERO, cents, number

PERMITS = "permits_and_fees"


@dataclass
class Component:
    name: str
    rate: Decimal
    amount: Decimal
    client_visible: bool = True
    evidence: object = None


def markup_structure(draft, values, use_contract=True):
    """The components and pass-through categories that govern this draft.

    Clear contract terms replace the company's markup structure for this project and are cited (SYS-00 4.3).
    Returns (components, pass_through_categories, contract_reference, evidence, flags).
    """
    terms = draft.get("contract_terms") or {}
    if use_contract and terms.get("status") == "known":
        structure = [{**m, "client_visible": True} for m in terms["markup"]]
        return structure, list(terms["pass_through"]), terms["reference"], contract_evidence(terms), []
    flags = []
    structure = values.get("MARKUP-STRUCTURE")
    if structure is None:
        flags.append(Flag("setting_missing", "MARKUP-STRUCTURE isn't set, so no markup is applied."))
    treatment = values.get("PERMIT-TREATMENT")
    if treatment is None:
        flags.append(Flag("setting_missing", "PERMIT-TREATMENT isn't set, so permits and fees are marked up."))
    passed = [PERMITS] if treatment == "pass_through_after_markup" else []
    return list(structure or []), passed, None, company_evidence("MARKUP-STRUCTURE"), flags


def marked(lines, pass_categories):
    return [l for l in lines if l.included and l.amount is not None and l.category not in pass_categories]


def subtotal(lines, pass_categories):
    """Direct cost of every included line that is marked up."""
    return sum((l.amount for l in marked(lines, pass_categories)), ZERO)


def apply_markup(lines, structure, pass_categories, evidence=None):
    """One component at a time, each over the subtotal of the categories it names ("all" is every marked one)."""
    base_lines = marked(lines, pass_categories)
    components = []
    for item in structure:
        scope = item["applies_to"]
        base = sum((l.amount for l in base_lines if "all" in scope or l.category in scope), ZERO)
        rate = number(item["rate"])
        components.append(Component(item["name"], rate, cents(base * rate / 100), item.get("client_visible", True),
                                    evidence))
    return components


def pass_through(lines, pass_categories):
    return sum((l.amount for l in lines if l.included and l.amount is not None and l.category in pass_categories), ZERO)
