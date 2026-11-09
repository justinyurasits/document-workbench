"""Evaluates a definition dependency's applies_when against the finished draft, for the manifest.

The definition writes conditions in a small fixed vocabulary. Each evaluates to True, False, or None when the
draft can't tell (an unknown fact, or a condition this evaluator doesn't recognize). None is recorded as
undecided, never guessed.
"""
import re

from construction_os.pricing.money import known_number


def evaluate(condition, draft, values):
    condition = (condition or "").strip()
    if not condition or condition == "always":
        return True
    if " and " in condition:
        parts = [evaluate(part, draft, values) for part in condition.split(" and ")]
        return False if False in parts else (None if None in parts else True)
    lines = draft["cost_lines"]
    schedule = draft["schedule"]
    if match := re.fullmatch(r"cost_lines include (\w+)", condition):
        return any(line["category"] == match.group(1) for line in lines)
    if match := re.fullmatch(r"pricing_basis is (\S+)", condition):
        return draft["pricing_basis"] == match.group(1)
    if match := re.fullmatch(r"pricing_basis in \[(.*)\]", condition):
        return draft["pricing_basis"] in [item.strip() for item in match.group(1).split(",")]
    if condition == "nte_present":
        return draft["nte_amount"].get("status") == "known"
    if match := re.fullmatch(r"schedule_extension_days > (\d+)", condition):
        days = known_number(schedule["extension_days"])
        return None if days is None else days > int(match.group(1))
    if match := re.fullmatch(r"schedule_extension_percent_of_duration > ([A-Z][A-Z-]+)", condition):
        days, duration = known_number(schedule["extension_days"]), known_number(schedule["contract_duration_days"])
        threshold = values.get(match.group(1))
        if days == 0:
            return False
        if days is None or not duration or threshold is None:
            return None
        return days / duration * 100 > threshold
    if match := re.fullmatch(r"([A-Z][A-Z-]+) enabled", condition):
        return bool((values.get(match.group(1)) or {}).get("enabled"))
    if match := re.fullmatch(r"(\w+) unknown", condition):
        field = draft["header"].get(match.group(1))
        return None if field is None else field.get("status") == "unknown"
    return None
