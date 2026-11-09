"""Reading a rendered PCO back: the pricing table, its summary and total bar, and the signature blocks."""
from docx import Document


def table_starting(path_or_document, label):
    document = Document(path_or_document) if not hasattr(path_or_document, "tables") else path_or_document
    for table in document.tables:
        if table.rows and table.rows[0].cells[0].text == label:
            return table
    raise AssertionError(f"no table starting {label}")


def pricing_table(path_or_document):
    return table_starting(path_or_document, "Div.")


AMOUNT = 6   # the last column of the pricing table: Div., Category, Description, Qty, Unit, Unit price, Amount


def merged(row):
    return row.cells[0]._tc is row.cells[AMOUNT - 1]._tc


def summary_rows(path):
    """Summary label -> amount. The total bar is also under "Total", whatever the package calls it."""
    rows = {}
    summary = [row for row in pricing_table(path).rows[1:] if merged(row)]
    for row in summary:
        rows[row.cells[0].text] = row.cells[AMOUNT].text
    if summary:
        rows["Total"] = summary[-1].cells[AMOUNT].text
    return rows


def total_label(path):
    return [row for row in pricing_table(path).rows[1:] if merged(row)][-1].cells[0].text


def signature_table(path):
    document = Document(path)
    for table in document.tables:
        if len(table.rows) > 1 and table.rows[1].cells[0].text == "Signature":
            return table
    raise AssertionError("no signature blocks")


def signature_blocks(path):
    """[(title, printed name, title line)] in order."""
    rows = signature_table(path).rows
    blocks = []
    for index, row in enumerate(rows):
        if row.cells[0]._tc is row.cells[1]._tc:
            fields = {rows[i].cells[0].text: rows[i].cells[1].text for i in range(index + 1, min(index + 5, len(rows)))}
            blocks.append((row.cells[0].text, fields.get("Printed name", ""), fields.get("Title", "")))
    return blocks


def parties(path):
    return [title for title, _, _ in signature_blocks(path)]
