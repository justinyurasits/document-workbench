"""Wording checks: each part of the PCO has one job, so nothing is said twice.

Line descriptions are short labels: no division or trade prefix, and no quantity with its unit (the columns show
them). Measurements appear only in Basis of quantities, which code writes. An assumption never repeats an
inclusion. Each check returns errors the model can act on.
"""
import re
from decimal import Decimal, InvalidOperation

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
                "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
WORDS = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
NUMBER = rf"(?:\(?(?P<{{name}}>\d+(?:\.\d+)?)\)?|(?P<{{name}}_w>\b(?:{WORDS})\b))"

# The units a line can be priced in, as a description might write them.
UNIT_WORDS = (r"sf|sq\.?\s*ft\.?|square\s+f(?:ee|oo)t|lf|lin\.?\s*ft\.?|linear\s+f(?:ee|oo)t|ea|each|hrs?|hours?|"
              r"days?|wks?|weeks?|months?|loads?|rolls?|ls|lump\s+sum|sq|squares?|cy|cubic\s+yards?")
QUANTITY_AND_UNIT = re.compile(NUMBER.format(name="n") + rf"\s*(?:-\s*)?(?:{UNIT_WORDS})\b", re.I)
ALWAYS_A_UNIT = re.compile(r"\blump\s+sum\b|\bper\s+(?:" + UNIT_WORDS + r")\b", re.I)

LENGTH = re.compile(NUMBER.format(name="n") + r"\s*(?:-\s*)?(?:ft\b|feet\b|foot\b|'|′)", re.I)
TIMES_AFTER = re.compile(NUMBER.format(name="n") + r"(?:\s*×\s*|\s+(?:x|by)\s+)", re.I)
TIMES_BEFORE = re.compile(r"(?:\s*×\s*|\s+(?:x|by)\s+)" + NUMBER.format(name="n"), re.I)
PREFIX = r"^\s*{name}\s*(?::|-|–|—)\s*"


def value_of(match):
    digits, word = match.group("n"), match.group("n_w")
    try:
        return Decimal(digits) if digits else Decimal(NUMBER_WORDS[word.lower()])
    except (InvalidOperation, KeyError):
        return None


def plain(text):
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


def assumption_text(item):
    return item if isinstance(item, str) else item["text"]


def known_value(field):
    return field.get("value") if isinstance(field, dict) and field.get("status") == "known" else None


def owner_text(proposal):
    """(where, text) for everything the Owner reads that the model writes. Basis of quantities is code's."""
    yield "description", known_value(proposal["description"]) or ""
    yield "reason", known_value(proposal["reason"]) or ""
    for key in ("inclusions", "deleted_scope"):
        for index, item in enumerate(known_value(proposal["scope"][key]) or []):
            yield f"scope/{key}/{index}", item
    for index, item in enumerate(proposal["scope"]["assumptions"]):
        yield f"scope/assumptions/{index}", assumption_text(item)
    for index, item in enumerate(proposal["clarifications_and_exclusions"]):
        if item.get("text"):
            yield f"clarifications_and_exclusions/{index}", item["text"]
    for index, line in enumerate(proposal["cost_lines"]):
        yield f"cost_lines/{index}", line["description"]


def line_errors(proposal, definition):
    """No division or trade prefix; no quantity next to a unit; no "lump sum" or "per <unit>"."""
    divisions = ((definition or {}).get("presentation") or {}).get("divisions") or {}
    errors = []
    for index, line in enumerate(proposal["cost_lines"]):
        text, where = line["description"], f"cost_lines/{index} ({line['description']})"
        division = known_value(line["division"])
        names = [n for n in (divisions.get(str(division).zfill(2)) if division else None, line.get("trade")) if n]
        for name in names:
            if re.match(PREFIX.format(name=re.escape(name)), text, re.I):
                said = text.lstrip()[:len(name)]
                errors.append(f"{where}: the description starts with \"{said}\"; the pricing table already shows "
                              f"the division. Write a short label, for example \"Install trim\".")
                break
        found = QUANTITY_AND_UNIT.search(text) or ALWAYS_A_UNIT.search(text)
        if found:
            errors.append(f"{where}: the description states a quantity or unit (\"{found.group().strip()}\"); the "
                          f"Qty and Unit columns show them. Write a short label without them.")
    return errors


def measurement_errors(proposal):
    """A measurement's length, width or height appears only in Basis of quantities, which code writes."""
    values = {Decimal(str(m[key])) for m in proposal.get("measurements") or []
              for key in ("length_ft", "width_ft", "height_ft") if m.get(key) is not None}
    if not values:
        return []
    errors = []
    for where, text in owner_text(proposal):
        for pattern in (LENGTH, TIMES_AFTER, TIMES_BEFORE):
            hit = next((m for m in pattern.finditer(text) if value_of(m) in values), None)
            if hit:
                errors.append(f"{where}: \"{hit.group().strip()}\" repeats a measurement. Measurements appear only "
                              f"in Basis of quantities, which code writes from measurements; say \"as measured\" or "
                              f"leave the size out.")
                break
    return errors


def repeat_errors(proposal, definition):
    """An assumption never repeats an inclusion, nor the field-measurement sentence code adds."""
    inclusions = {plain(item) for item in known_value(proposal["scope"]["inclusions"]) or []}
    added = ((definition or {}).get("estimating") or {}).get("field_measurement_assumption")
    errors = []
    for index, item in enumerate(proposal["scope"]["assumptions"]):
        text = plain(assumption_text(item))
        if text in inclusions:
            errors.append(f"scope/assumptions/{index}: repeats an inclusion word for word. State the scope once, "
                          f"under inclusions; remove this assumption.")
        elif added and text == plain(added):
            errors.append(f"scope/assumptions/{index}: code adds this sentence whenever there are measurements; "
                          f"remove it.")
    return errors


def wording_errors(proposal, definition):
    return line_errors(proposal, definition) + measurement_errors(proposal) + repeat_errors(proposal, definition)
