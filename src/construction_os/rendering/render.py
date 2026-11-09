"""Render a structured PCO draft into the company's Word template. Flags never block the output."""
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
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


def line_attachment(line):
    field_value = line["attachment"]
    if field_value.get("status") == "known":
        return field_value["value"]
    return "[att.]" if field_value.get("status") == "unknown" else ""


def shown_quantity(line, value):
    if value is None:
        return quantity_text(line["quantity"])
    return f"{value:g}" if value == cents(value) else f"{cents(value):g}"


def labels(definition):
    return (definition or {}).get("presentation") or {}


def category_label(category, presentation):
    return (presentation.get("category_labels") or {}).get(category) or category.replace("_", " ").capitalize()


def basis_phrase(line, quantity, presentation):
    """The line's basis, from its evidence, in the company's words: "(per electrician's quote)"."""
    phrases = presentation.get("basis_phrases") or {}
    party = (presentation.get("trade_parties") or {}).get(line["trade"])
    basis = line["basis"]
    if basis == "quote":
        template = phrases.get("quote") if party else phrases.get("quote_no_party")
    else:
        template = phrases.get(basis)
    if not template:
        return ""
    amount = quantity_text(line["quantity"]) if quantity is None else number_text(quantity)
    return f" ({template.format(party=party, trade=line['trade'], quantity=amount, unit=shown(line['unit'], '[unit]'))})"


def number_text(value):
    """Whole quantities print without trailing zeros ("2", not "2.00"); others keep their decimals ("1.57")."""
    value = value if value == cents(value) else cents(value)
    return f"{value.to_integral_value():f}" if value == value.to_integral_value() else f"{value:f}"


def pricing_rows(draft, priced, mode, presentation):
    lines = []
    shown_lines = [(draft["cost_lines"][p.index], p) for p in priced.lines if p.included]
    for index, (line, result) in enumerate(shown_lines):
        amount, unit_price = result.amount, result.unit_price
        hide = mode == "hidden" and line["category"] not in priced.pass_through_categories
        division = line["division"]
        lines.append({
            "division": division["value"] if division.get("status") == "known" else ("[div]" if division.get("status") == "unknown" else ""),
            "category": category_label(line["category"], presentation),
            "description": line["description"] + basis_phrase(line, result.quantity, presentation),
            "attachment": line_attachment(line),
            "quantity": shown_quantity(line, result.quantity),
            "unit": shown(line["unit"], "[unit]"),
            "unit_price": "Included" if hide else (money(unit_price) if unit_price is not None else "[unit price]"),
            "amount": "Included" if hide else (money(amount) if amount is not None else "[amount]"),
            "fill": LINEN if index % 2 else WHITE,
        })
    rows = []
    if mode != "hidden":
        rows.append(("Subtotal", priced.subtotal))
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
    summary = [{"label": label, "amount": signed_money(amount), "fill": WHITE} for label, amount in rows]
    return lines, summary


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


def markup_basis(draft, priced, mode, company_name):
    """How the markup is calculated. Says "contract" only when a contract term was supplied and cited."""
    if mode == "hidden":
        return ""
    rates = join_names(f"{c.name} {c.rate:g}%" for c in priced.components)
    if priced.contract_reference:
        sentence = f"Markup per {priced.contract_reference}: {rates}, each calculated on the direct-cost subtotal, not compounded."
    else:
        owner = f"{company_name}'" if company_name.endswith("s") else f"{company_name}'s"
        sentence = f"Markup per {owner} standard pricing: {rates}, each calculated on the direct-cost subtotal."
    if priced.pass_through:
        sentence += " Permits and fees are passed through without markup."
    return sentence


def signature_blocks(draft, package, needs):
    """One block per party, in the order the company's co-signature timing sets."""
    timing = package.values["AUTH-WRITTEN.cosign_timing"]
    order = list(SIGNATURE_ORDER[timing])
    if draft["signatures"]["order"] != order:
        needs.flags.append(Flag("signature_order", f"The draft's signature order doesn't match {timing}. Used the setting."))
    titles = labels(package.definition).get("signature_titles") or {}
    company = package.company("name")
    executive = package.values["role.Executive"]["name"]
    pm = package.values["role.ProjectManager"]["name"]
    owner = draft["header"]["owner"]
    parties = {
        "executive_cosign": (titles.get("executive_cosign", "{company}").format(company=company), executive or "",
                             package.company("signatory", "title") or ""),
        "client": (titles.get("client", "Owner"), owner["value"] if owner.get("status") == "known" else "", ""),
        "architect": (titles.get("architect_if_required", "Architect (if required by contract)"), "", ""),
        "contractor_pm": (titles.get("contractor_pm", "Prepared by"), pm or "", ""),
    }
    architect = draft["architect_signs"]
    if architect.get("status") == "unknown":   # An extra block is easy to delete; a missing one is easy to miss.
        order.insert(order.index("contractor_pm"), "architect")
        needs.add("Architect", architect["question"], key="architect")
    elif architect["value"]:
        parties["architect"] = (titles.get("architect", "Architect"), "", "")
        order.insert(order.index("contractor_pm"), "architect")
        needs.add("Architect", "Who signs for the architect?", key="architect")
    if not pm:
        needs.add("Prepared by", "Which project manager prepared this?", key="prepared_by")
    return [{"key": key, "title": parties[key][0], "title_upper": parties[key][0].upper(), "name": parties[key][1],
             "role": parties[key][2]} for key in order]


def schedule_fields(draft, needs, presentation):
    schedule = draft["schedule"]
    extension, authorization = schedule["extension_days"], schedule["authorization_by"]
    needs.check("Schedule impact", extension, key="schedule.extension_days")
    needs.check("Authorization-by date", authorization, key="schedule.authorization_by")
    by = shown_date(authorization, "[date]")
    no_change = presentation.get("schedule_no_change")
    if extension.get("status") == "known" and extension["value"] == 0 and no_change:
        return {"rows": [{"label": "Schedule impact", "value": no_change["impact"]}],
                "sentence": no_change["sentence"].format(date=by)}
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
    return {"rows": [
        {"label": "Calendar days", "value": days},
        {"label": "Critical path affected",
         "value": critical["value"].capitalize() if critical.get("status") == "known" else "[yes/no]"},
        {"label": "Completion date", "value": shown(schedule["completion_date"], "[completion date]")},
        {"label": "Condition", "value": condition},
    ], "sentence": ""}


def request_line(draft, needs, presentation):
    """How the Owner's request arrived, for the causes the company records it for."""
    wording = presentation.get("request_line")
    record = draft.get("request_record")
    cause = draft["header"]["cause_category"]
    if not wording or (record or {}).get("status") == "not_applicable":
        return ""
    if cause.get("status") == "known" and cause["value"] not in wording.get("applies_to", []):
        return ""
    if (record or {}).get("status") == "known":
        return wording["known"].format(record=record["value"])
    needs.add("Request record", (record or {}).get("question") or
              "How and when did the Owner request this change (email or meeting, and its date)?", key="request_record")
    return wording["unknown"]


def exhibits(draft, needs):
    """Lettered exhibits. Checked only when a file was actually supplied; never otherwise."""
    items = []
    for item in draft["attachments"]:
        supplied = item.get("supplied") is True
        items.append({"mark": "☒" if supplied else "☐", "letter": item["letter"],
                      "label": f"Exhibit {item['letter']} — {item['description']}"})
        if not supplied:
            kind = item.get("kind") or "quote"
            needs.add(f"Exhibit {item['letter']} ({item['description']})", "Attach the file.", key=f"attachment.{kind}")
    return items or [{"mark": "", "letter": "", "label": "None"}]


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
    """Every prompt for every trade. A coverage gap is a flag; it never blocks."""
    trades = {line["trade"] for line in draft["cost_lines"]}
    for prompt in definition.get("clarification_prompts") or []:
        covered = set()
        for item in draft["clarifications_and_exclusions"]:
            if item["prompt"] == prompt["id"]:
                covered |= trades if "all" in item["trades"] else set(item["trades"])
        for trade in sorted(trades - covered):
            flags.append(Flag("prompt_not_considered", f"{prompt['prompt']}: not considered for {trade}."))


def check_permits(draft, definition, needs):
    permit_trades = definition.get("permit_trades") or []
    for line in draft["cost_lines"]:
        if line["trade"] in permit_trades and line["permit"] != "included_in_quote":
            needs.add(f"{line['trade'].capitalize()} permit", "Does the quote include the permit? If not, who pulls it, and at what cost?",
                      kind="permit_question", key="permit")


def check_attachment_letters(draft, flags):
    letters = {item["letter"] for item in draft["attachments"]}
    for line in draft["cost_lines"]:
        letter = line["attachment"].get("value") if line["attachment"].get("status") == "known" else None
        if letter and letter not in letters:
            flags.append(Flag("attachment_letter", f"{line['description']} points to attachment {letter}, which isn't listed."))


def identity_value(package, path):
    data = package.profile.get("company") or {}
    for part in path.split("."):
        data = (data or {}).get(part)
    return data


def header_rows(draft, package, needs, valid_through):
    header = draft["header"]
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
    lines, summary = pricing_rows(draft, priced, mode, wording)
    total_label, total_amount, total_words, total_figures = total_fields(priced, wording)
    period, valid_through = validity(draft, values)
    template = DocxTemplate(package.template)
    rows = header_rows(draft, package, needs, valid_through)
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
        "description": shown(draft["description"], "[Description]"),
        "reason": shown(draft["reason"], "[Reason]"),
        "request_line": request_line(draft, needs, wording),
        "inclusions": known_items(draft["scope"]["inclusions"], needs, "Added scope", "None stated", "[Added scope]"),
        "deleted_scope": known_items(draft["scope"]["deleted_scope"], needs, "Deleted scope", "None", "[Deleted scope, or None]"),
        "assumptions": draft["scope"]["assumptions"] or ["None stated"],
        "clarifications": clarification_lines(draft, needs),
        "lines": lines,
        "summary": summary,
        "total_label": total_label,
        "total_amount": total_amount,
        "total_words": total_words,
        "total_figures": total_figures,
        "markup_basis": markup_basis(draft, priced, mode, package.company("name")),
        "schedule": schedule_fields(draft, needs, wording),
        "exhibits": exhibits(draft, needs),
        "signature_blocks": signature_blocks(draft, package, needs),
    }
    for line in draft["cost_lines"]:
        for name in ("quantity", "unit", "unit_price", "division", "attachment"):
            needs.check(f"{line['description']}, {name.replace('_', ' ')}", line[name], key=f"line.{name}")
        needs.check(f"{line['description']}, quote reference", line["evidence"]["reference"], key="line.reference")
        needs.check(f"{line['description']}, quote date", line["evidence"]["date"], key="line.quote_date")
    needs.add("PCCO number", "Which prime contract change order number will incorporate this PCO once it is executed?",
              kind="pcco_number", key="pcco_number")
    check_clarifications(draft, package.definition, flags)
    check_permits(draft, package.definition, needs)
    check_attachment_letters(draft, flags)
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
    return RenderResult(out, flags, priced, needs.texts, list(needs.items))


FIELD_PHRASES = {   # A model question naming one of these restates a form field the checklist already asks about.
    "header.project": ("project name",),
    "header.project_address": ("project address", "address of the project", "what is the address"),
    "header.owner": ("owner's name", "owner name", "who is the owner", "client's name", "client name", "who is the client"),
    "header.pco_number": ("pco number",),
    "header.change_event_ref": ("change event",),
    "pcco_number": ("pcco",),
}
ITEM_FLAGS = ("missing_input", "permit_question", "internal_rule", "pcco_number")


def normalized(text):
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def restates(question, items):
    """True when a model open question repeats an item the form already raises."""
    plain = normalized(question)
    for item in items:
        asked = normalized(item.text.split(": ", 1)[-1])
        if plain == asked or plain == normalized(item.text):
            return True
        if any(normalized(phrase) in plain for phrase in FIELD_PHRASES.get(item.key, ())):
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
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    document.save(out)
    return out
