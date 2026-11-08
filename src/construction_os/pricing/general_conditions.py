"""General conditions: added only when the extension is more than GC-EXTENSION-THRESHOLD of the duration.

Durations are calendar days. Weeks are extension_days / 7, prorated exactly; only the line amount is rounded.
Roles and durations are never guessed: a gap is a flag.
"""
from .flags import Flag
from .lines import price_line
from .money import known_number, number

THRESHOLD = "GC-EXTENSION-THRESHOLD"


def gc_applies(schedule, values):
    """True, False, or None when it can't be decided. Returns (applies, extension_days, flags)."""
    extension = known_number(schedule.get("extension_days"))
    if extension is None:
        return None, None, [Flag("gc_duration_unknown",
                                 "The schedule extension is unknown, so general conditions can't be decided.")]
    if extension == 0:
        return False, extension, []
    threshold = number(values.get(THRESHOLD))
    if threshold is None:
        return None, extension, [Flag("setting_missing", f"{THRESHOLD} isn't set, so general conditions can't be decided.")]
    duration = known_number(schedule.get("contract_duration_days"))
    if not duration:
        return None, extension, [Flag("gc_duration_unknown",
                                      f"The schedule extends {extension:g} days, but the contract duration is "
                                      f"unknown, so general conditions can't be decided.")]
    share = extension / duration * 100
    return share > threshold, extension, []


def general_conditions(draft, package):
    """The draft's general conditions lines, priced or excluded. Returns (priced lines by index, flags)."""
    applies, extension, flags = gc_applies(draft["schedule"], package.values)
    indexed = [(i, line) for i, line in enumerate(draft["cost_lines"]) if line["category"] == "general_conditions"]
    threshold = package.values.get(THRESHOLD)
    priced = {}
    if applies and not indexed:
        flags.append(Flag("gc_missing_lines",
                          f"The extension is more than {threshold}% of the duration, so general conditions apply, "
                          f"but the draft has no general conditions lines. Add one per role."))
    for index, line in indexed:
        if applies:
            result, line_flags = price_line(line, index, package, draft, extension_days=extension)
            flags += line_flags
        else:
            result, _ = price_line(line, index, package, draft, extension_days=extension or 0)
            if applies is None:
                result.amount = result.unit_price = None
            else:
                result.included = False
                flags.append(Flag("gc_not_applicable",
                                  f"{line['description']}: the extension isn't more than {threshold}% of the "
                                  f"duration, so general conditions don't apply. Left out of the total.", index))
        priced[index] = result
    return priced, flags
