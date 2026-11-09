"""Render a structured PCO draft into the company's Word template. Flags never block the output."""
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from docx import Document
from docx.shared import Inches, Pt
from docxtpl import DocxTemplate, InlineImage, RichText
from jinja2 import Environment, StrictUndefined

from construction_os.pricing import cents, price

NOT_APPLICABLE = "Not applicable"
PLACEHOLDER = "PLACEHOLDER"
LINEN, WHITE = "F5F2EE", "FFFFFF"
SIGNATURE_ORDER = {
    "before_client": ["executive_cosign", "client", "contractor_pm"],
    "after_client": ["client", "executive_cosign", "contractor_pm"],
}
CHECKLIST_TITLE = "PM checklist — not for the client"
# Flags the PM must read before anything else: sending the draft as is would be wrong.
URGENT_FLAGS = ("contract_terms_not_applied",)
BODY_SIZE, SMALL_SIZE = 19, 15  # half-points
HEADER_FIELDS = [("pco_number", "PCO number", "[PCO-###]"), ("date", "Date", "[Date]"), ("project", "Project", "[Project name]"),
                 ("project_address", "Address", "[Project address]"), ("owner", "Client", "[Client name]"),
                 ("contract_reference", "Contract", "[Contract]"), ("change_event_ref", "Change event", "[CE-###]"),
                 ("location", "Location", "[Location]"), ("trades", "Trades", "[Trades]"),
                 ("requested_approval_date", "Decision needed by", "[Decision date]"),
                 ("cause_category", "Change type", "[Change type]")]
DATE_FIELDS = {"date", "requested_approval_date"}
BASIS_LABELS = {"hours_x_rate": "Hours × rate", "quote": "Quote", "estimate": "Estimate",
                "rate_x_duration": "Rate × duration", "weeks_x_rate": "Weeks × rate", "actual_fee": "Actual fee"}


@dataclass
class Flag:
    kind: str
    message: str


@dataclass
class RenderResult:
    path: Path
    flags: list = field(default_factory=list)
    priced: object = None
    needed: list = field(default_factory=list)

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
    """Everything unconfirmed, for the PM's checklist. It never appears in the client's document."""

    def __init__(self):
        self.flags = []
        self.items = []

    def add(self, label, question, kind="missing_input"):
        self.flags.append(Flag(kind, f"{label}: {question}"))
        item = f"{label}: {question}"
        if item not in self.items:
            self.items.append(item)

    def check(self, label, field_value):
        if field_value.get("status") == "unknown":
            self.add(label, field_value["question"])


def long_date(value):
    when = date.fromisoformat(value)
    return f"{when:%B} {when.day}, {when.year}"


def shown_date(field_value, blank):
    return long_date(field_value["value"]) if field_value.get("status") == "known" else shown(field_value, blank)


def known_items(field_value, needs, label, empty, blank):
    needs.check(label, field_value)
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


def line_source(line):
    evidence = line["evidence"]
    label = BASIS_LABELS[line["basis"]]
    source = f"{label}: {shown(evidence['reference'], '[quote reference]')}"
    if evidence["date"].get("status") == "known":
        source += f", dated {long_date(evidence['date']['value'])}"
    elif evidence["date"].get("status") == "unknown":
        source += ", dated [quote date]"
    return source


def line_attachment(line):
    field_value = line["attachment"]
    if field_value.get("status") == "known":
        return field_value["value"]
    return "[att.]" if field_value.get("status") == "unknown" else ""


def shown_quantity(line, value):
    if value is None:
        return quantity_text(line["quantity"])
    return f"{value:g}" if value == cents(value) else f"{cents(value):g}"


def pricing_rows(draft, priced, mode):
    lines = []
    shown_lines = [(draft["cost_lines"][p.index], p) for p in priced.lines if p.included]
    for index, (line, result) in enumerate(shown_lines):
        amount, unit_price = result.amount, result.unit_price
        hide = mode == "hidden" and line["category"] not in priced.pass_through_categories
        division = line["division"]
        lines.append({
            "division": division["value"] if division.get("status") == "known" else ("[div]" if division.get("status") == "unknown" else ""),
            "description": line["description"],
            "source": line_source(line),
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
    total = money(priced.total) if priced.complete else "[total]"
    summary = [{"label": label, "amount": money(amount) if amount >= 0 else f"-{money(-amount)}", "fill": WHITE}
               for label, amount in rows]
    summary.append({"label": "Total", "amount": total, "fill": LINEN})
    return lines, summary


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


def signature_rows(draft, package, needs):
    """By party, in the order the company's co-signature timing sets."""
    timing = package.values["AUTH-WRITTEN.cosign_timing"]
    order = list(SIGNATURE_ORDER[timing])
    if draft["signatures"]["order"] != order:
        needs.flags.append(Flag("signature_order", f"The draft's signature order doesn't match {timing}. Used the setting."))
    executive = package.values["role.Executive"]["name"]
    title = package.company("signatory", "title")
    pm = package.values["role.ProjectManager"]["name"]
    parties = {
        "executive_cosign": (package.company("name"), ", ".join(x for x in (executive, title) if x) or "[Executive name and title]", BODY_SIZE),
        "client": ("Client", shown(draft["header"]["owner"], "[Client name]"), BODY_SIZE),
        "architect": ("Architect", "[Architect name]", BODY_SIZE),
        "contractor_pm": ("Prepared by", pm or "[Project manager name]", SMALL_SIZE),
    }
    architect = draft["architect_signs"]
    if architect.get("status") == "unknown":   # An extra line is easy to delete; a missing one is easy to miss.
        parties["architect"] = ("Architect", "[if the architect signs]", BODY_SIZE)
        order.insert(order.index("contractor_pm"), "architect")
        needs.add("Architect", architect["question"])
    elif architect["value"]:
        order.insert(order.index("contractor_pm"), "architect")
        needs.add("Architect", "Who signs for the architect?")
    if not pm:
        needs.add("Prepared by", "Which project manager prepared this?")
    rows = []
    for key in order:
        label, name, size = parties[key]
        rows.append({"key": key, "label": RichText(label, size=size), "name": RichText(name, size=size)})
    return rows


def schedule_rows(draft, needs):
    schedule = draft["schedule"]
    extension, authorization = schedule["extension_days"], schedule["authorization_by"]
    needs.check("Schedule impact", extension)
    needs.check("Critical path", schedule["critical_path"])
    needs.check("Completion date", schedule["completion_date"])
    needs.check("Authorization-by date", authorization)
    if extension.get("status") != "known":
        days = "[days]"
    elif extension["value"] == 0:
        days = "None"
    else:
        days = f"{extension['value']:g} calendar day{'' if extension['value'] == 1 else 's'} added"
    critical = schedule["critical_path"]
    if authorization.get("status") == "not_applicable":
        condition = NOT_APPLICABLE
    else:
        condition = f"Schedule assumes signed authorization by {shown_date(authorization, '[date]')}."
    return {
        "days": days,
        "critical_path": critical["value"].capitalize() if critical.get("status") == "known" else "[yes/no]",
        "completion": shown(schedule["completion_date"], "[completion date]"),
        "condition": condition,
    }


def clarification_lines(draft, needs):
    lines = []
    for item in draft["clarifications_and_exclusions"]:
        if item["disposition"] == "not_applicable":
            continue
        lines.append(item["text"])
        if item["disposition"] == "open_question" and item["prompt"] != "permits":
            needs.add("Clarification", item["text"])
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
                      kind="permit_question")


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
        needs.check(label, header[name])

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
    priced = price(draft, package)
    flags += [Flag(f.code, f.message) for f in priced.flags]
    mode = presentation(draft, values, flags)
    lines, summary = pricing_rows(draft, priced, mode)
    period, valid_through = validity(draft, values)
    template = DocxTemplate(package.template)
    rows = header_rows(draft, package, needs, valid_through)
    needs.check("Description", draft["description"])
    needs.check("Reason", draft["reason"])
    context = {
        "logo": InlineImage(template, str(package.logo), height=Inches(0.42)),
        "company": {"name": package.company("name")},
        "company_name_upper": package.company("name").upper(),
        "company_address": package.company("address"),
        "company_email": package.company("email"),
        "generation_reference": f"Generated from package {package.reference}",
        "footer_title": f"Potential Change Order {shown(header['pco_number'], '[PCO-###]')}",
        "header_rows": rows,
        "validity_period": period,
        "validity_date": valid_through,
        "description": shown(draft["description"], "[Description]"),
        "reason": shown(draft["reason"], "[Reason]"),
        "inclusions": known_items(draft["scope"]["inclusions"], needs, "Inclusions", "None stated", "[Inclusions]"),
        "deleted_scope": known_items(draft["scope"]["deleted_scope"], needs, "Deleted scope", "None", "[Deleted scope, or None]"),
        "assumptions": draft["scope"]["assumptions"] or ["None stated"],
        "clarifications": clarification_lines(draft, needs),
        "lines": lines,
        "summary": summary,
        "markup_basis": markup_basis(draft, priced, mode, package.company("name")),
        "schedule": schedule_rows(draft, needs),
        "signatures": signature_rows(draft, package, needs),
        "attachments": draft["attachments"] or [{"letter": "", "description": "None"}],
    }
    for line in draft["cost_lines"]:
        for name in ("quantity", "unit", "unit_price", "division", "attachment"):
            needs.check(f"{line['description']}, {name.replace('_', ' ')}", line[name])
        needs.check(f"{line['description']}, quote reference", line["evidence"]["reference"])
        needs.check(f"{line['description']}, quote date", line["evidence"]["date"])
    check_clarifications(draft, package.definition, flags)
    check_permits(draft, package.definition, needs)
    check_attachment_letters(draft, flags)
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
    return RenderResult(out, flags, priced, needs.items)


def render_checklist(result, draft, package, out):
    """The PM's list of every open question and flag. Separate from the client's document."""
    questions = list(result.needed)
    for question in draft.get("open_questions") or []:
        if question not in questions:
            questions.append(question)
    urgent = [flag for flag in result.flags if flag.kind in URGENT_FLAGS]
    others = [flag for flag in result.flags if flag.kind not in ("missing_input", "permit_question", *URGENT_FLAGS)]
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
    line(f"For {result.path.name}. Clear these before sending the PCO.", after=10)
    if urgent:
        line("Read first", size=11.5, bold=True)
        for flag in urgent:
            line(f"☐  {flag.message}", bold=True)
    line("Open questions", size=11.5, bold=True)
    for question in questions or ["None"]:
        line(f"☐  {question}")
    line("Flags", size=11.5, bold=True, after=3)
    for flag in others or [Flag("", "None")]:
        line(f"☐  {flag.message}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    document.save(out)
    return out
