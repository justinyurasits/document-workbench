"""Builds a plain Word template for the fictional company. Structure only: it has the same fields as a real one."""
import base64
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def loop(table, tag):
    table.add_row().cells[0].paragraphs[0].add_run(tag)


def bullets(document, name, text="{{ item }}"):
    document.add_paragraph("{%p for item in " + name + " %}")
    document.add_paragraph(text)
    document.add_paragraph("{%p endfor %}")


def shade(cell, fill="2C2C2C"):
    shading = OxmlElement("w:shd")
    for key, value in (("w:val", "clear"), ("w:color", "auto"), ("w:fill", fill)):
        shading.set(qn(key), value)
    cell._tc.get_or_add_tcPr().append(shading)


def pricing_rows(table, columns):
    """The same row kinds as the package's template: division, line, line_keep, subtotal, summary, total."""
    loop(table, "{%tr for row in rows %}")
    loop(table, "{%tr if row.kind == 'division' %}")
    table.add_row().cells[0].merge(table.rows[-1].cells[columns - 1]).text = "{{ row.label }}"
    loop(table, "{%tr elif row.kind in ('line', 'line_keep') %}")
    fields = ["division", "category", "description", "quantity", "unit", "unit_price", "amount"]
    for cell, name in zip(table.add_row().cells, fields):
        cell.text = "{{ row." + name + " }}"
    loop(table, "{%tr elif row.kind in ('subtotal', 'summary') %}")
    row = table.add_row()
    row.cells[0].merge(row.cells[columns - 2]).text = "{{ row.label }}"
    row.cells[columns - 1].text = "{{ row.amount }}"
    loop(table, "{%tr elif row.kind == 'total' %}")
    row = table.add_row()
    merged = row.cells[0].merge(row.cells[columns - 2])
    merged.text = "{{ row.label }}"
    row.cells[columns - 1].text = "{{ row.amount }}"
    shade(merged)
    shade(row.cells[columns - 1])
    loop(table, "{%tr endif %}")
    loop(table, "{%tr endfor %}")


def build(root):
    (root / "templates/assets").mkdir(parents=True, exist_ok=True)
    (root / "templates/assets/logo.png").write_bytes(TINY_PNG)
    document = Document()
    section = document.sections[0]
    section.different_first_page_header_footer = True
    section.header.paragraphs[0].add_run("{{ logo }} {{ company_name_upper }}\t{{ document_id }} | {{ issue_date }}")
    for foot in (section.footer, section.first_page_footer):
        foot.paragraphs[0].add_run("{{ company_address }} {{ company_email }} {{ generation_reference }}")
    document.add_paragraph("{{ logo_large }}")
    document.add_paragraph("{{ company_name_upper }}")
    document.add_paragraph("POTENTIAL CHANGE ORDER")
    document.add_paragraph("{{ document_id }} · {{ issue_date }}")
    grid = document.add_table(rows=0, cols=4)
    loop(grid, "{%tr for row in header_rows %}")
    for cell, tag in zip(grid.add_row().cells, ("label_a", "value_a", "label_b", "value_b")):
        cell.text = "{{ row." + tag + " }}"
    loop(grid, "{%tr endfor %}")
    document.add_paragraph("NO WORK PROCEEDS UNTIL THE OWNER SIGNS AND {{ company_name_upper }} CO-SIGNS.")
    document.add_paragraph("1. DESCRIPTION OF POTENTIAL CHANGE")
    document.add_paragraph("{{ description }}")
    document.add_paragraph("{{ work_includes }}")
    bullets(document, "work_items")
    document.add_paragraph("{%p if basis_of_quantities %}")
    document.add_paragraph("{{ basis_of_quantities_label }}")
    bullets(document, "basis_of_quantities")
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("{%p if deleted_scope %}")
    document.add_paragraph("Deleted scope")
    bullets(document, "deleted_scope")
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("2. PRICING")
    document.add_paragraph("{%p if pricing.detail %}")
    lines = document.add_table(rows=1, cols=7)
    for cell, label in zip(lines.rows[0].cells, ["Div.", "Category", "Description", "Qty", "Unit", "Unit price", "Amount"]):
        cell.text = label
    loop(lines, "{%tr set rows = pricing.detail %}")
    pricing_rows(lines, 7)
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("{%p if pricing.summary %}")
    summary = document.add_table(rows=0, cols=2)
    loop(summary, "{%tr set rows = pricing.summary %}")
    pricing_rows(summary, 2)
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("{{ markup_basis }}")
    document.add_paragraph("3. SCHEDULE IMPACT")
    schedule = document.add_table(rows=0, cols=2)
    loop(schedule, "{%tr for row in schedule.rows %}")
    cells = schedule.add_row().cells
    cells[0].text, cells[1].text = "{{ row.label }}", "{{ row.value }}"
    loop(schedule, "{%tr endfor %}")
    document.add_paragraph("{%p if schedule.activities %}")
    activities = document.add_table(rows=1, cols=2)
    activities.rows[0].cells[0].text, activities.rows[0].cells[1].text = "{{ schedule.activity_label }}", "{{ schedule.duration_label }}"
    loop(activities, "{%tr for item in schedule.activities %}")
    cells = activities.add_row().cells
    cells[0].text, cells[1].text = "{{ item.activity }}", "{{ item.duration }}"
    loop(activities, "{%tr endfor %}")
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("{%p if schedule.sentence %}")
    document.add_paragraph("{{ schedule.sentence }}")
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("4. ASSUMPTIONS AND CLARIFICATIONS")
    document.add_paragraph("{%p for group in assumption_groups %}")
    document.add_paragraph("{{ group.title }}")
    bullets(document, "group.entries")
    document.add_paragraph("{%p endfor %}")
    document.add_paragraph("{%p if exhibits %}")
    document.add_paragraph("5. ATTACHMENTS")
    bullets(document, "exhibits", "{{ item.mark }} {{ item.label }}")
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("AUTHORIZATION AND SIGNATURES")
    document.add_paragraph("The Owner authorizes the Contractor for {{ total_words }} ({{ total_figures }}).")
    document.add_paragraph("Effective when the Owner signs and the Contractor co-signs.")
    document.add_paragraph("Valid {{ validity_period }} through {{ validity_date }}.")
    document.add_paragraph("Incorporated by PCCO-[###]; billable only after it is executed.")
    for left, right in (("signers.left", "signers.right"), ("signers.architect", None)):
        if right is None:
            document.add_paragraph("{%p if signers.architect %}")
        table = document.add_table(rows=0, cols=5)
        title = table.add_row().cells
        title[0].merge(title[1]).text = "{{ " + left + ".title_upper }}"
        if right:
            title[3].merge(title[4]).text = "{{ " + right + ".title_upper }}"
        for label, field in (("Signature", None), ("Printed name", "name"), ("Title", "role"), ("Date", None)):
            cells = table.add_row().cells
            for start, who in ((0, left), (3, right)):
                if who:
                    cells[start].text = label
                    cells[start + 1].text = "{{ " + who + "." + field + " }}" if field else " "
        if right is None:
            document.add_paragraph("{%p endif %}")
    document.styles["Normal"].font.name = "Arial"
    document.save(root / "templates/pco.docx")
