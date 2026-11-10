"""Turns a checked proposal into a full structured draft. Code sets everything the model must not author.

Evidence follows where the figure was found: a supplied quote file is a supplied_quote; a quote the PM only
mentions in the notes is pm_notes with basis quote, and the PM is asked for the written quote (principle 8:
never cite a source that doesn't say it). The PCO stands alone (CHG-53 2): quotes, the Owner's correspondence
and Owner selections are filed as backup, never attached. Only an Owner-facing drawing becomes an exhibit, and
only when it was supplied. Clarifications that use the company's default get its approved text verbatim.
"""
import copy
import re

from construction_os.rendering.render import SIGNATURE_ORDER, Flag

from .proposal import RATE_BASES
from .takeoff import compute, measurement_lines

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
    measurements = {m["id"]: m for m in proposal.get("measurements") or []}
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
        "cost_lines": [cost_line(line, by_id, package, measurements) for line in proposal["cost_lines"]],
        "contract_terms": not_applicable("Contract pricing terms are not applied yet (build step 6)."),
        "markup_presentation": {"value": package.values["MARKUP-PRESENTATION"], "source": "company_setting",
                                "reference": not_applicable("Company setting.")},
        "architect_signs": architect(proposal["architect_signs"]),
        "signatures": {"order": list(SIGNATURE_ORDER[package.values["AUTH-WRITTEN.cosign_timing"]])},
        "schedule": schedule_with_activities(proposal),
        "attachments": [],
        "open_questions": proposal["open_questions"],
    }
    if proposal.get("measurements"):
        draft["measurements"] = copy.deepcopy(proposal["measurements"])
        draft["basis_of_quantities"] = measurement_lines(proposal["measurements"])
        sentence = (package.definition.get("estimating") or {}).get("field_measurement_assumption")
        if sentence:          # the package's wording; the numbers stay in Basis of quantities
            draft["scope"]["assumptions"].append({"kind": "scope", "text": sentence})
    if proposal.get("internal_notes"):
        draft["internal_notes"] = proposal["internal_notes"]
    if "request_record" in proposal:
        draft["request_record"] = proposal["request_record"]
    if "repeat_client" in proposal:
        draft["repeat_client"] = proposal["repeat_client"]
    file_documents(draft, proposal, inputs, package)
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


def file_documents(draft, proposal, inputs, package):
    """Owner-facing drawings become attachments; everything else supplied is backup filed with the PCO.

    A supplied Owner selection file means the selection goes to the Owner with this PCO: lines whose selection
    isn't approved yet become issued_with_pco. The selection is still referenced by ID only, never attached.
    """
    presentation = package.definition.get("presentation") or {}
    attachments, backup = [], []
    for record in inputs:
        if record.kind == "drawing":
            attachments.append({"letter": chr(ord("A") + len(attachments)), "description":
                                f"Layout or drawings ({record.original_name})", "supplied": True, "kind": "drawing"})
        elif record.kind in ("quote", "correspondence", "selection"):
            backup.append({"kind": record.kind, "description": record.original_name, "file": record.file})
    requested = set(proposal.get("exhibits_needed") or [])
    for exhibit in (presentation.get("exhibits") or {}).get("standard", []):
        supplied = any(a["kind"] == exhibit.get("input_kind") for a in attachments)
        if exhibit["id"] in requested and not supplied:
            attachments.append({"letter": chr(ord("A") + len(attachments)), "description": exhibit["title"],
                                "supplied": False, "kind": exhibit.get("input_kind") or "other"})
    if any(record.kind == "selection" for record in inputs):
        for line in draft["cost_lines"]:
            selection = line.get("selection")
            if selection and selection["status"] not in ("approved", "approved_as_noted"):
                selection["status"] = "issued_with_pco"
    draft["attachments"] = attachments
    draft["backup"] = backup


def table_unit(line, package):
    """The rate-table unit for a labor line priced per unit of time whose role and unit match the table."""
    if line["category"] != "labor" or line["basis"] != "rate_x_duration" or "role" not in line:
        return None
    row = next((r for r in package.values.get("LABOR-BILLING-RATES") or [] if r.get("role") == line["role"]), None)
    unit = str((line.get("unit") or {}).get("value", "")).strip().lower()
    if row and unit in {row.get("unit"), f"{row.get('unit')}s", {"hour": "hr", "day": "day", "week": "wk"}.get(row.get("unit"))}:
        return row.get("unit")
    return None


def schedule_with_activities(proposal):
    """Crew durations (in days) become the schedule's activity breakdown, shown when days are added."""
    schedule = copy.deepcopy(proposal["schedule"])
    activities = [{"activity": line["description"], "duration_days": line["duration"]["days"]}
                  for line in proposal["cost_lines"]
                  if line["category"] != "general_conditions" and "days" in (line.get("duration") or {})]
    if activities:
        schedule["activities"] = activities
    return schedule


def cost_line(line, inputs, package, measurements=None):
    category, basis = line["category"], line["basis"]
    result = {key: line[key] for key in ("category", "trade", "division", "description", "quantity", "unit", "basis",
                                         "permit")}
    for key in ("role", "selection", "cost_item", "duration", "quantity_basis"):
        if key in line:
            result[key] = copy.deepcopy(line[key])
    result["attachment"] = not_applicable("Backup on file in the job folder, not attached.")
    if line.get("takeoff"):
        quantity, formula = compute(line["takeoff"], measurements or {}, package.values, line["unit"].get("value"))
        result["takeoff"] = {**copy.deepcopy(line["takeoff"]), "formula": formula, "result": f"{quantity}"}
        result["quantity"] = known(float(quantity))
    duration = line.get("duration") or {}
    if category != "general_conditions" and ("days" in duration or "hours" in duration):
        result["quantity"] = known(duration.get("days", duration.get("hours")))
    if line.get("cost_item"):
        result["unit_price"] = not_applicable("From the company cost history, at the company's range point.")
        result["evidence"] = {"source": "cost_history", "reference": known(line["cost_item"]["id"]),
                              "date": not_applicable("Historical unit cost.")}
        return result
    if RATE_BASES.get(category) == basis or table_unit(line, package) is not None:
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
        yield "An assumption", item if isinstance(item, str) else item["text"]
    for item in draft["scope"].get("not_included") or []:
        yield "A not-included item", item
    for item in draft["clarifications_and_exclusions"]:
        yield "A clarification", item["text"]
