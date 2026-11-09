"""The renderer against the real package: its sample drafts and template. All expectations come from the package."""
import copy
import json
import shutil
import subprocess
import os
import re
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from docx import Document
from docx.oxml.ns import qn

from construction_os.rendering import CHECKLIST_TITLE, document_text, load_package, render, render_checklist, unfilled_fields
from construction_os.validate import sample_paths
from docx_helpers import pricing_table, signature_table, summary_rows

CORPUS = Path(os.environ.get("CORPUS", Path(__file__).resolve().parents[2] / "construction-os-corpus"))
pytestmark = pytest.mark.skipif(not (CORPUS / "templates/pco.docx").exists() or not (CORPUS / "samples").is_dir(),
                                reason="package not found; set CORPUS")
SAMPLES = sample_paths(CORPUS)
money = lambda value: f"${Decimal(value):,.2f}"


@pytest.fixture
def package():
    return load_package(CORPUS)


def expectations():
    return yaml.safe_load((CORPUS / "samples/expected.yaml").read_text())


def rendered(tmp_path, package, path):
    return render(json.loads(path.read_text()), package, tmp_path / f"{path.stem}.docx")


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_renders_with_every_field_filled(tmp_path, package, path):
    assert unfilled_fields(rendered(tmp_path, package, path).path) == []


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_totals_match_the_package(tmp_path, package, path):
    expected = expectations()[path.name]
    rows = summary_rows(rendered(tmp_path, package, path).path)
    assert rows["Subtotal"] == money(expected["subtotal"]) and rows["Total"] == money(expected["total"])


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_has_the_required_text(tmp_path, package, path):
    text = document_text(rendered(tmp_path, package, path).path)
    for required in expectations()["required_text"]:
        assert required in text


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_checklist(tmp_path, package, path):
    draft = json.loads(path.read_text())
    result = render(draft, package, tmp_path / "pco.docx")
    text = document_text(render_checklist(result, draft, package, tmp_path / "list.docx"))
    assert text.startswith(CHECKLIST_TITLE)


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_all_prompts_considered_in_the_samples(tmp_path, package, path):
    assert "prompt_not_considered" not in rendered(tmp_path, package, path).kinds()


def test_template_has_no_identity_typed_in(package):
    with zipfile.ZipFile(package.template) as archive:
        text = "".join(archive.read(n).decode("utf-8", "ignore") for n in archive.namelist() if n.endswith(".xml"))
    for value in (package.company("name"), package.company("legal_name"), package.company("address"), package.tagline):
        assert value not in text


def test_tagline_stays_out_of_the_document(tmp_path, package):
    assert package.tagline not in document_text(rendered(tmp_path, package, SAMPLES[0]).path)


def test_one_typeface(tmp_path, package):
    with zipfile.ZipFile(rendered(tmp_path, package, SAMPLES[0]).path) as archive:
        xml = "".join(archive.read(n).decode() for n in archive.namelist() if re.match(r"word/(document|header\d*|footer\d*)\.xml", n))
    assert len(set(re.findall(r'w:ascii="([^"]+)"', xml))) == 1


# Page layout. A PCO is as long as it needs to be; these keep-together rules replace any page limit.
# They are checked as Word properties, not by counting pages, which depends on the fonts installed.

def row_paragraphs(row):
    return [p for cell in row.cells for p in cell.paragraphs]


def keeps_with_next(row):
    return all(p.paragraph_format.keep_with_next for p in row_paragraphs(row))


def row_property(row, name):
    properties = row._tr.trPr
    return properties is not None and properties.find(qn(f"w:{name}")) is not None


def long_draft(path):
    """A sample draft with enough lines and signers to run past a page."""
    draft = json.loads(path.read_text())
    draft["cost_lines"] = [copy.deepcopy(line) for line in draft["cost_lines"] for _ in range(12)]
    return draft


def table_starting(document, label):
    return next(t for t in document.tables if t.rows[0].cells[0].text == label)


def test_template_repeats_the_pricing_header_and_keeps_only_the_last_line_with_the_summary(package):
    table = table_starting(Document(package.template), "Div.")
    rows = list(table.rows)
    assert row_property(rows[0], "tblHeader") and keeps_with_next(rows[0])
    after = {rows[i].cells[0].text: rows[i + 1] for i in range(len(rows) - 1) if rows[i].cells[0].text.startswith("{%tr for")}
    assert not keeps_with_next(after["{%tr for line in lines[:-1] %}"])
    assert keeps_with_next(after["{%tr for line in lines[-1:] %}"])
    assert keeps_with_next(after["{%tr for row in summary %}"])


def test_pricing_table_breaks_between_lines_and_keeps_the_summary_with_the_last_line(tmp_path, package):
    draft = long_draft(SAMPLES[0])
    result = render(draft, package, tmp_path / "long.docx")
    rows = list(table_starting(Document(result.path), "Div.").rows)
    count = len(draft["cost_lines"])
    header, lines, summary = rows[0], rows[1:1 + count], rows[1 + count:]
    assert row_property(header, "tblHeader") and row_property(header, "cantSplit") and keeps_with_next(header)
    assert all(row_property(row, "cantSplit") for row in lines + summary)
    assert all(p.paragraph_format.keep_together for row in lines + summary for p in row_paragraphs(row))
    assert not any(keeps_with_next(row) for row in lines[:-1])
    assert keeps_with_next(lines[-1]) and all(keeps_with_next(row) for row in summary)
    assert summary[-1].cells[0].text == package.definition["presentation"]["total_labels"]["add"]


def test_signature_block_never_splits(tmp_path, package):
    """The authorization paragraph and every signature block stay together on one page."""
    path = render(long_draft(SAMPLES[0]), package, tmp_path / "long.docx").path
    paragraphs = Document(path).paragraphs
    start = next(i for i, p in enumerate(paragraphs) if p.text == "AUTHORIZATION AND SIGNATURES")
    heading, authorization = paragraphs[start], paragraphs[start + 1]
    assert authorization.text.startswith("By signing below, the Owner authorizes")
    assert heading.paragraph_format.keep_with_next and authorization.paragraph_format.keep_with_next
    assert authorization.paragraph_format.keep_together
    rows = list(signature_table(path).rows)
    assert len(rows) == 3 * 5
    assert all(row_property(row, "cantSplit") for row in rows)
    assert all(keeps_with_next(row) and all(p.paragraph_format.keep_together for p in row_paragraphs(row)) for row in rows)


# Presentation (PCO polish). Checked on the package's own template, as Word properties and text.

def headings(document):
    return [p.text for p in document.paragraphs if re.fullmatch(r"(\d\. )?[A-Z][A-Z ]+", p.text or "")]


def test_numbered_sections_in_order_under_the_title(tmp_path, package):
    document = Document(rendered(tmp_path, package, SAMPLES[0]).path)
    assert headings(document) == ["POTENTIAL CHANGE ORDER", "1. DESCRIPTION OF POTENTIAL CHANGE", "2. SCOPE OF WORK",
                                  "3. PRICING", "4. SCHEDULE IMPACT", "AUTHORIZATION AND SIGNATURES"]   # no exhibits supplied
    texts = [p.text for p in document.paragraphs]
    labels = [t for t in texts if t in ("Added scope", "Deleted scope", "Assumptions", "Clarifications and exclusions")]
    assert labels == ["Added scope", "Deleted scope", "Assumptions", "Clarifications and exclusions"]
    assert "TERMS" not in texts


def test_notice_box_sits_under_the_header_table(tmp_path, package):
    document = Document(rendered(tmp_path, package, SAMPLES[0]).path)
    notice = document.tables[1]
    assert len(notice.rows) == 1 and len(notice.columns) == 1
    text = notice.rows[0].cells[0].text
    name = package.company("name").upper()
    assert text == (f"NO WORK ON THIS POTENTIAL CHANGE ORDER PROCEEDS UNTIL IT IS SIGNED BY THE OWNER AND CO-SIGNED BY "
                    f"{name}. APPROVAL BY EMAIL, TEXT MESSAGE OR CONVERSATION DOES NOT AUTHORIZE WORK.")


def cell_has(cell, name):
    return cell._tc.tcPr is not None and cell._tc.tcPr.find(qn(f"w:{name}")) is not None


def fill(cell):
    shading = cell._tc.tcPr.find(qn("w:shd")) if cell._tc.tcPr is not None else None
    return shading.get(qn("w:fill")) if shading is not None else None


def test_pricing_columns_are_short_and_amounts_never_wrap(tmp_path, package):
    table = pricing_table(rendered(tmp_path, package, SAMPLES[0]).path)
    assert [c.text for c in table.rows[0].cells] == ["Div.", "Category", "Description", "Qty", "Unit", "Unit price",
                                                     "Amount"]
    for row in table.rows:
        assert cell_has(row.cells[6], "noWrap"), [c.text for c in row.cells]
        if row.cells[0]._tc is not row.cells[5]._tc:
            assert all(cell_has(row.cells[i], "noWrap") for i in (0, 1, 3, 4, 5))


def test_total_is_a_dark_bar_with_the_package_label(tmp_path, package):
    rows = pricing_table(rendered(tmp_path, package, SAMPLES[0]).path).rows
    total = rows[-1]
    assert total.cells[0].text == package.definition["presentation"]["total_labels"]["add"] == "Total potential change: add"
    assert fill(total.cells[0]) == fill(total.cells[6]) == "2C2C2C"


def test_authorization_states_the_total_validity_pcco_and_billing(tmp_path, package):
    path = rendered(tmp_path, package, CORPUS / "samples/pco_test1_draft.json").path
    text = document_text(path)
    assert "for five thousand six hundred fourteen dollars and 40/100 ($5,614.40)" in text
    assert "valid for 30 days from the date above" in text
    assert "Prime Contract Change Order No. PCCO-[###], which updates the contract sum and contract time" in text
    assert "not billable until that change order is executed" in text
    assert "Approval by email, text message or conversation does not authorize the work" in text
    assert "the Owner authorizes the Contractor to perform the work" in text


def test_running_header_has_the_pco_number_and_date(tmp_path, package):
    header = Document(rendered(tmp_path, package, SAMPLES[0]).path).sections[0].header.paragraphs[0].text
    assert header.endswith("PCO-001  |  November 2, 2026") and "CHG-53" not in header


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_the_pco_never_says_client(tmp_path, package, path):
    assert not re.search(r"\bclient", document_text(rendered(tmp_path, package, path).path), re.I)


SOFFICE = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"


@pytest.mark.skipif(not (Path(SOFFICE).exists() and shutil.which("pdftotext")), reason="LibreOffice or pdftotext not installed")
def test_a_seven_figure_amount_never_wraps(tmp_path, package):
    """Laid out by LibreOffice: every amount must come out on one line, the total bar included."""
    from construction_os.rendering.__main__ import preview_env

    draft = json.loads(SAMPLES[0].read_text())
    for line in draft["cost_lines"]:
        if line["basis"] != "hours_x_rate":
            line["unit_price"] = {"status": "known", "value": 3999999.99}
    result = render(draft, package, tmp_path / "big.docx")
    subprocess.run([SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(result.path)],
                   check=True, capture_output=True, env=preview_env())
    text = subprocess.run(["pdftotext", "-layout", str(result.path.with_suffix(".pdf")), "-"],
                          capture_output=True, text=True).stdout
    assert result.priced.total > Decimal("9000000")
    for amount in [f"${line.amount:,.2f}" for line in result.priced.lines] + [f"${result.priced.total:,.2f}"]:
        assert amount in text, f"{amount} wrapped or is missing"
    dangling = [line for line in text.splitlines() if re.search(r"\$[\d,.]*$", line.rstrip())
                and not re.search(r"\$[\d,]+\.\d\d$", line.rstrip())]
    assert not dangling, dangling


# The PCO stands alone and reads as a letter (CHG-53 4.0)

def test_authorization_is_four_short_paragraphs(tmp_path, package):
    paragraphs = [p.text for p in Document(rendered(tmp_path, package, SAMPLES[0]).path).paragraphs]
    start = paragraphs.index("AUTHORIZATION AND SIGNATURES")
    block = paragraphs[start + 1:start + 5]
    assert [p.split(" ", 3)[:3] for p in block] == [["By", "signing", "below,"], ["This", "authorization", "takes"],
                                                    ["This", "price", "is"], ["Once", "executed,", "this"]]


def test_defaults_point_at_nothing_the_owner_never_gets(package):
    for prompt in package.definition["clarification_prompts"]:
        assert "quote" not in prompt["default"]["text"].lower(), prompt["id"]


INTERNAL_WORDS = re.compile(r"\b(PM|quotes?|subcontractors?|company carpenter|client)\b", re.I)


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_no_internal_words_or_exhibits_for_backup(tmp_path, package, path):
    text = document_text(rendered(tmp_path, package, path).path).replace("Prepared by", "")
    assert not INTERNAL_WORDS.search(text), INTERNAL_WORDS.search(text)
    assert "Exhibit" not in text and "5. ATTACHMENTS" not in text


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_the_company_name_only_in_header_notice_signatures_and_footer(tmp_path, package, path):
    document = Document(rendered(tmp_path, package, path).path)
    name = package.company("name")
    letter = [p.text for p in document.paragraphs]
    letter += [c.text for r in pricing_table(document).rows for c in r.cells]
    assert not any(name in text or name.upper() in text for text in letter)
    assert name.upper() in document.tables[1].rows[0].cells[0].text           # the notice box


@pytest.mark.skipif(not (Path(SOFFICE).exists() and shutil.which("pdftotext")), reason="LibreOffice or pdftotext not installed")
def test_the_longest_category_label_never_wraps(tmp_path, package):
    """Laid out by LibreOffice: "General conditions" and "Subcontract" each stay on one line."""
    from construction_os.rendering.__main__ import preview_env

    draft = json.loads((CORPUS / "samples/pco_test1_draft.json").read_text())
    gc = dict(draft["cost_lines"][3], category="general_conditions", basis="weeks_x_rate",
              role="Project Manager (Part-Time)", description="General conditions: project management")
    draft["cost_lines"].append(gc)
    draft["schedule"]["extension_days"] = {"status": "known", "value": 30}
    draft["schedule"]["contract_duration_days"] = {"status": "known", "value": 100}
    labels = package.definition["presentation"]["category_labels"].values()
    longest = max(labels, key=len)
    result = render(draft, package, tmp_path / "gc.docx")
    subprocess.run([SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(result.path)],
                   check=True, capture_output=True, env=preview_env())
    text = subprocess.run(["pdftotext", "-layout", str(result.path.with_suffix(".pdf")), "-"],
                          capture_output=True, text=True).stdout
    assert longest == "General conditions" and longest in text and "Subcontract " in text
    assert not re.search(r"Subcontrac\s*$", text, re.M) and not re.search(r"General\s*$", text, re.M)
