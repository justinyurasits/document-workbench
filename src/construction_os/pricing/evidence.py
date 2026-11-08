"""The pricing evidence interface: candidate rates and prices for a cost line, each with where it came from.

Version 1 reads the company's rate tables and the evidence the draft itself carries (a supplied quote, the
PM's notes, a historical record). Extracted history plugs in later through the same function.
"""
from dataclasses import dataclass
from decimal import Decimal

from .money import known_number, number

RATE_TABLES = {"labor": "LABOR-BILLING-RATES", "general_conditions": "GC-WEEKLY-RATES"}
UNITS = {"hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour", "day": "day", "days": "day",
         "wk": "week", "wks": "week", "week": "week", "weeks": "week"}
DRAFT_SOURCES = {"supplied_quote", "pm_notes", "historical_record", "estimate"}


@dataclass(frozen=True)
class Evidence:
    source: str                 # rate_table | supplied_quote | pm_notes | historical_record | estimate | contract
    reference: str | None
    basis: str
    date: str | None = None
    rate: Decimal | None = None
    unit: str | None = None     # hour, day or week, for a rate
    note: str | None = None     # the rate table row's own source, or the reason a date doesn't apply

    @property
    def names_a_source(self):
        """Says where the number came from: a draft source with a known reference, or a historical record."""
        if self.source == "historical_record":
            return True
        return self.source in DRAFT_SOURCES and bool(self.reference)


def unit_of(line):
    unit = line.get("unit") or {}
    return UNITS.get(str(unit.get("value", "")).strip().lower()) if unit.get("status") == "known" else None


def from_draft(line):
    """The evidence the draft's line already carries."""
    evidence = line["evidence"]
    reference, when = evidence.get("reference") or {}, evidence.get("date") or {}
    return Evidence(
        source=evidence["source"],
        reference=reference.get("value") if reference.get("status") == "known" else None,
        basis=line["basis"],
        date=when.get("value") if when.get("status") == "known" else None,
        rate=known_number(line["unit_price"]),
        unit=unit_of(line),
        note=when.get("reason"),
    )


def table_rows(line, package):
    """Rows of the company rate table for the line's category whose role matches exactly."""
    key = RATE_TABLES.get(line["category"])
    role = line.get("role")
    if key is None or not role:
        return []
    basis = "weeks_x_rate" if line["category"] == "general_conditions" else "hours_x_rate"
    return [Evidence("rate_table", f"{key}, {row['role']}", basis, None, number(row["rate"]), row.get("unit"),
                     row.get("source"))
            for row in package.values.get(key) or [] if row.get("role") == role]


def find_evidence(line, package, draft):
    """Candidate rates or prices for one cost line: the company's rate table rows first, then the draft's own."""
    return table_rows(line, package) + [from_draft(line)]


def contract_evidence(terms):
    return Evidence("contract", terms.get("reference"), "contract_term")


def company_evidence(key):
    return Evidence("company_setting", key, "company_setting")
