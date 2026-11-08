"""The pricing engine: a structured draft and a company package in, every number on the PCO out.

Order: price each line from its evidence, decide general conditions, subtotal, markup component by
component, pass-through after markup, the repeat-client discount last, then the total. Code does all
the arithmetic; the renderer only displays the result. Flags never block.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from .flags import Flag
from .general_conditions import general_conditions
from .lines import price_line
from .markup import Component, apply_markup, markup_structure, pass_through, subtotal
from .money import ZERO, cents, number
from .evidence import company_evidence

TAX = re.compile(r"\btax(es)?\b", re.I)
DISCOUNT = "REPEAT-CLIENT-DISCOUNT"


@dataclass
class Priced:
    lines: list                      # PricedLine per draft cost line, in draft order
    subtotal: Decimal
    components: list
    markup: Decimal
    pass_through: Decimal
    total: Decimal
    complete: bool
    contract_reference: str | None
    pass_through_categories: list
    discount: Component | None = None
    flags: list = field(default_factory=list)

    @property
    def amounts(self):
        """Amounts of the lines in the total; None where a quantity or price is unknown."""
        return [line.amount for line in self.lines if line.included]


def total(subtotal_amount, markup, discount, passed):
    return subtotal_amount + markup - (discount.amount if discount else ZERO) + passed


def check_tax(draft, values):
    if values.get("TAX-TREATMENT") != "included_in_material_estimates":
        return []
    return [Flag("tax_line", f"{line['description']}: the company carries tax in material estimates, so there "
                             f"should be no separate tax line.", index)
            for index, line in enumerate(draft["cost_lines"]) if TAX.search(line["description"])]


def repeat_client_discount(draft, values, base):
    """The last step, only when the company enables it and the client is a repeat client. Never on pass-through."""
    setting = values.get(DISCOUNT) or {}
    if not setting.get("enabled"):
        return None, []
    rate = number(setting.get("rate"))
    if rate is None:
        return None, [Flag("setting_missing", f"{DISCOUNT} is enabled with no rate, so no discount is applied.")]
    repeat = draft.get("repeat_client") or {}
    if repeat.get("status") != "known":
        return None, [Flag("repeat_client_unknown", "Is this a repeat client? The company discounts repeat "
                                                    "clients' change orders; no discount applied until confirmed.")]
    if not repeat["value"]:
        return None, []
    return Component("Repeat-client discount", rate, cents(base * rate / 100), True, company_evidence(DISCOUNT)), []


def price(draft, package, use_contract=True):
    """Every number on the PCO, the evidence behind each line, and the flags."""
    values = package.values
    structure, pass_categories, reference, markup_evidence, flags = markup_structure(draft, values, use_contract)
    gc_lines, gc_flags = general_conditions(draft, package)
    lines = []
    for index, line in enumerate(draft["cost_lines"]):
        if index in gc_lines:
            lines.append(gc_lines[index])
            continue
        result, line_flags = price_line(line, index, package, draft)
        lines.append(result)
        flags += line_flags
    flags += gc_flags + check_tax(draft, values)
    direct = subtotal(lines, pass_categories)
    components = apply_markup(lines, structure, pass_categories, markup_evidence)
    markup = sum((c.amount for c in components), ZERO)
    discount, discount_flags = repeat_client_discount(draft, values, direct + markup)
    flags += discount_flags
    passed = pass_through(lines, pass_categories)
    complete = all(line.amount is not None for line in lines if line.included)
    return Priced(lines, direct, components, markup, passed, total(direct, markup, discount, passed), complete,
                  reference, pass_categories, discount, sorted(flags, key=lambda f: (f.line is None, f.line or 0)))
