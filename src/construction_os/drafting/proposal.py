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
            "schedule", "open_questions", "repeat_client")
    properties = {name: copy.deepcopy(props[name]) for name in keep}
    properties["header"] = header
    properties["nte_amount"] = {"oneOf": [{"$ref": "#/$defs/unknown"}, {"$ref": "#/$defs/not_applicable"}]}
    properties["cost_lines"] = {"type": "array", "items": {"$ref": "#/$defs/cost_line"}}
    properties["architect_signs"] = {"$ref": "#/$defs/boolean"}
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
    required = [name for name in properties if name not in ("repeat_client", "request_record", "exhibits_needed")]
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


def check_proposal(proposal, schema, texts, roles, definition=None):
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
        if amount is None:
            errors.append(f"{where}: give amount, cited from an input, or unknown with a question.")
        elif amount["status"] == "cited":
            errors += citation_errors(where, amount, texts)
    for index, item in enumerate(proposal["contract_language"]):
        errors += citation_errors(f"contract_language/{index}", {**item, "value": None}, texts)
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
