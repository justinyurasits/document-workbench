"""Prices one cost line from its evidence. Never invents hours, rates or durations."""
from dataclasses import dataclass
from decimal import Decimal

from .evidence import find_evidence
from .flags import Flag
from .money import cents, known_number

DAYS_PER_WEEK = Decimal(7)


@dataclass
class PricedLine:
    index: int
    category: str
    description: str
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    evidence: object                # the Evidence the price used
    included: bool = True           # False when the line is left out of the total (see general conditions)


def amount_of(quantity, rate):
    return None if quantity is None or rate is None else cents(quantity * rate)


def price_line(line, index, package, draft, extension_days=None):
    """One line, priced from the evidence the interface finds for it. Returns the line and its flags.

    Labor priced as hours × rate and general conditions priced as weeks × rate take the company's table
    rate. Every other line (quotes, estimates, lump sums, fees) is carried as given, with its evidence.
    For general conditions, extension_days sets the quantity: the extension in weeks, rounded to two decimals,
    and the amount is those rounded weeks × the weekly rate, so the printed line multiplies out exactly.
    """
    candidates = find_evidence(line, package, draft)
    table, own = candidates[:-1], candidates[-1]
    quantity, given = known_number(line["quantity"]), known_number(line["unit_price"])
    name = line["description"]

    def priced(q, rate, evidence, amount=None):
        return PricedLine(index, line["category"], name, q, rate, amount if amount is not None else amount_of(q, rate),
                          evidence)

    if line["category"] == "general_conditions":
        return general_conditions_line(line, index, table, own, quantity, given, extension_days, priced)
    if line["category"] == "labor" and line["basis"] == "hours_x_rate":
        return from_table(table, own, quantity, given, index, name, "labor", priced)
    flags = []
    if line["category"] == "labor" and table and own.unit and given is not None:
        row = next((t for t in table if t.unit == own.unit), None)
        if row and row.rate != given:
            flags.append(Flag("labor_rate_differs",
                              f"{name}: the draft's {given:,.2f} per {own.unit} differs from the company rate "
                              f"{row.rate:,.2f} ({row.reference}). Carried the draft's {own.source} as given.", index))
    return priced(quantity, given, own), flags


def from_table(table, own, quantity, given, index, name, kind, priced):
    matching = [t for t in table if t.unit == own.unit]
    flags = []
    if matching:
        row = matching[0]
        if given is not None and given != row.rate:
            flags.append(Flag(f"{kind}_rate_differs",
                              f"{name}: the draft's rate {given:,.2f} differs from the company rate {row.rate:,.2f} "
                              f"({row.reference}). Used the company rate.", index))
        return priced(quantity, row.rate, row), flags
    if table:
        flags.append(Flag(f"{kind}_unit_mismatch",
                          f"{name}: the company rate is per {table[0].unit}, but the line is in "
                          f"{own.unit or 'an unknown unit'}. Not converted.", index))
    return carried_or_blank(own, quantity, given, index, name, priced, flags)


def carried_or_blank(own, quantity, given, index, name, priced, flags):
    """No usable table row: keep the draft's price only if its evidence says where it came from."""
    if given is not None and own.names_a_source:
        return priced(quantity, given, own), flags
    flags.append(Flag("unsupported_price",
                      f"{name}: no company rate matches and the price has no stated source. Left blank.", index))
    return priced(quantity, None, own), flags


def general_conditions_line(line, index, table, own, quantity, given, extension_days, priced):
    name = line["description"]
    weeks = cents(extension_days / DAYS_PER_WEEK)
    flags = []
    if quantity is not None and cents(quantity) != weeks:
        flags.append(Flag("gc_quantity_differs",
                          f"{name}: the draft says {quantity:g} weeks; the extension is {extension_days:g} calendar "
                          f"days, {weeks} weeks. Used the extension.", index))
    row = next((t for t in table if t.unit == "week"), None)
    if row is None:
        flags.append(Flag("gc_rate_not_found",
                          f"{name}: no weekly general conditions rate for role "
                          f"{line.get('role') or '(none given)'}.", index))
        if given is not None and own.names_a_source:
            return priced(weeks, given, own), flags
        flags.append(Flag("unsupported_price", f"{name}: the price has no stated source. Left blank.", index))
        return priced(weeks, None, own), flags
    if given is not None and given != row.rate:
        flags.append(Flag("gc_rate_differs",
                          f"{name}: the draft's weekly rate {given:,.2f} differs from the company rate "
                          f"{row.rate:,.2f} ({row.reference}). Used the company rate.", index))
    return priced(weeks, row.rate, row), flags
