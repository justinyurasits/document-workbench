"""Builds a plain Word template for the fictional company. Structure only: it has the same fields as a real one."""
import base64
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def loop(table, tag):
    table.add_row().cells[0].paragraphs[0].add_run(tag)


def bullets(document, name, text="{{ item }}"):
    document.add_paragraph("{%p for item in " + name + " %}")
    document.add_paragraph(text)
    document.add_paragraph("{%p endfor %}")


def build(root):
    (root / "templates/assets").mkdir(parents=True, exist_ok=True)
    (root / "templates/assets/logo.png").write_bytes(TINY_PNG)
    document = Document()
    section = document.sections[0]
    section.header.paragraphs[0].add_run("{{ logo }} {{ company_name_upper }}\t{{ document_id }} | {{ issue_date }}")
    section.footer.paragraphs[0].add_run("{{ company_address }} {{ company_email }} {{ generation_reference }}")
    grid = document.add_table(rows=0, cols=4)
    loop(grid, "{%tr for row in header_rows %}")
    for cell, tag in zip(grid.add_row().cells, ("label_a", "value_a", "label_b", "value_b")):
        cell.text = "{{ row." + tag + " }}"
    loop(grid, "{%tr endfor %}")
    document.add_paragraph("{{ description }}")
    document.add_paragraph("NO WORK PROCEEDS UNTIL THE OWNER SIGNS AND {{ company_name_upper }} CO-SIGNS.")
    document.add_paragraph("Reason: {{ reason }}")
    document.add_paragraph("{%p if request_line %}")
    document.add_paragraph("{{ request_line }}")
    document.add_paragraph("{%p endif %}")
    document.add_paragraph("Included")
    bullets(document, "inclusions")
    document.add_paragraph("Deleted")
    bullets(document, "deleted_scope")
    bullets(document, "assumptions")
    bullets(document, "clarifications")
    labels = ["Div.", "Category", "Description", "Att.", "Qty", "Unit", "Unit price", "Amount"]
    pricing = document.add_table(rows=1, cols=8)
    for cell, label in zip(pricing.rows[0].cells, labels):
        cell.text = label
    loop(pricing, "{%tr for line in lines %}")
    fields = ["division", "category", "description", "attachment", "quantity", "unit", "unit_price", "amount"]
    for cell, name in zip(pricing.add_row().cells, fields):
        cell.text = "{{ line." + name + " }}"
    loop(pricing, "{%tr endfor %}")
    loop(pricing, "{%tr for row in summary %}")
    row = pricing.add_row()
    row.cells[0].merge(row.cells[6]).text = "{{ row.label }}"
    row.cells[7].text = "{{ row.amount }}"
    loop(pricing, "{%tr endfor %}")
    total = pricing.add_row()
    total.cells[0].merge(total.cells[6]).text = "{{ total_label }}"
    total.cells[7].text = "{{ total_amount }}"
    document.add_paragraph("{{ markup_basis }}")
    schedule = document.add_table(rows=0, cols=2)
    loop(schedule, "{%tr for row in schedule.rows %}")
    cells = schedule.add_row().cells
    cells[0].text, cells[1].text = "{{ row.label }}", "{{ row.value }}"
    loop(schedule, "{%tr endfor %}")
    document.add_paragraph("{%p if schedule.sentence %}")
    document.add_paragraph("{{ schedule.sentence }}")
    document.add_paragraph("{%p endif %}")
    bullets(document, "exhibits", "{{ item.mark }} {{ item.label }}")
    document.add_paragraph("The Owner authorizes {{ company.name }} for {{ total_words }} ({{ total_figures }}), valid "
                           "{{ validity_period }} through {{ validity_date }}, by PCCO-[###].")
    signers = document.add_table(rows=0, cols=2)
    loop(signers, "{%tr for block in signature_blocks %}")
    title = signers.add_row()
    title.cells[0].merge(title.cells[1]).text = "{{ block.title_upper }}"
    for label, tag in (("Signature", " "), ("Printed name", "{{ block.name }}"), ("Title", "{{ block.role }}"),
                       ("Date", " ")):
        cells = signers.add_row().cells
        cells[0].text, cells[1].text = label, tag
    loop(signers, "{%tr endfor %}")
    document.styles["Normal"].font.name = "Arial"
    document.save(root / "templates/pco.docx")
