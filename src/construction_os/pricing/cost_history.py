"""Cost history: the company's past unit costs, a pricing evidence source beside rate tables and quotes.

Code retrieves candidate items for a proposed line (by division, unit and description); the model picks one by
id; code takes the price from the data at the company's COST-RANGE-POINT. The model never types a price from
cost history. A draft priced this way is a suggestion; the PM decides every price.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from .money import cents, number

POINTS = ("low", "mid", "high")
COMPONENTS = ("material", "total")          # labor in cost history never prices the company's own labor
UNIT_NAMES = {"HR": "HR", "HRS": "HR", "HOUR": "HR", "HOURS": "HR", "DAY": "DAY", "DAYS": "DAY", "WK": "WK",
              "WEEK": "WK", "WEEKS": "WK", "MO": "MO", "MONTH": "MO", "MONTHS": "MO", "EA": "EA", "EACH": "EA",
              "SF": "SF", "SQFT": "SF", "LF": "LF", "LS": "LS", "SQ": "SQ", "ROLL": "ROLL", "ROLLS": "ROLL",
              "LOAD": "LOAD", "LOADS": "LOAD"}
WORD = re.compile(r"[a-z0-9]+")
STOP = {"and", "or", "the", "a", "an", "of", "for", "to", "in", "with", "per", "at", "on", "each", "new", "existing"}


def unit_name(unit):
    """A unit as the cost history writes it: "Days" and "day" are DAY, "sqft" is SF."""
    text = str(unit or "").strip().upper().replace(".", "")
    return UNIT_NAMES.get(text, text)


@dataclass(frozen=True)
class Range:
    low: Decimal | None
    high: Decimal | None

    def at(self, point):
        """The value at a range point, to the cent. An item with one value uses it at every point."""
        if self.low is None:
            return None
        if self.high is None or point == "low":
            return self.low
        if point == "high":
            return self.high
        return cents((self.low + self.high) / 2)

    def text(self):
        if self.low is None:
            return "none"
        return f"${self.low:,.2f}" if self.high is None or self.high == self.low else f"${self.low:,.2f}–${self.high:,.2f}"


@dataclass(frozen=True)
class CostItem:
    id: str
    division: str | None
    category: str
    description: str
    unit: str
    material: Range
    labor: Range
    total: Range
    data_points: int | None
    allowance: bool = False
    excluded: str | None = None
    words: frozenset = field(default=frozenset(), compare=False)

    def component(self, name):
        return getattr(self, name)

    def components(self):
        return [name for name in COMPONENTS if self.component(name).low is not None]


def _range(data):
    data = data or {}
    return Range(number(data.get("low")), number(data.get("high")))


def items(package):
    """The package's cost history, parsed once per package."""
    cache = getattr(package, "_cost_history", None)
    if cache is None:
        cache = {}
        for row in package.values.get("COST-HISTORY") or []:
            item = CostItem(row["id"], row.get("division"), row.get("category") or "", row["description"],
                            unit_name(row["unit"]), _range(row.get("material")), _range(row.get("labor")),
                            _range(row.get("total")), row.get("data_points"), bool(row.get("allowance")),
                            row.get("excluded"), frozenset(words(f"{row['description']} {row.get('category') or ''}")))
            cache[item.id] = item
        try:
            package._cost_history = cache
        except AttributeError:
            pass
    return cache


def words(text):
    return {w for w in WORD.findall(str(text).lower()) if w not in STOP and len(w) > 1}


def range_point(package):
    point = package.values.get("COST-RANGE-POINT", "mid")
    return point if point in POINTS else "mid"


def price(item, component, package):
    """The item's component (material or total) at the company's range point."""
    return item.component(component).at(range_point(package))


def describe(item, component, package):
    """For the PM checklist: the item, the price used, the range, the data points, and whether it's an allowance."""
    value = price(item, component, package)
    points = item.data_points or 1
    parts = [f"Cost history {item.id}: \"{item.description}\", {component} ${value:,.2f}/{item.unit} at the "
             f"{range_point(package)} point",
             f"range {item.component(component).text()}" if item.component(component).high is not None else "no range",
             f"{points} data point{'' if points == 1 else 's'}"]
    if item.allowance:
        parts.append("allowance-derived (a past lump allowance, not a contract allowance)")
    return "; ".join(parts) + "."


def candidates(package, division=None, unit=None, query="", limit=8):
    """Cost items for a proposed line, best first. A real unit-cost item ranks above an allowance-derived one;
    then more matching words, then more data points. Excluded rows are never offered."""
    wanted = words(query)
    unit = unit_name(unit) if unit else None
    found = []
    for item in items(package).values():
        if item.excluded or not item.components():
            continue
        if division and item.division != str(division).zfill(2):
            continue
        if unit and item.unit != unit:
            continue
        score = len(wanted & item.words)
        if wanted and not score:
            continue
        found.append((item.allowance, -score, -(item.data_points or 1), item.id, item))
    found.sort(key=lambda entry: entry[:4])
    return [entry[-1] for entry in found[:limit]]


def offer(item):
    """What the model sees of an item: enough to choose it, and no prices to copy."""
    return {"id": item.id, "description": item.description, "category": item.category, "division": item.division,
            "unit": item.unit, "components": item.components(), "data_points": item.data_points or 1,
            "allowance_derived": item.allowance}
