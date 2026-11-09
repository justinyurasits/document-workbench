"""Turns a checked proposal into a full structured draft. Code sets everything the model must not author.

Evidence follows where the figure was found: a supplied quote file is a supplied_quote; a quote the PM only
mentions in the notes is pm_notes with basis quote, and the PM is asked for the written quote (principle 8:
never cite a source that doesn't say it). Exhibits are lettered here: one per quoted line (a supplied quote file
is one exhibit however many lines cite it), then the definition's standard exhibits. An exhibit is marked
supplied only when a file of that kind is among the request's inputs. Clarifications that use the company's
default get its approved text verbatim.
"""
import copy
import re

from construction_os.conditions import evaluate
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
    by_id = {record.id: record for record in inputs}
    flags = []
    header = {
        "pco_number": unknown("Which PCO number comes next on this project?"),
        "date": known(today.isoformat()),
        "project": known(project.name) if project.name else unknown("What is the project name?"),
        **proposal["header"],
    }
    draft = {
        "definition": package.definition["id"],
        "header": {name: header[name] for name in ("pco_number", "date", "project", "project_address", "owner",
                                                    "contract_reference", "location", "trades", "change_event_ref",
                                                    "requested_approval_date", "cause_category")},
        "description": proposal["description"],
        "reason": proposal["reason"],
        "scope": proposal["scope"],
        "clarifications_and_exclusions": clarifications(proposal["clarifications_and_exclusions"], package.definition),
        "pricing_basis": proposal["pricing_basis"],
        "nte_amount": proposal["nte_amount"],
        "cost_lines": [cost_line(line, by_id) for line in proposal["cost_lines"]],
        "contract_terms": not_applicable("Contract pricing terms are not applied yet (build step 6)."),
        "markup_presentation": {"value": package.values["MARKUP-PRESENTATION"], "source": "company_setting",
                                "reference": not_applicable("Company setting.")},
        "architect_signs": architect(proposal["architect_signs"]),
        "signatures": {"order": list(SIGNATURE_ORDER[package.values["AUTH-WRITTEN.cosign_timing"]])},
        "schedule": proposal["schedule"],
        "attachments": [],
        "open_questions": proposal["open_questions"],
    }
    if "request_record" in proposal:
        draft["request_record"] = proposal["request_record"]
    if "repeat_client" in proposal:
        draft["repeat_client"] = proposal["repeat_client"]
    letter_exhibits(draft, proposal, inputs, package)
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


def clarifications(items, definition):
    """The model's clarifications, with the company's approved default text where it asked for it."""
    defaults = {p["id"]: p["default"] for p in definition.get("clarification_prompts") or [] if p.get("default")}
    result = []
    for item in items:
        if item.get("use_default"):
            default = defaults[item["prompt"]]
            result.append({"prompt": item["prompt"], "trades": item["trades"], "disposition": default["disposition"],
                           "text": default["text"]})
        else:
            result.append({key: item[key] for key in ("prompt", "trades", "disposition", "text")})
    return result


def quote_title(line, presentation):
    party = (presentation.get("trade_parties") or {}).get(line["trade"])
    template = (presentation.get("exhibits") or {}).get("quote")
    if party and template:
        return template.format(Party=party[0].upper() + party[1:], party=party)
    return f"{line['trade'].capitalize()} quote"


def letter_exhibits(draft, proposal, inputs, package):
    """One exhibit per quoted line, then the standard exhibits. Supplied only when a file was actually given."""
    presentation = package.definition.get("presentation") or {}
    attachments = []

    def add(description, supplied, kind):
        letter = chr(ord("A") + len(attachments))
        attachments.append({"letter": letter, "description": description, "supplied": supplied, "kind": kind})
        return letter

    file_letters = {}
    by_id = {record.id: record for record in inputs}
    for line, proposed in zip(draft["cost_lines"], proposal["cost_lines"]):
        if line["basis"] != "quote":
            continue
        amount = proposed.get("amount") or {}
        source = by_id.get(amount.get("input"))
        if source is not None and source.kind == "quote":
            if source.id not in file_letters:
                file_letters[source.id] = add(f"{quote_title(line, presentation)} ({source.original_name})", True, "quote")
            line["attachment"] = known(file_letters[source.id])
        else:
            line["attachment"] = known(add(quote_title(line, presentation), False, "quote"))
    for record in inputs:
        if record.kind == "quote" and record.id not in file_letters:
            file_letters[record.id] = add(f"Quote ({record.original_name})", True, "quote")
    requested = set(proposal.get("exhibits_needed") or [])
    for exhibit in (presentation.get("exhibits") or {}).get("standard", []):
        files = [record.original_name for record in inputs if record.kind == exhibit.get("input_kind")]
        if exhibit.get("applies_when") == "requested":
            applies = exhibit["id"] in requested
        else:
            applies = evaluate(exhibit.get("applies_when"), draft, package.values) is not False
        if files:
            add(f"{exhibit['title']} ({', '.join(files)})", True, exhibit.get("input_kind") or "other")
        elif applies:
            add(exhibit["title"], False, exhibit.get("input_kind") or "other")
    draft["attachments"] = attachments


def cost_line(line, inputs):
    category, basis = line["category"], line["basis"]
    result = {key: line[key] for key in ("category", "trade", "division", "description", "quantity", "unit", "basis",
                                         "permit")}
    if "role" in line:
        result["role"] = line["role"]
    result["attachment"] = not_applicable("No attachment for this line.")   # quoted lines get their exhibit letter
    if RATE_BASES.get(category) == basis:
        result["unit_price"] = not_applicable("From the company rate table.")
        result["evidence"] = {
            "source": "rate_table",
            "reference": known(f"Company rate table, {line['role']}") if "role" in line
            else unknown(f"Which rate table role prices \"{line['description']}\"?"),
            "date": not_applicable("Current company rate table."),
        }
        return result
    amount = line["amount"]
    if amount["status"] == "unknown":
        result["unit_price"] = unknown(amount["question"])
        result["evidence"] = {"source": "pm_notes", "reference": known("PM's notes"),
                              "date": not_applicable("From the PM's request.")}
        return result
    source = inputs[amount["input"]]
    result["unit_price"] = known(amount["value"])
    if source.kind == "quote":
        result["evidence"] = {"source": "supplied_quote", "reference": known(source.original_name),
                              "date": unknown(f"What is the date of the quote {source.original_name}?")}
    else:
        result["evidence"] = {"source": "pm_notes", "reference": known("PM's notes"),
                              "date": not_applicable("From the PM's request.")}
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
