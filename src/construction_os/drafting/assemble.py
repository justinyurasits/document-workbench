"""Turns a checked proposal into a full structured draft. Code sets everything the model must not author.

Evidence follows where the figure was found: a supplied quote file is a supplied_quote; a quote the PM only
mentions in the notes is pm_notes with basis quote, and the PM is asked for the written quote (principle 8:
never cite a source that doesn't say it).
"""
import copy
import re

from construction_os.rendering.render import SIGNATURE_ORDER, Flag

from .proposal import RATE_BASES

DOLLARS = re.compile(r"\$\s?\d")


def known(value):
    return {"status": "known", "value": value}


def unknown(question):
    return {"status": "unknown", "question": question}


def not_applicable(reason):
    return {"status": "not_applicable", "reason": reason}


def assemble_draft(proposal, project, inputs, package, today):
    """inputs: the request's InputRecords, in order. Returns (draft, flags)."""
    proposal = copy.deepcopy(proposal)
    quotes = [record for record in inputs if record.kind == "quote"]
    letters = {record.id: chr(ord("A") + index) for index, record in enumerate(quotes)}
    by_id = {record.id: record for record in inputs}
    flags = []
    header = {
        "pco_number": unknown("Which PCO number comes next on this project?"),
        "date": known(today.isoformat()),
        "project": known(project.name) if project.name else unknown("What is the project name?"),
        **proposal["header"],
    }
    lines = [cost_line(line, by_id, letters) for line in proposal["cost_lines"]]
    draft = {
        "definition": package.definition["id"],
        "header": {name: header[name] for name in ("pco_number", "date", "project", "project_address", "owner",
                                                    "contract_reference", "location", "trades", "change_event_ref",
                                                    "requested_approval_date", "cause_category")},
        "description": proposal["description"],
        "reason": proposal["reason"],
        "scope": proposal["scope"],
        "clarifications_and_exclusions": proposal["clarifications_and_exclusions"],
        "pricing_basis": proposal["pricing_basis"],
        "nte_amount": proposal["nte_amount"],
        "cost_lines": lines,
        "contract_terms": not_applicable("Contract pricing terms are not applied yet (build step 6)."),
        "markup_presentation": {"value": package.values["MARKUP-PRESENTATION"], "source": "company_setting",
                                "reference": not_applicable("Company setting.")},
        "architect_signs": architect(proposal["architect_signs"]),
        "signatures": {"order": list(SIGNATURE_ORDER[package.values["AUTH-WRITTEN.cosign_timing"]])},
        "schedule": proposal["schedule"],
        "attachments": [{"letter": letters[r.id], "description": r.original_name} for r in quotes],
        "open_questions": proposal["open_questions"],
    }
    if "repeat_client" in proposal:
        draft["repeat_client"] = proposal["repeat_client"]
    if proposal["contract_language"]:
        quoted = " ".join(f"\"{item['excerpt']}\" ({source_name(by_id[item['input']])})"
                          for item in proposal["contract_language"])
        flags.append(Flag(
            "contract_terms_not_applied",
            f"Contract pricing terms were supplied, and this draft does NOT apply them: {quoted} It is priced at "
            f"the company standard, so sent as is it may charge more than the contract allows. Reprice per the "
            f"contract before sending."))
    for where, text in descriptive_text(draft):
        if DOLLARS.search(text):
            flags.append(Flag("price_in_scope_text",
                                      f"{where} states a dollar figure: \"{text}\". Prices belong only in the "
                                      f"pricing table, which code computes. Remove it or confirm it."))
    return draft, flags


def cost_line(line, inputs, letters):
    category, basis = line["category"], line["basis"]
    result = {key: line[key] for key in ("category", "trade", "division", "description", "quantity", "unit", "basis",
                                         "permit")}
    if "role" in line:
        result["role"] = line["role"]
    if RATE_BASES.get(category) == basis:
        result["unit_price"] = not_applicable("From the company rate table.")
        result["evidence"] = {
            "source": "rate_table",
            "reference": known(f"Company rate table, {line['role']}") if "role" in line
            else unknown(f"Which rate table role prices \"{line['description']}\"?"),
            "date": not_applicable("Current company rate table."),
        }
        result["attachment"] = not_applicable("No attachment for this line.")
        return result
    amount = line["amount"]
    if amount["status"] == "unknown":
        result["unit_price"] = unknown(amount["question"])
        result["evidence"] = {"source": "pm_notes", "reference": known("PM's notes"),
                              "date": not_applicable("From the PM's request.")}
        result["attachment"] = not_applicable("No attachment for this line.")
        return result
    source = inputs[amount["input"]]
    result["unit_price"] = known(amount["value"])
    if source.kind == "quote":
        result["evidence"] = {"source": "supplied_quote", "reference": known(source.original_name),
                              "date": unknown(f"What is the date of the quote {source.original_name}?")}
        result["attachment"] = known(letters[source.id])
    else:
        result["evidence"] = {"source": "pm_notes", "reference": known("PM's notes"),
                              "date": not_applicable("From the PM's request.")}
        result["attachment"] = (unknown(f"Get the written quote for \"{line['description']}\" and attach it.")
                                if basis == "quote" else not_applicable("No attachment for this line."))
    return result


def source_name(record):
    return "the PM's notes" if record.kind == "notes" else f"quote {record.original_name}"


def architect(field):
    if field.get("status") == "known":
        return {"value": field["value"], "source": "pm", "reference": known("PM's notes")}
    return field


def descriptive_text(draft):
    yield "The description", draft["description"].get("value", "")
    yield "The reason", draft["reason"].get("value", "")
    for item in draft["scope"]["inclusions"].get("value") or []:
        yield "An inclusion", item
    for item in draft["scope"]["assumptions"]:
        yield "An assumption", item
    for item in draft["clarifications_and_exclusions"]:
        yield "A clarification", item["text"]
