"""Render a structured PCO draft into the company's Word template. Flags never block the output."""
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from docx import Document
from docx.shared import Inches, Pt
from docxtpl import DocxTemplate, InlineImage
from jinja2 import Environment, StrictUndefined

from construction_os.conditions import evaluate
from construction_os.pricing import cents, price

from .words import dollars_in_words

NOT_APPLICABLE = "Not applicable"
PLACEHOLDER = "PLACEHOLDER"
LINEN, WHITE = "F5F2EE", "FFFFFF"
SIGNATURE_ORDER = {
    "before_client": ["executive_cosign", "client", "contractor_pm"],
    "after_client": ["client", "executive_cosign", "contractor_pm"],
}
CHECKLIST_TITLE = "PM checklist — not for the owner"
# Listed first under "Before sending": sent as is, the draft would be wrong.
URGENT_FLAGS = ("contract_terms_not_applied",)
CHECKLIST_TIERS = ("Before sending", "Worth confirming")
HEADER_FIELDS = [("pco_number", "PCO number", "PCO-[###]"), ("date", "Date", "[Date]"), ("project", "Project", "[Project name]"),
                 ("project_address", "Address", "[Project address]"), ("owner", "Owner", "[Owner name]"),
                 ("contract_reference", "Contract", "[Contract]"), ("change_event_ref", "Change event", "[CE-###]"),
                 ("location", "Location", "[Location]"), ("trades", "Trades", "[Trades]"),
                 ("requested_approval_date", "Decision needed by", "[Decision date]"),
                 ("cause_category", "Change type", "[Change type]")]
DATE_FIELDS = {"date", "requested_approval_date"}


@dataclass
class Flag:
    kind: str
    message: str


@dataclass(frozen=True)
class Item:
    """One checklist item: what is missing or wrong, under a key the definition's checklist tiers name."""
    key: str
    text: str


@dataclass
class RenderResult:
    path: Path
    flags: list = field(default_factory=list)
    priced: object = None
    needed: list = field(default_factory=list)
    items: list = field(default_factory=list)
    layout: str = "flat"
    pricing_lines: list = field(default_factory=list)   # the line detail, for the checklist when it's summary_only

    def kinds(self):
        return {flag.kind for flag in self.flags}


def money(value):
    return f"${value:,.2f}"


def shown(field_value, blank):
    """The value, or a bracketed blank the PM fills in."""
    status = field_value.get("status")
    if status == "known":
        value = field_value["value"]
        return ", ".join(value) if isinstance(value, list) else str(value)
    return blank if status == "unknown" else NOT_APPLICABLE


class Needs:
    """Everything unconfirmed, for the PM's checklist. It never appears in the owner's document."""

    def __init__(self):
        self.flags = []
        self.items = []

    def add(self, label, question, kind="missing_input", key="other"):
        text = f"{label}: {question}"
        self.flags.append(Flag(kind, text))
        if all(item.text != text for item in self.items):
            self.items.append(Item(key, text))

    def check(self, label, field_value, key="other"):
        if (field_value or {}).get("status") == "unknown":
            self.add(label, field_value["question"], key=key)

    def note(self, key, text, kind="estimate"):
        """An item whose text stands as written (no "label: question")."""
        self.flags.append(Flag(kind, text))
        if all(item.text != text for item in self.items):
            self.items.append(Item(key, text))

    @property
    def texts(self):
        return [item.text for item in self.items]


def long_date(value):
    when = date.fromisoformat(value)
    return f"{when:%B} {when.day}, {when.year}"


def shown_date(field_value, blank):
    return long_date(field_value["value"]) if field_value.get("status") == "known" else shown(field_value, blank)


def known_items(field_value, needs, label, empty, blank):
    needs.check(label, field_value, key=f"scope.{label.lower().replace(' ', '_')}")
    if field_value.get("status") == "known":
        return list(field_value["value"]) or [empty]
    return [blank]


def parse_days(duration):
    number, unit = duration.split()
    return int(number) * {"day": 1, "week": 7}[unit.rstrip("s")]


def quantity_text(field_value):
    return f"{field_value['value']:g}" if field_value.get("status") == "known" else "[qty]"


def validity(draft, values):
    period = values["PCO-VALIDITY"]
    when = draft["header"]["date"]
    if when.get("status") != "known":
        return period, "[valid-through date]"
    end = date.fromisoformat(when["value"]) + timedelta(days=parse_days(period))
    return period, long_date(end.isoformat())


def presentation(draft, values, flags):
    setting = values["MARKUP-PRESENTATION"]
    chosen = draft["markup_presentation"]
    if chosen["source"] == "contract_term":
        return chosen["value"]
    if chosen["value"] != setting:
        flags.append(Flag("markup_presentation", f"The draft says {chosen['value']}, but the company setting is "
                                                 f"{setting}. Used the setting."))
    return setting


def shown_quantity(line, value):
    return quantity_text(line["quantity"]) if value is None else number_text(value)


def labels(definition):
    return (definition or {}).get("presentation") or {}


def category_label(category, presentation):
    return (presentation.get("category_labels") or {}).get(category) or category.replace("_", " ").capitalize()


def basis_phrase(line, quantity, presentation):
    """The line's basis in the Owner's terms: "(6 hours at the Contractor's rate)", "(lump sum)". Never names a
    quote, a subcontractor or how a figure was estimated; a basis with no phrase (a subcontract line) gets none."""
    template = (presentation.get("basis_phrases") or {}).get(line["basis"])
    if not template:
        return ""
    amount = quantity_text(line["quantity"]) if quantity is None else number_text(quantity)
    return f" ({template.format(quantity=amount, unit=shown(line['unit'], '[unit]'))})"


def number_text(value):
    """Whole quantities print without trailing zeros ("2", not "2.00"); others keep their decimals ("1.57")."""
    value = value if value == cents(value) else cents(value)
    return f"{value.to_integral_value():f}" if value == value.to_integral_value() else f"{value:f}"


LAYOUTS = ("by_division", "flat", "summary_only")


def line_row(line, result, mode, priced, presentation, index):
    hide = mode == "hidden" and line["category"] not in priced.pass_through_categories
    division = line["division"]
    return {
        "kind": "line",
        "code": division["value"] if division.get("status") == "known" else None,
        "division": division["value"] if division.get("status") == "known" else ("[div]" if division.get("status") == "unknown" else ""),
        "category": category_label(line["category"], presentation),
        "description": line["description"] + basis_phrase(line, result.quantity, presentation),
        "quantity": shown_quantity(line, result.quantity),
        "unit": shown(line["unit"], "[unit]"),
        "unit_price": "Included" if hide else (money(result.unit_price) if result.unit_price is not None else "[unit price]"),
        "amount": "Included" if hide else (money(result.amount) if result.amount is not None else "[amount]"),
        "value": result.amount,
        "fill": LINEN if index % 2 else WHITE,
    }


def total_of(rows):
    """The sum of the rows' amounts, or None when any is unknown: a partial sum is never shown as a total."""
    values = [row["value"] for row in rows]
    return None if any(v is None for v in values) else sum(values, Decimal("0.00"))


def shown_amount(value, blank="[amount]"):
    return blank if value is None else signed_money(value)


def summary_rows(priced, mode, wording, work=None, general=None):
    """Subtotal, general conditions when they apply, each markup component, pass-through, discount. Amounts are
    the engine's, to the cent: never rounded. While any line is unknown, they show as bracketed blanks."""
    known = priced.complete
    rows = []
    if mode != "hidden":
        if work is not None:
            rows.append((wording.get("subtotal", "Subtotal"), work))
            if general is not None:
                rows.append((wording.get("general_conditions", "General conditions"), general))
                rows.append((wording.get("subtotal_before_markup", "Subtotal before markup"), priced.subtotal))
        else:
            rows.append((wording.get("subtotal", "Subtotal"), priced.subtotal))
    if mode == "itemized":
        rows += [(f"{c.name} ({c.rate:g}%)", c.amount) for c in priced.components if c.client_visible]
        rest = sum(c.amount for c in priced.components if not c.client_visible)
        if rest:
            rows.append(("Markup", rest))
    elif mode == "combined":
        rows.append(("Markup", priced.markup))
    if priced.discount and mode != "hidden":
        rows.append((f"{priced.discount.name} ({priced.discount.rate:g}%)", -priced.discount.amount))
    if priced.pass_through and mode != "hidden":
        rows.append(("Permits and fees, passed through without markup", priced.pass_through))
    return [{"kind": "summary", "label": label, "amount": shown_amount(amount if known else None), "fill": WHITE}
            for label, amount in rows]


def keep_last(rows):
    if rows:
        rows[-1]["kind"] = "line_keep"
    return rows


def pricing_layout(draft, priced, mode, layout, presentation, total_row):
    """The Owner's pricing per PRICING-PRESENTATION. Returns {"detail": rows, "summary": rows, "lines": rows}.

    by_division: lines grouped by division, each group with its subtotal; general conditions and pass-through
    fees are summary rows. The division subtotals add up to the summary's subtotal. flat: one table of lines,
    summary and total. summary_only: the summary table; the lines go to the PM's checklist.
    """
    wording = presentation.get("pricing") or {}
    shown_lines = [(draft["cost_lines"][p.index], p) for p in priced.lines if p.included]
    lines = [line_row(line, result, mode, priced, presentation, index)
             for index, (line, result) in enumerate(shown_lines)]
    if layout == "flat" or layout not in LAYOUTS:
        detail = keep_last([dict(row) for row in lines]) + summary_rows(priced, mode, wording) + [total_row]
        return {"detail": detail, "summary": [], "lines": lines}
    passed_at = {i for i, (line, _) in enumerate(shown_lines) if line["category"] in priced.pass_through_categories}
    general_at = {i for i, (line, _) in enumerate(shown_lines)
                  if line["category"] == "general_conditions" and i not in passed_at}
    pass_through = [row for i, row in enumerate(lines) if i in passed_at]
    general = [row for i, row in enumerate(lines) if i in general_at]
    work = [row for i, row in enumerate(lines) if i not in passed_at and i not in general_at]
    names = presentation.get("divisions") or {}
    groups = {}
    for row in work:
        groups.setdefault(row["code"], []).append(row)
    detail = []
    for code in sorted(groups, key=lambda c: (c is None, c or "")):
        rows = [dict(row) for row in groups[code]]
        if code is None:
            label = wording.get("division_unassigned", "Division to be assigned")
            subtotal = wording.get("unassigned_subtotal", "Subtotal, division to be assigned")
        else:
            label = wording.get("division_label", "Division {code}: {name}").format(code=code, name=names.get(code, ""))
            label = label.rstrip(": ")
            subtotal = wording.get("division_subtotal", "Division {code} subtotal").format(code=code)
        detail.append({"kind": "division", "label": label})
        detail += keep_last(rows)
        if mode != "hidden":
            detail.append({"kind": "subtotal", "label": subtotal, "amount": shown_amount(total_of(rows))})
    if pass_through:
        detail.append({"kind": "division", "label": wording.get("pass_through_group", "Passed through without markup")})
        rows = [dict(row) for row in pass_through]
        detail += keep_last(rows)
        detail.append({"kind": "subtotal", "label": wording.get("pass_through_subtotal", "Passed through, subtotal"),
                       "amount": shown_amount(total_of(rows))})
    general_total = total_of(general) if general else None
    work_total = total_of(work)
    summary = summary_rows(priced, mode, wording, work=work_total if work_total is not None else Decimal("0"),
                           general=general_total if general else None) + [total_row]
    if layout == "summary_only":
        return {"detail": [], "summary": summary, "lines": lines}
    return {"detail": detail, "summary": summary, "lines": lines}


def signed_money(amount):
    return money(amount) if amount >= 0 else f"-{money(-amount)}"


def total_fields(priced, presentation):
    """The total bar's label and amount, and the total in words and figures for the authorization."""
    total_labels = presentation.get("total_labels") or {"add": "Total", "deduct": "Total"}
    if not priced.complete:
        return total_labels["add"], "[total]", "[total in words]", "[total]"
    deduct = priced.total < 0
    words = dollars_in_words(priced.total)
    return (total_labels["deduct" if deduct else "add"], money(abs(priced.total)),
            f"a deduction of {words}" if deduct else words, money(abs(priced.total)))


def join_names(names):
    names = list(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def markup_basis(draft, priced, mode, presentation):
    """How the markup is calculated. Says "contract" only when a contract term was supplied and cited."""
    if mode == "hidden":
        return ""
    wording = presentation.get("markup_basis") or {
        "company": "Markup per standard pricing: {rates}, each calculated on the subtotal before markup.",
        "contract": "Markup per {reference}: {rates}, each calculated on the subtotal before markup, not compounded.",
        "pass_through": " Permits and fees are passed through without markup."}
    rates = join_names(f"{c.name} {c.rate:g}%" for c in priced.components)
    if priced.contract_reference:
        sentence = wording["contract"].format(reference=priced.contract_reference, rates=rates)
    else:
        sentence = wording["company"].format(rates=rates)
    if priced.pass_through:
        sentence += wording["pass_through"]
    return sentence


def signers(draft, package, needs):
    """The Contractor and the Owner side by side, in the order the co-signature timing sets (the first party on the
    left); the architect below while architect_signs is unknown or true. The preparer is named in the header table."""
    timing = package.values["AUTH-WRITTEN.cosign_timing"]
    order = list(SIGNATURE_ORDER[timing])
    if draft["signatures"]["order"] != order:
        needs.flags.append(Flag("signature_order", f"The draft's signature order doesn't match {timing}. Used the setting."))
    titles = labels(package.definition).get("signature_titles") or {}
    company = package.company("name")
    executive = package.values["role.Executive"]["name"]
    owner = draft["header"]["owner"]

    def party(title, name="", role=""):
        return {"title": title, "title_upper": title.upper(), "name": name, "role": role}

    parties = {
        "executive_cosign": party(titles.get("executive_cosign", "{company}").format(company=company), executive or "",
                                  package.company("signatory", "title") or ""),
        "client": party(titles.get("client", "Owner"), owner["value"] if owner.get("status") == "known" else ""),
    }
    signing = [key for key in order if key in parties]
    architect = None
    status = draft["architect_signs"]
    if status.get("status") == "unknown":   # An extra block is easy to delete; a missing one is easy to miss.
        architect = party(titles.get("architect_if_required", "Architect (if required by contract)"))
        needs.add("Architect", status["question"], key="architect")
    elif status["value"]:
        architect = party(titles.get("architect", "Architect"))
        needs.add("Architect", "Who signs for the architect?", key="architect")
    return {"left": parties[signing[0]], "right": parties[signing[1]], "architect": architect, "order": signing}


def preparer(package, needs):
    pm = package.values["role.ProjectManager"]["name"]
    if not pm:
        needs.add("Prepared by", "Which project manager prepared this?", key="prepared_by")
    return pm or "[Project manager name]"


def schedule_fields(draft, needs, presentation):
    schedule = draft["schedule"]
    extension, authorization = schedule["extension_days"], schedule["authorization_by"]
    needs.check("Schedule impact", extension, key="schedule.extension_days")
    needs.check("Authorization-by date", authorization, key="schedule.authorization_by")
    by = shown_date(authorization, "[date]")
    no_change = presentation.get("schedule_no_change")
    if extension.get("status") == "known" and extension["value"] == 0 and no_change:
        return {"rows": [{"label": "Schedule impact", "value": no_change["impact"]}], "activities": [],
                "sentence": no_change["sentence"].format(date=by)}
    added = presentation.get("schedule_added")
    needs.check("Critical path", schedule["critical_path"], key="schedule.critical_path")
    needs.check("Completion date", schedule["completion_date"], key="schedule.completion_date")
    if extension.get("status") != "known":
        days = "[days]"
    elif extension["value"] == 0:
        days = "None"
    else:
        days = f"{extension['value']:g} calendar day{'' if extension['value'] == 1 else 's'} added"
    critical = schedule["critical_path"]
    condition = NOT_APPLICABLE if authorization.get("status") == "not_applicable" else \
        f"Schedule assumes signed authorization by {by}."
    rows = [
        {"label": "Calendar days", "value": days},
        {"label": "Critical path affected",
         "value": critical["value"].capitalize() if critical.get("status") == "known" else "[yes/no]"},
        {"label": "Completion date", "value": shown(schedule["completion_date"], "[completion date]")},
    ]
    if not added:
        return {"rows": rows + [{"label": "Condition", "value": condition}], "sentence": "", "activities": []}
    activities = []
    if extension.get("status") == "known" and extension["value"] > 0:
        for item in schedule.get("activities") or []:
            days = number_text(Decimal(str(item["duration_days"])))
            activities.append({"activity": item["activity"],
                               "duration": f"{days} day{'' if days == '1' else 's'}"})
    return {"rows": rows, "activities": activities, "activity_label": added.get("activity", "Activity"),
            "duration_label": added.get("duration", "Duration"), "sentence": added["sentence"].format(date=by)}


def description_paragraph(draft, presentation):
    """One paragraph in contract voice. For the causes the company records, it opens with the Owner's request and
    the draft's text continues the sentence ("the Contractor proposes …")."""
    description = draft["description"]
    if description.get("status") != "known":
        return shown(description, "[Description]")
    opening = presentation.get("description_opening")
    cause = draft["header"]["cause_category"]
    if not opening or cause.get("status") != "known" or cause["value"] not in opening.get("applies_to", []):
        return description["value"]
    record = draft.get("request_record") or {}
    lead = opening["known"].format(record=record["value"]) if record.get("status") == "known" else opening["unknown"]
    return lead + description["value"]


def party(line, presentation):
    name = (presentation.get("trade_parties") or {}).get(line["trade"])
    return f"{name[0].upper()}{name[1:]}'s" if name else f"{line['trade'].capitalize()}"


def backup_items(draft, needs, presentation):
    """Backup is filed with the PCO, never attached (CHG-53 2). The PM confirms it's on file."""
    wording = presentation.get("backup") or {}
    filed = {item["kind"] for item in draft.get("backup") or []}
    quoted = [line for line in draft["cost_lines"]
              if line["basis"] == "quote" and line["evidence"]["source"] != "supplied_quote"]
    parties = [party(line, presentation) for line in quoted]
    for line, name in zip(quoted, parties):
        if "quote" not in wording:
            break
        text = wording["quote"].format(Party=name.removesuffix("'s"), party=name)
        if not name.endswith("'s"):
            text = text.replace(f"{name}'s quote", f"{name} quote")
        if parties.count(name) > 1:
            text += f" ({line['description']})"
        label, _, rest = text.partition(": ")
        needs.add(label, rest, kind="backup", key="backup.quote")
    opening = presentation.get("description_opening") or {}
    cause = draft["header"]["cause_category"]
    owner_request = cause.get("status") == "known" and cause["value"] in opening.get("applies_to", [])
    if owner_request and "correspondence" not in filed and "correspondence" in wording:
        label, _, rest = wording["correspondence"].partition(": ")
        needs.add(label, rest, kind="backup", key="backup.correspondence")


def selection_lines(draft, needs, presentation):
    """Owner selections (PRO-34) referenced in the scope by ID. The selection itself is never attached."""
    wording = presentation.get("selection") or {}
    lines = []
    for line in draft["cost_lines"]:
        selection = line.get("selection")
        if not selection or not wording:
            continue
        status, items = selection["status"], selection["items"]
        ident = selection["id"]["value"] if selection["id"].get("status") == "known" else None
        when = shown_date(selection["date"], "[date]")
        if status in ("approved", "approved_as_noted", "issued_with_pco") and ident:
            lines.append(wording[status].format(items=items, id=ident, date=when))
            if status == "approved_as_noted":
                needs.add("Owner selection", wording["confirm_notes"].format(id=ident), kind="selection",
                          key="selection.notes")
        elif status in ("pending", "revised") and ident:
            lines.append(wording["not_approved"].format(items=items, id=ident))
            needs.add("Owner selection", wording["not_approved_question"].format(id=ident, status=status),
                      kind="selection", key="selection")
        else:
            lines.append(wording["unknown"].format(items=items))
            needs.add("Owner selection", wording["question"].format(items=items[0].lower() + items[1:]),
                      kind="selection", key="selection")
    return lines


def scope_items(draft, needs, presentation):
    """The work includes: main work first, supporting work after, then the Owner selections that govern it."""
    items = known_items(draft["scope"]["inclusions"], needs, "Added scope", "None stated", "[Added scope]")
    return items + selection_lines(draft, needs, presentation)


def deleted_items(draft, needs):
    """Shown only when there is some; an unknown answer is a question for the PM, never a blank on the PCO."""
    deleted = draft["scope"]["deleted_scope"]
    needs.check("Deleted scope", deleted, key="scope.deleted_scope")
    return list(deleted["value"]) if deleted.get("status") == "known" else []


def assumption_groups(draft, needs, presentation):
    """Section 4: Owner-facing assumptions by group, job-specific exclusions, then the standard clarifications.
    Each group appears only when it has items."""
    headings = presentation.get("assumption_headings") or {
        "scope": "Assumptions", "existing_conditions": "Existing conditions", "access_logistics": "Access and logistics",
        "not_included": "Not included", "clarifications": "Clarifications and exclusions"}
    grouped = {"scope": [], "existing_conditions": [], "access_logistics": []}
    for item in draft["scope"]["assumptions"]:
        if isinstance(item, str):
            grouped["scope"].append(item)
        else:
            grouped[item["kind"]].append(item["text"])
    grouped["not_included"] = list(draft["scope"].get("not_included") or [])
    clarifications = clarification_lines(draft, needs)
    grouped["clarifications"] = [] if clarifications == ["None stated"] else clarifications
    return [{"title": headings[key], "entries": grouped[key]} for key in headings if grouped.get(key)]


def exhibits(draft, needs, presentation):
    """Owner-facing attachments only (a drawing or layout), and only when actually supplied. Backup and Owner
    selections are never exhibits. Lettered in the order shown."""
    standard = (presentation.get("exhibits") or {}).get("standard", [])
    facing = {e["input_kind"] for e in standard if e.get("owner_facing")}
    shown_items = []
    for item in draft["attachments"]:
        if item.get("kind") not in facing:
            continue
        if item.get("supplied") is True:
            letter = chr(ord("A") + len(shown_items))
            shown_items.append({"mark": "☒", "letter": letter, "label": f"Exhibit {letter} — {item['description']}"})
        else:
            wording = (presentation.get("exhibits") or {}).get("requested_not_supplied", "{title}: supply one if needed.")
            label, _, rest = wording.format(title=item["description"]).partition(": ")
            needs.add(label, rest, kind="exhibit", key=f"attachment.{item['kind']}")
    return shown_items


def num(value):
    text = f"{Decimal(str(value)).normalize():f}"
    return text


def estimating_items(draft, priced, needs):
    """What the PM checks in an estimated draft: every estimated measurement, quantity and duration, and every
    cost-history price with its item, range and data points. None of it reaches the Owner's PCO."""
    estimated = [m for m in draft.get("measurements") or [] if m["source"] == "estimate"]
    for m in estimated:
        size = f"{num(m['length_ft'])} × {num(m['width_ft'])} ft" + (f", {num(m['height_ft'])} ft high" if m.get("height_ft") else "")
        needs.note("estimate.measurement", f"Estimated measurement: {m['label']}, {size}. Basis: {m['basis']}")
    if estimated:
        needs.note("measurement.confirm", "Confirm field dimensions before sending.")
    by_id = {m["id"]: m for m in draft.get("measurements") or []}
    for line in draft["cost_lines"]:
        name = line["description"]
        takeoff = line.get("takeoff")
        if takeoff and takeoff.get("formula"):
            sources = {by_id[p["measurement"]]["source"] for p in takeoff["parts"] if p["measurement"] in by_id}
            kind = "estimated measurements" if "estimate" in sources else "measurements from the notes"
            needs.note("estimate.quantity", f"Quantity from takeoff ({kind}): {name}: {takeoff['formula']}.")
        duration = line.get("duration") or {}
        if duration:
            amount = f"{num(duration['days'])} days" if "days" in duration else f"{num(duration['hours'])} hours"
            needs.note("estimate.duration", f"Estimated duration: {name}: {amount}. Basis: {duration['basis']}")
        if line.get("quantity_basis"):
            quantity = shown(line["quantity"], "[qty]")
            needs.note("estimate.quantity", f"Estimated quantity: {name}: {quantity} {shown(line['unit'], '')}. "
                                            f"Basis: {line['quantity_basis']}")
    for result in priced.lines:
        evidence = result.evidence
        if evidence is not None and evidence.source == "cost_history" and evidence.note:
            needs.note("cost_history", f"{result.description}: {evidence.note}", kind="cost_history")
    labor_trades = {line["trade"] for line in draft["cost_lines"] if line["category"] == "labor"}
    for line in draft["cost_lines"]:
        chosen = line.get("cost_item") or {}
        if line["category"] == "subcontractors" and chosen.get("component") == "total" and line["trade"] in labor_trades:
            needs.note("cost_history.double_count",
                       f"Possible double count: \"{line['description']}\" is priced from cost history as installed "
                       f"work, and company labor is also carried for {line['trade']}. Keep one.", kind="cost_history")


def internal_notes(draft, needs, presentation):
    label = presentation.get("internal_note", "Note for the PM")
    for note in draft.get("internal_notes") or []:
        needs.add(label, note, kind="internal_note", key="note")


def internal_rules(draft, package, needs):
    """Internal rules the definition depends on that apply to this draft, e.g. a spending approval."""
    for item in package.definition.get("depends_on") or []:
        rule = item["id"]
        if item.get("audience") != "internal" or "." in rule or rule in package.values:
            continue
        if evaluate(item.get("applies_when"), draft, package.values) is True:
            needs.add(rule, f"applies to this change ({item['applies_when'].replace('_', ' ')}). Confirm the approval "
                            f"it requires before sending.", kind="internal_rule", key=f"rule.{rule}")


def clarification_lines(draft, needs):
    lines = []
    for item in draft["clarifications_and_exclusions"]:
        if item["disposition"] == "not_applicable":
            continue
        lines.append(item["text"])
        if item["disposition"] == "open_question" and item["prompt"] != "permits":
            needs.add("Clarification", item["text"], key="clarification")
    return lines or ["None stated"]


def check_clarifications(draft, definition, flags):
    """Every prompt for every trade. A coverage gap is a flag; it never blocks. The company's default text applies
    to every trade, so using it considers the prompt for all of them, whatever trades the item lists."""
    trades = {line["trade"] for line in draft["cost_lines"]}
    for prompt in definition.get("clarification_prompts") or []:
        default = (prompt.get("default") or {}).get("text")
        covered = set()
        for item in draft["clarifications_and_exclusions"]:
            if item["prompt"] == prompt["id"]:
                applies_to_all = "all" in item["trades"] or (default and item["text"] == default)
                covered |= trades if applies_to_all else set(item["trades"])
        for trade in sorted(trades - covered):
            flags.append(Flag("prompt_not_considered", f"{prompt['prompt']}: not considered for {trade}."))


def check_permits(draft, definition, needs):
    permit_trades = definition.get("permit_trades") or []
    for line in draft["cost_lines"]:
        if line["trade"] in permit_trades and line["permit"] != "included_in_quote":
            needs.add(f"{line['trade'].capitalize()} permit", "Does the quote include the permit? If not, who pulls it, and at what cost?",
                      kind="permit_question", key="permit")


def identity_value(package, path):
    data = package.profile.get("company") or {}
    for part in path.split("."):
        data = (data or {}).get(part)
    return data


def header_rows(draft, package, needs, valid_through, prepared_by):
    header = draft["header"]
    extra = (labels(package.definition).get("header_rows") or {})
    for name, label, _blank in HEADER_FIELDS:
        needs.check(label, header[name], key=f"header.{name}")

    def field(name):
        _, label, blank = next(f for f in HEADER_FIELDS if f[0] == name)
        value = shown_date(header[name], blank) if name in DATE_FIELDS else shown(header[name], blank)
        return label, value

    pairs = [field("pco_number"), field("date"), field("project"), ("Valid through", valid_through),
             field("project_address"), field("change_event_ref"), field("owner"), field("contract_reference"),
             field("location"), field("trades"), ("Contractor", package.company("legal_name")),
             field("requested_approval_date"), field("cause_category")]
    if "prepared_by" in extra:
        row = extra["prepared_by"]
        pairs.append((row["label"], f"{prepared_by}, {row['role']}" if row.get("role") else prepared_by))
    requested = extra.get("requested_by")
    cause = header["cause_category"]
    if requested and cause.get("status") == "known" and cause["value"] in requested.get("applies_to", []):
        pairs.append((requested["label"], requested["value"]))
    for item in package.definition.get("header_identity") or []:
        value = identity_value(package, item["path"])
        pairs.append((item["label"], value or f"[{item['label']}]"))
    rows = []
    for index in range(0, len(pairs), 2):
        a = pairs[index]
        b = pairs[index + 1] if index + 1 < len(pairs) else ("", "")
        rows.append({"label_a": a[0], "value_a": a[1], "label_b": b[0], "value_b": b[1]})
    return rows


def document_text(path):
    """All text in the body, tables, headers and footers."""
    document = Document(path)
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            seen = []
            for cell in row.cells:
                if cell._tc not in seen:
                    seen.append(cell._tc)
                    parts.append(cell.text)
    for section in document.sections:
        parts += [p.text for p in section.header.paragraphs] + [p.text for p in section.footer.paragraphs]
    return "\n".join(parts)


def unfilled_fields(path):
    """Template tags left in any part of the file."""
    with zipfile.ZipFile(path) as archive:
        found = []
        for name in archive.namelist():
            if name.endswith(".xml"):
                text = re.sub(r"<[^>]+>", "", archive.read(name).decode("utf-8", "ignore"))
                found += re.findall(r"\{\{.*?\}\}|\{%.*?%\}", text)
    return found


def render(draft, package, out):
    """Fill the company's Word template from a draft. Returns the file, its flags and the computed prices."""
    needs = Needs()
    flags = needs.flags
    values = package.values
    header = draft["header"]
    wording = labels(package.definition)
    priced = price(draft, package)
    flags += [Flag(f.code, f.message) for f in priced.flags]
    mode = presentation(draft, values, flags)
    total_label, total_amount, total_words, total_figures = total_fields(priced, wording)
    layout = values.get("PRICING-PRESENTATION", "flat")
    total_row = {"kind": "total", "label": total_label, "amount": total_amount}
    pricing = pricing_layout(draft, priced, mode, layout, wording, total_row)
    period, valid_through = validity(draft, values)
    template = DocxTemplate(package.template)
    rows = header_rows(draft, package, needs, valid_through, preparer(package, needs))
    needs.check("Description", draft["description"], key="description")
    needs.check("Reason", draft["reason"], key="reason")
    context = {
        "logo": InlineImage(template, str(package.logo), height=Inches(0.42)),
        "company": {"name": package.company("name")},
        "company_name_upper": package.company("name").upper(),
        "company_address": package.company("address"),
        "company_email": package.company("email"),
        "generation_reference": f"Generated from package {package.reference}",
        "document_id": shown(header["pco_number"], "PCO-[###]"),
        "issue_date": shown_date(header["date"], "[date]"),
        "header_rows": rows,
        "validity_period": period,
        "validity_date": valid_through,
        "description": description_paragraph(draft, wording),
        "logo_large": InlineImage(template, str(package.logo), height=Inches(0.85)),
        "work_includes": wording.get("work_includes", "The work includes:"),
        "work_items": scope_items(draft, needs, wording),
        "basis_of_quantities": list(draft.get("basis_of_quantities") or []),
        "basis_of_quantities_label": wording.get("basis_of_quantities", "Basis of quantities"),
        "deleted_scope": deleted_items(draft, needs),
        "assumption_groups": assumption_groups(draft, needs, wording),
        "pricing": {"detail": pricing["detail"], "summary": pricing["summary"]},
        "total_words": total_words,
        "total_figures": total_figures,
        "markup_basis": markup_basis(draft, priced, mode, wording),
        "schedule": schedule_fields(draft, needs, wording),
        "exhibits": exhibits(draft, needs, wording),
        "signers": signers(draft, package, needs),
    }
    backup_items(draft, needs, wording)
    internal_notes(draft, needs, wording)
    estimating_items(draft, priced, needs)
    for line in draft["cost_lines"]:
        for name in ("quantity", "unit", "unit_price", "division"):
            needs.check(f"{line['description']}, {name.replace('_', ' ')}", line[name], key=f"line.{name}")
        needs.check(f"{line['description']}, quote reference", line["evidence"]["reference"], key="line.reference")
        needs.check(f"{line['description']}, quote date", line["evidence"]["date"], key="line.quote_date")
    needs.add("PCCO number", "Which prime contract change order number will incorporate this PCO once it is executed?",
              kind="pcco_number", key="pcco_number")
    check_clarifications(draft, package.definition, flags)
    check_permits(draft, package.definition, needs)
    internal_rules(draft, package, needs)
    if not priced.complete:
        flags.append(Flag("incomplete_pricing", "A quantity or price is unknown, so the total is left blank."))
    if priced.contract_reference and priced.complete:
        company = price(draft, package, use_contract=False)
        flags.append(Flag("contract_pricing", f"Priced per {priced.contract_reference}: {money(priced.total)}. "
                                              f"Company pricing would give {money(company.total)}."))
    template.render(context, Environment(undefined=StrictUndefined), autoescape=True)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    template.save(out)
    for text in document_text(out).split("\n"):
        if PLACEHOLDER in text:
            flags.append(Flag("placeholder", f"The document contains a placeholder: {text.strip()}"))
    for tag in unfilled_fields(out):
        flags.append(Flag("unfilled_field", f"A template field wasn't filled: {tag}"))
    return RenderResult(out, flags, priced, needs.texts, list(needs.items), layout, pricing["lines"])


FIELD_PHRASES = {   # A model question naming one of these restates a form field the checklist already asks about.
    "header.project": ("project name",),
    "header.project_address": ("project address", "address of the project", "what is the address"),
    "header.owner": ("owner's name", "owner name", "who is the owner", "client's name", "client name", "who is the client"),
    "header.pco_number": ("pco number",),
    "header.change_event_ref": ("change event",),
    "pcco_number": ("pcco",),
    "backup.quote": ("written quote", "quotes in writing", "quote in writing", "copy of the quote", "copies of the quotes"),
    "permit": ("permit",),
    "selection": ("owner selection",),
}
ITEM_FLAGS = ("missing_input", "permit_question", "internal_rule", "pcco_number", "backup", "selection", "exhibit",
              "internal_note", "estimate", "cost_history")


def normalized(text):
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def restates(question, items):
    """True when a model open question repeats an item the form already raises."""
    plain = normalized(question)
    for item in items:
        asked = normalized(item.text.split(": ", 1)[-1])
        if plain == asked or plain == normalized(item.text):
            return True
        phrases = FIELD_PHRASES.get(item.key, ())
        if item.key.startswith("rule."):
            phrases = (item.key.removeprefix("rule."),)     # the rule's own ID, e.g. APPROVAL-SPEND
        if any(normalized(phrase) in plain for phrase in phrases):
            return True
    return False


def checklist_items(result, draft, definition):
    """The PM checklist in two tiers, one item per missing fact. Tiers come from the definition."""
    items = list(result.items)
    for question in draft.get("open_questions") or []:
        if not restates(question, items):
            items.append(Item("open_question", question))
    for flag in result.flags:
        if flag.kind not in ITEM_FLAGS:
            items.append(Item(flag.kind, flag.message))
    unique = []
    for item in items:
        if all(normalized(item.text) != normalized(seen.text) for seen in unique):
            unique.append(item)
    before = set(((definition or {}).get("checklist") or {}).get("before_sending") or [])
    first = [i for i in unique if i.key in URGENT_FLAGS]
    sending = first + [i for i in unique if i.key in before and i.key not in URGENT_FLAGS]
    confirming = [i for i in unique if i.key not in before and i.key not in URGENT_FLAGS]
    return sending, confirming


def render_checklist(result, draft, package, out):
    """The PM's list of every open question and flag, in two tiers. Separate from the Owner's document."""
    sending, confirming = checklist_items(result, draft, package.definition)
    font = Document(package.template).styles["Normal"].font.name
    document = Document()
    document.styles["Normal"].font.name = font
    document.styles["Normal"].font.size = Pt(10.5)

    def line(text, size=10.5, bold=False, after=3):
        item = document.add_paragraph()
        item.paragraph_format.space_after = Pt(after)
        run = item.add_run(text)
        run.font.name, run.font.size, run.font.bold = font, Pt(size), bold

    line(CHECKLIST_TITLE, size=15, bold=True, after=2)
    line(f"For {result.path.name}.", after=10)
    for tier, items in zip(CHECKLIST_TIERS, (sending, confirming)):
        line(tier, size=11.5, bold=True)
        for item in items or [Item("", "None")]:
            line(f"☐  {item.text}" if item.key else item.text, bold=item.key in URGENT_FLAGS)
    if result.layout == "summary_only" and result.pricing_lines:   # the Owner sees the summary only
        title = ((package.definition.get("presentation") or {}).get("pricing") or {}).get(
            "detail_appendix", "Pricing detail (internal)")
        line(title, size=11.5, bold=True)
        for row in result.pricing_lines:
            line(f"{row['division']} · {row['category']} · {row['description']}: {row['quantity']} {row['unit']} × "
                 f"{row['unit_price']} = {row['amount']}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    document.save(out)
    return out
