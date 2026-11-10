"""Reading a rendered PCO back: the pricing tables (any PRICING-PRESENTATION), the total bar and the signatures."""
from docx import Document
from docx.oxml.ns import qn

DARK = "2C2C2C"


def document_of(path_or_document):
    return Document(path_or_document) if not hasattr(path_or_document, "tables") else path_or_document


def table_starting(path_or_document, label):
    for table in document_of(path_or_document).tables:
        if table.rows and table.rows[0].cells[0].text == label:
            return table
    raise AssertionError(f"no table starting {label}")


def pricing_table(path_or_document):
    """The line table (by_division and flat). summary_only has none."""
    return table_starting(path_or_document, "Div.")


def fill(cell):
    shading = cell._tc.tcPr.find(qn("w:shd")) if cell._tc.tcPr is not None else None
    return shading.get(qn("w:fill")) if shading is not None else None


def summary_table(path_or_document):
    """The separate summary table (by_division and summary_only): two columns, ending in the dark total bar."""
    for table in document_of(path_or_document).tables:
        if len(table.columns) == 2 and table.rows and fill(table.rows[-1].cells[-1]) == DARK:
            return table
    return None


def amount_rows(path_or_document):
    """(label, amount) for every row that carries an amount: group subtotals, summary rows and the total bar."""
    document = document_of(path_or_document)
    rows = []
    try:
        lines = pricing_table(document)
    except AssertionError:
        lines = None
    if lines is not None:
        last = len(lines.columns) - 1
        for row in lines.rows[1:]:
            if row.cells[0]._tc is row.cells[last - 1]._tc and row.cells[0]._tc is not row.cells[last]._tc:
                rows.append((row.cells[0].text, row.cells[last].text, fill(row.cells[last])))
    summary = summary_table(document)
    if summary is not None:
        rows += [(row.cells[0].text, row.cells[1].text, fill(row.cells[1])) for row in summary.rows]
    return rows


def summary_rows(path_or_document):
    """Label -> amount. The total bar is also under "Total", whatever the package calls it."""
    rows = {}
    for label, amount, shade in amount_rows(path_or_document):
        rows[label] = amount
        if shade == DARK:
            rows["Total"] = amount
    return rows


def total_label(path_or_document):
    return next(label for label, _, shade in amount_rows(path_or_document) if shade == DARK)


def signature_table(path_or_document):
    """The Contractor and the Owner side by side: five columns, the first row their titles."""
    for table in document_of(path_or_document).tables:
        if len(table.columns) == 5 and len(table.rows) > 1 and table.rows[1].cells[0].text == "Signature":
            return table
    raise AssertionError("no signature table")


def signature_tables(path_or_document):
    return [t for t in document_of(path_or_document).tables
            if len(t.columns) == 5 and len(t.rows) > 1 and t.rows[1].cells[0].text == "Signature"]


def side(table, start):
    rows = table.rows
    fields = {rows[i].cells[start].text: rows[i].cells[start + 1].text for i in range(1, len(rows))}
    title = rows[0].cells[start].text
    return (title, fields.get("Printed name", ""), fields.get("Title", "")) if title else None


def signature_blocks(path_or_document):
    """[(title, printed name, title line)]: left, right, then the architect below when present."""
    blocks = []
    for table in signature_tables(path_or_document):
        blocks += [b for b in (side(table, 0), side(table, 3)) if b]
    return blocks


def parties(path_or_document):
    return [title for title, _, _ in signature_blocks(path_or_document)]
