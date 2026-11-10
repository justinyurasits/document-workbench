"""What the model may propose, and the checks its proposal must pass before code assembles the draft.

The proposal schema is derived from schemas/pco_draft.schema.json, so the two can't drift. It keeps the
content the model drafts and removes every field it must not author: unit prices, evidence, attachments,
signatures, markup presentation, contract terms, and the header fields code sets. Every object keeps
additionalProperties: false, so a proposal carrying a price fails validation and is rejected, never repaired.
"""
import copy
import json
import re
from decimal import Decimal

import jsonschema

from construction_os.validate import DRAFT_SCHEMA

from .inputs import normalized

TOOL_NAME = "propose_pco_draft"
CODE_HEADER_FIELDS = ("pco_number", "date", "project")     # set by code: PM assigns the number; today; project.yaml
RATE_BASES = {"labor": "hours_x_rate", "general_conditions": "weeks_x_rate"}


def priced_from_table(line, roles):
    """Labor per unit of time for a listed role (a crew per DAY) takes the table rate, like hours × rate."""
    unit = (line.get("unit") or {}).get("value", "")
    table_unit = (roles.get("labor") or {}).get(line.get("role"))
    return (line["category"] == "labor" and line["basis"] == "rate_x_duration" and table_unit is not None
            and str(unit).strip().lower() in {table_unit, f"{table_unit}s", {"hour": "hr", "day": "day", "week": "wk"}[table_unit]})
TOKEN = re.compile(r"(?<![\w.,])\d[\d,]*(?:\.\d+)?")
GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")


def draft_schema():
    return json.loads(DRAFT_SCHEMA.read_text())


def proposal_schema():
    draft = draft_schema()
    defs = copy.deepcopy(draft["$defs"])
    line = defs["cost_line"]
    for name in ("unit_price", "evidence", "attachment"):
        del line["properties"][name]
        line["required"].remove(name)
    for name in ("formula", "result"):                      # code computes these from the takeoff
        del line["properties"]["takeoff"]["properties"][name]
    clarification = defs["clarification"]
    clarification["required"] = ["prompt", "trades"]
    clarification["properties"]["use_default"] = {
        "const": True, "description": "Use the company's approved text for this prompt; leave disposition and text out."}
    line["properties"]["amount"] = {
        "description": "Only for lines not priced from a rate table: the amount copied from an input, or unknown.",
        "oneOf": [
            {"type": "object", "additionalProperties": False, "required": ["status", "input", "excerpt", "value"],
             "properties": {"status": {"const": "cited"}, "input": {"type": "string", "minLength": 1},
                            "excerpt": {"type": "string", "minLength": 1}, "value": {"type": "number", "minimum": 0}}},
            {"$ref": "#/$defs/unknown"},
        ],
    }
    header = copy.deepcopy(draft["properties"]["header"])
    for name in CODE_HEADER_FIELDS:
        del header["properties"][name]
        header["required"].remove(name)
    props = draft["properties"]
    keep = ("description", "reason", "request_record", "scope", "clarifications_and_exclusions", "pricing_basis",
            "schedule", "open_questions", "repeat_client", "measurements")
    properties = {name: copy.deepcopy(props[name]) for name in keep}
    properties["header"] = header
    properties["nte_amount"] = {"oneOf": [{"$ref": "#/$defs/unknown"}, {"$ref": "#/$defs/not_applicable"}]}
    properties["cost_lines"] = {"type": "array", "items": {"$ref": "#/$defs/cost_line"}}
    properties["architect_signs"] = {"$ref": "#/$defs/boolean"}
    properties["internal_notes"] = copy.deepcopy(draft["properties"]["internal_notes"])
    properties["exhibits_needed"] = {
        "description": "Standard exhibits this change needs that are listed as 'requested' in the definition.",
        "type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
    properties["contract_language"] = {
        "description": "Contract pricing terms found in the inputs, copied verbatim. Not applied in this version.",
        "type": "array",
        "items": {"type": "object", "additionalProperties": False, "required": ["input", "excerpt"],
                  "properties": {"input": {"type": "string", "minLength": 1},
                                 "excerpt": {"type": "string", "minLength": 1}}},
    }
    required = [name for name in properties
                if name not in ("repeat_client", "request_record", "exhibits_needed", "internal_notes", "measurements")]
    return {"type": "object", "additionalProperties": False, "required": required, "properties": properties,
            "$defs": defs}


def tool_definition(schema):
    return {"name": TOOL_NAME,
            "description": "Submit the proposed PCO content: scope, cost lines with quantities and cited amounts, "
                           "schedule, clarifications and open questions. Never prices, rates or totals.",
            "input_schema": schema}


def numbers_in(text):
    """Plain numbers written in the text. "$2,850", "2850.00" and "$900." count; "2.85k" and "3x" don't."""
    found = set()
    for match in TOKEN.finditer(text):
        token = match.group().rstrip(",")
        following = text[match.start() + len(token):match.start() + len(token) + 1]
        if following.isalpha():
            continue
        whole, _, fraction = token.partition(".")
        if GROUPED.fullmatch(whole):
            found.add(Decimal(whole.replace(",", "") + (f".{fraction}" if fraction else "")))
    return found


def schema_errors(proposal, schema):
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(proposal), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in error.absolute_path) or "(top level)"
        errors.append(f"{where}: {error.message}")
    return errors


def check_proposal(proposal, schema, texts, roles, definition=None, package=None):
    """Every reason the proposal can't be used. Empty means it passes.

    texts: input id -> the input's text. roles: {"labor": {role: unit}, "general_conditions": {role: unit}}.
    definition: the PCO definition, for its clarification defaults and requestable exhibits.
    """
    errors = schema_errors(proposal, schema)
    if errors:
        return errors
    definition = definition or {}
    defaults = {p["id"] for p in definition.get("clarification_prompts") or [] if p.get("default")}
    for index, item in enumerate(proposal["clarifications_and_exclusions"]):
        where = f"clarifications_and_exclusions/{index} ({item['prompt']})"
        if item.get("use_default"):
            if item["prompt"] not in defaults:
                errors.append(f"{where}: this prompt has no company default; write disposition and text instead.")
            elif "text" in item or "disposition" in item:
                errors.append(f"{where}: with use_default, leave disposition and text out; code inserts the approved text.")
        elif "text" not in item or "disposition" not in item:
            errors.append(f"{where}: give disposition and text, or use_default where the company has a default.")
    errors += description_errors(proposal, definition)
    for index, line in enumerate(proposal["cost_lines"]):
        if "selection" in line:
            errors += selection_errors(f"cost_lines/{index} ({line['description']}) selection", line["selection"], texts)
    requestable = {e["id"] for e in ((definition.get("presentation") or {}).get("exhibits") or {}).get("standard", [])
                   if e.get("applies_when") == "requested"}
    for name in proposal.get("exhibits_needed") or []:
        if name not in requestable:
            errors.append(f"exhibits_needed: {name!r} isn't one of the exhibits that can be requested "
                          f"({', '.join(sorted(requestable)) or 'none'}).")
    for index, line in enumerate(proposal["cost_lines"]):
        where = f"cost_lines/{index} ({line['description']})"
        category, basis = line["category"], line["basis"]
        if "role" in line and category in roles and line["role"] not in roles[category]:
            errors.append(f"{where}: role {line['role']!r} isn't in the {category} roles list. Use a listed role "
                          f"exactly, or leave role out and add an open question.")
        if "role" in line and category not in roles:
            errors.append(f"{where}: role applies only to labor and general_conditions lines.")
        if basis in RATE_BASES.values():
            if RATE_BASES.get(category) != basis:
                errors.append(f"{where}: basis {basis} is for {category} lines only where the rate table prices them.")
            if "amount" in line:
                errors.append(f"{where}: a line priced as {basis} takes its rate from the company table; remove amount.")
            continue
        amount = line.get("amount")
        if line.get("cost_item"):
            if amount is not None and amount["status"] == "cited":
                errors.append(f"{where}: a cost-history line takes its price from the company's cost history; "
                              f"remove amount.")
            continue
        if priced_from_table(line, roles):
            if amount is not None and amount["status"] == "cited":
                errors.append(f"{where}: {line['role']} is priced per {roles['labor'][line['role']]} from the company "
                              f"table; remove amount.")
            continue
        if amount is None:
            errors.append(f"{where}: give amount, cited from an input, or unknown with a question.")
        elif amount["status"] == "cited":
            errors += citation_errors(where, amount, texts)
    for index, item in enumerate(proposal["contract_language"]):
        errors += citation_errors(f"contract_language/{index}", {**item, "value": None}, texts)
    if package is not None:
        errors += estimating_errors(proposal, texts, package)
    return errors


def citation_errors(where, cited, texts):
    if cited["input"] not in texts:
        return [f"{where}: input {cited['input']!r} doesn't exist; inputs are {', '.join(texts)}."]
    excerpt = normalized(cited["excerpt"])
    if excerpt not in normalized(texts[cited["input"]]):
        return [f"{where}: the excerpt isn't in {cited['input']} word for word. Copy it exactly, or make the "
                f"amount unknown with a question."]
    if cited["value"] is not None and Decimal(str(cited["value"])) not in numbers_in(excerpt):
        return [f"{where}: {cited['value']} isn't a number written in the excerpt. If the figure isn't written as "
                f"a plain number, make the amount unknown with a question."]
    return []


def description_errors(proposal, definition):
    """For the causes the company opens with the Owner's request, the description continues that sentence."""
    opening = (definition.get("presentation") or {}).get("description_opening")
    cause, description = proposal["header"]["cause_category"], proposal["description"]
    if not opening or cause.get("status") != "known" or cause["value"] not in opening.get("applies_to", []):
        return []
    if description.get("status") == "known" and not description["value"].startswith(opening["continues_with"]):
        return [f"description: for a {cause['value']} change the PCO opens with the Owner's request, so the description "
                f"must continue that sentence, starting \"{opening['continues_with']}\"."]
    return []


def date_forms(iso):
    year, month, day = (int(part) for part in iso.split("-"))
    names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]
    return {iso, f"{names[month - 1]} {day}, {year}", f"{month}/{day}/{year}", f"{month}/{day}/{year % 100:02d}",
            f"{names[month - 1][:3]} {day}, {year}"}


def selection_errors(where, selection, texts):
    """A selection ID or date must come from the inputs, never be made up."""
    errors = []
    corpus = " ".join(normalized(text) for text in texts.values())
    ident, when = selection["id"], selection["date"]
    if ident.get("status") == "known" and normalized(ident["value"]) not in corpus:
        errors.append(f"{where}: ID {ident['value']!r} isn't in the inputs. Leave it unknown with a question.")
    if when.get("status") == "known" and not any(form in corpus for form in date_forms(when["value"])):
        errors.append(f"{where}: date {when['value']} isn't in the inputs. Leave it unknown with a question.")
    return errors


COST_ITEM_CATEGORIES = {"materials", "subcontractors", "equipment", "permits_and_fees"}


def estimating_errors(proposal, texts, package):
    """Cost-history picks, measurements, takeoffs and durations: code must be able to price and compute every one."""
    from construction_os.pricing import cost_history

    from .takeoff import compute

    errors = []
    measurements = {}
    for index, m in enumerate(proposal.get("measurements") or []):
        where = f"measurements/{index} ({m['id']})"
        if m["id"] in measurements:
            errors.append(f"{where}: the id is used twice.")
        measurements[m["id"]] = m
        if m["source"] == "notes":
            if "excerpt" not in m or "input" not in m:
                errors.append(f"{where}: a measurement from the notes needs input and a verbatim excerpt.")
            else:
                errors += citation_errors(where, {"input": m["input"], "excerpt": m["excerpt"], "value": None}, texts)
        elif not m.get("basis"):
            errors.append(f"{where}: an estimated measurement needs its basis.")
    extension = (proposal["schedule"].get("extension_days") or {})
    extension_days = extension.get("value") if extension.get("status") == "known" else None
    for index, line in enumerate(proposal["cost_lines"]):
        where = f"cost_lines/{index} ({line['description']})"
        chosen = line.get("cost_item")
        if chosen:
            item = cost_history.items(package).get(chosen["id"])
            if item is None:
                errors.append(f"{where}: cost item {chosen['id']!r} doesn't exist. Pick an id find_cost_items returned.")
            elif item.excluded:
                errors.append(f"{where}: cost item {item.id} is excluded ({item.excluded}).")
            else:
                if line["category"] not in COST_ITEM_CATEGORIES:
                    errors.append(f"{where}: a {line['category']} line can't be priced from cost history; the company's "
                                  f"own labor comes from the rate table.")
                if chosen["component"] not in item.components():
                    errors.append(f"{where}: cost item {item.id} has no {chosen['component']} price; it has "
                                  f"{', '.join(item.components()) or 'none'}.")
                if cost_history.unit_name((line.get("unit") or {}).get("value")) != item.unit:
                    errors.append(f"{where}: the line is in {(line.get('unit') or {}).get('value')}, but cost item "
                                  f"{item.id} is per {item.unit}.")
                if line["basis"] != "unit_cost":
                    errors.append(f"{where}: a cost-history line has basis unit_cost.")
        if line.get("takeoff") or line.get("duration"):
            if (line.get("quantity") or {}).get("status") == "known":
                errors.append(f"{where}: the quantity comes from the takeoff or duration; make it not_applicable.")
        if line.get("takeoff"):
            try:
                compute(line["takeoff"], measurements, package.values, (line.get("unit") or {}).get("value"))
            except ValueError as error:
                errors.append(f"{where}: takeoff: {error}.")
        duration = line.get("duration")
        if duration:
            unit = cost_history.unit_name((line.get("unit") or {}).get("value"))
            if line["category"] == "general_conditions":
                if "days" not in duration:
                    errors.append(f"{where}: a general conditions duration is in days.")
                elif extension_days is not None and duration["days"] > extension_days:
                    errors.append(f"{where}: {duration['days']} days is more than the {extension_days}-day extension.")
            elif unit == "DAY" and "days" not in duration or unit == "HR" and "hours" not in duration:
                errors.append(f"{where}: give the duration in {'days' if unit == 'DAY' else 'hours'} to match the unit.")
            elif unit not in ("DAY", "HR"):
                errors.append(f"{where}: a duration needs a DAY or HR unit.")
    return errors
