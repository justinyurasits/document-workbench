"""Takeoff math: quantities from measurements. The model proposes measurements and which surfaces a line covers;
code does the areas, lengths, counts, the waste factor from the company's config and the unit conversion."""
from decimal import Decimal

from construction_os.pricing.cost_history import unit_name
from construction_os.pricing.money import cents

PART_UNITS = {"floor_area": "SF", "ceiling_area": "SF", "wall_area": "SF", "perimeter": "LF", "count": "EA"}
CONVERSIONS = {("SF", "SF"): Decimal(1), ("SF", "SQ"): Decimal("0.01"), ("LF", "LF"): Decimal(1), ("EA", "EA"): Decimal(1)}
ALL_WALLS = ["length", "width", "length", "width"]


def num(value):
    text = f"{Decimal(str(value)).normalize():f}"
    return text


def waste_percent(values, waste_class):
    for row in values.get("WASTE-FACTORS") or []:
        if row["class"] == waste_class:
            return Decimal(str(row["percent"])), row.get("high_percent")
    return None, None


def part_quantity(part, measurement):
    """(quantity, unit, formula) for one part of a takeoff, or raises ValueError with what's wrong."""
    count = Decimal(str(measurement["count"]))
    length, width = Decimal(str(measurement["length_ft"])), Decimal(str(measurement["width_ft"]))
    sides = {"length": length, "width": width}
    kind = part["kind"]
    times = f"{num(count)} × " if count != 1 else ""
    if kind in ("floor_area", "ceiling_area"):
        return count * length * width, "SF", f"{times}{num(length)} ft × {num(width)} ft {kind.split('_')[0]}"
    if kind == "count":
        return count, "EA", f"{num(count)} each"
    walls = part.get("walls") or ALL_WALLS
    run = sum(sides[w] for w in walls)
    run_text = "(" + " + ".join(f"{num(sides[w])}" for w in walls) + " ft)"
    if kind == "perimeter":
        return count * run, "LF", f"{times}{run_text}"
    height = measurement.get("height_ft")
    if height is None:
        raise ValueError(f"measurement {measurement['id']!r} has no height_ft, which a wall area needs")
    height = Decimal(str(height))
    return count * run * height, "SF", f"{times}{run_text} × {num(height)} ft walls"


def compute(takeoff, measurements, values, line_unit):
    """(quantity in the line's unit, formula for the PM). Raises ValueError for anything code can't compute."""
    total, unit, parts = Decimal(0), None, []
    for part in takeoff["parts"]:
        measurement = measurements.get(part["measurement"])
        if measurement is None:
            raise ValueError(f"measurement {part['measurement']!r} isn't in measurements")
        quantity, part_unit, formula = part_quantity(part, measurement)
        if unit and part_unit != unit:
            raise ValueError(f"the parts mix {unit} and {part_unit}")
        unit = part_unit
        total += quantity
        parts.append(f"{formula} = {num(quantity)} {part_unit}")
    formula = "; ".join(parts) + (f"; total {num(total)} {unit}" if len(parts) > 1 else "")
    waste_class = takeoff.get("waste_class")
    if waste_class:
        percent, high = waste_percent(values, waste_class)
        if percent is None:
            raise ValueError(f"waste class {waste_class!r} isn't in the company's WASTE-FACTORS")
        total = total * (1 + percent / 100)
        formula += f"; + {num(percent)}% waste ({waste_class}" + (f", up to {num(high)}%" if high else "") + \
                   f") = {num(cents(total))} {unit}"
    target = unit_name(line_unit)
    factor = CONVERSIONS.get((unit, target))
    if factor is None:
        raise ValueError(f"a takeoff in {unit} can't be converted to {line_unit}")
    quantity = cents(total * factor)
    if factor != 1:
        formula += f" = {num(quantity)} {target}"
    return quantity, formula


def measurement_lines(measurements):
    """For the PCO's Basis of quantities: what was measured, approximately."""
    lines = []
    for m in measurements:
        size = f"approximately {num(m['length_ft'])} ft × {num(m['width_ft'])} ft"
        if m.get("height_ft"):
            size += f", {num(m['height_ft'])} ft high"
        lines.append(f"{m['label']}: {size}.")
    return lines
