"""Decimal helpers. Every amount is rounded to cents per line and per component, never in between."""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def number(value):
    """A Decimal from a YAML or JSON number, without float noise."""
    return None if value is None else Decimal(str(value))


def known_number(field):
    return number(field["value"]) if (field or {}).get("status") == "known" else None
