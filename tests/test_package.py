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
from docx_helpers import pricing_table, signature_table, summary_rows, summary_table

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


def row_after(rows, tag):
    index = next(i for i, row in enumerate(rows) if row.cells[0].text == tag)
    return rows[index + 1]


def test_template_repeats_the_pricing_header_and_keeps_only_the_last_line_with_what_follows(package):
    rows = list(table_starting(Document(package.template), "Div.").rows)
    assert row_property(rows[0], "tblHeader") and keeps_with_next(rows[0])
    assert not keeps_with_next(row_after(rows, "{%tr elif row.kind == 'line' %}"))
    assert keeps_with_next(row_after(rows, "{%tr elif row.kind == 'line_keep' %}"))
    assert keeps_with_next(row_after(rows, "{%tr if row.kind == 'division' %}"))
    assert keeps_with_next(row_after(rows, "{%tr elif row.kind in ('subtotal', 'summary') %}"))


def test_pricing_groups_break_between_lines_and_keep_each_subtotal_with_its_last_line(tmp_path, package):
    assert package.values["PRICING-PRESENTATION"] == "by_division"
    draft = long_draft(SAMPLES[0])
    path = render(draft, package, tmp_path / "long.docx").path
    rows = list(table_starting(Document(path), "Div.").rows)
    header = rows[0]
    assert row_property(header, "tblHeader") and row_property(header, "cantSplit") and keeps_with_next(header)
    assert all(row_property(row, "cantSplit") for row in rows)
    group = []
    for row in rows[1:]:
        if row.cells[0]._tc is row.cells[6]._tc:          # a division heading: kept with its first line
            assert keeps_with_next(row) and not group
        elif row.cells[0]._tc is row.cells[5]._tc:        # the group's subtotal: its last line keeps with it
            assert keeps_with_next(group[-1]) and not any(keeps_with_next(line) for line in group[:-1])
            group = []
        else:
            group.append(row)
    total = summary_table(path).rows[-1]
    assert total.cells[0].text == package.definition["presentation"]["total_labels"]["add"]
    assert all(keeps_with_next(row) for row in summary_table(path).rows)


def test_signature_block_never_splits(tmp_path, package):
    """The authorization paragraphs and the signatures stay together on one page."""
    path = render(long_draft(SAMPLES[0]), package, tmp_path / "long.docx").path
    paragraphs = Document(path).paragraphs
    start = next(i for i, p in enumerate(paragraphs) if p.text == "AUTHORIZATION AND SIGNATURES")
    block = paragraphs[start:start + 5]
    assert block[1].text.startswith("By signing below, the Owner authorizes")
    assert all(p.paragraph_format.keep_with_next for p in block)
    assert all(p.paragraph_format.keep_together for p in block[1:])
    rows = list(signature_table(path).rows)
    assert len(rows) == 5                                   # titles, then signature, name, title, date
    assert all(row_property(row, "cantSplit") for row in rows)
    assert all(keeps_with_next(row) and all(p.paragraph_format.keep_together for p in row_paragraphs(row)) for row in rows)


# Presentation (PCO polish). Checked on the package's own template, as Word properties and text.

def headings(document):
    return [p.text for p in document.paragraphs if re.fullmatch(r"(\d\. )?[A-Z][A-Z ]+", p.text or "")]


def test_numbered_sections_in_order_under_the_title(tmp_path, package):
    document = Document(rendered(tmp_path, package, SAMPLES[0]).path)
    assert headings(document) == [package.company("name").upper(), "POTENTIAL CHANGE ORDER", "1. DESCRIPTION OF POTENTIAL CHANGE", "2. PRICING",
                                  "3. SCHEDULE IMPACT", "4. ASSUMPTIONS AND CLARIFICATIONS",
                                  "AUTHORIZATION AND SIGNATURES"]   # no exhibits supplied
    texts = [p.text for p in document.paragraphs]
    assert "The work includes:" in texts and "Added scope" not in texts and "TERMS" not in texts


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


def test_pricing_columns_are_short_and_never_wrap_mid_word(tmp_path, package):
    path = rendered(tmp_path, package, SAMPLES[0]).path
    table = pricing_table(path)
    assert [c.text for c in table.rows[0].cells] == ["Div.", "Category", "Description", "Qty", "Unit", "Unit price",
                                                     "Amount"]
    for row in table.rows:
        if row.cells[0]._tc is row.cells[6]._tc:             # a division heading
            continue
        assert cell_has(row.cells[6], "noWrap"), [c.text for c in row.cells]
        if row.cells[0]._tc is not row.cells[5]._tc:          # a line: everything but Description holds its line
            assert all(cell_has(row.cells[i], "noWrap") for i in (0, 1, 3, 4, 5))
    assert all(cell_has(row.cells[1], "noWrap") for row in summary_table(path).rows)


def test_total_is_a_dark_bar_with_the_package_label(tmp_path, package):
    total = summary_table(rendered(tmp_path, package, SAMPLES[0]).path).rows[-1]
    assert total.cells[0].text == package.definition["presentation"]["total_labels"]["add"] == "Total potential change: add"
    assert fill(total.cells[0]) == fill(total.cells[1]) == "2C2C2C"


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
def test_the_company_name_only_on_the_cover_header_notice_signatures_and_footer(tmp_path, package, path):
    document = Document(rendered(tmp_path, package, path).path)
    name = package.company("name")
    paragraphs = [p.text for p in document.paragraphs]
    letter = paragraphs[paragraphs.index("1. DESCRIPTION OF POTENTIAL CHANGE"):]
    letter += [c.text for r in pricing_table(document).rows for c in r.cells]
    assert not any(name in text or name.upper() in text for text in letter)
    assert name.upper() in document.tables[1].rows[0].cells[0].text           # the notice box
    assert paragraphs.count(name.upper()) == 1                                 # the cover


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


@pytest.mark.skipif(not (Path(SOFFICE).exists() and shutil.which("pdftotext")), reason="LibreOffice or pdftotext not installed")
def test_day_units_and_unknown_quantities_never_wrap(tmp_path, package):
    """Laid out by LibreOffice: "DAY" and "[qty]" stay whole in their columns."""
    from construction_os.rendering.__main__ import preview_env

    draft = json.loads((CORPUS / "samples/pco_test1_draft.json").read_text())
    role, rate = next((row["role"], row["rate"]) for row in package.values["LABOR-BILLING-RATES"] if row["unit"] == "day")
    crew = dict(draft["cost_lines"][3], role=role, basis="rate_x_duration", description="Remove finishes",
                unit={"status": "known", "value": "DAY"}, quantity={"status": "unknown", "question": "How many days?"},
                unit_price={"status": "unknown", "question": "What day rate?"})
    draft["cost_lines"].append(crew)
    result = render(draft, package, tmp_path / "day.docx")
    subprocess.run([SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(result.path)],
                   check=True, capture_output=True, env=preview_env())
    text = subprocess.run(["pdftotext", "-layout", str(result.path.with_suffix(".pdf")), "-"],
                          capture_output=True, text=True).stdout
    row = next(line for line in text.splitlines() if "Remove finishes" in line)
    day_rate = re.escape(f"${Decimal(str(rate)):,.2f}")          # the table's day rate (day-rate crews, #12)
    assert re.search(rf"\[qty\]\s+DAY\s+{day_rate}\s+\[amount\]", row), row
    assert not re.search(r"(\[qt|\bDA)\s*$", text, re.M)


# Say each thing once (Nov 10): the columns carry quantity and unit, and only the deliberate repeat stays

@pytest.mark.parametrize("unit", ["SF", "EA", "LOAD", "LS"])
def test_an_estimate_line_never_says_lump_sum(tmp_path, package, unit):
    draft = json.loads((CORPUS / "samples/pco_test1_draft.json").read_text())
    line = draft["cost_lines"][0]
    draft["cost_lines"].append(dict(line, basis="estimate", description="Sample label", unit={"status": "known", "value": unit}))
    rows = [[c.text for c in r.cells] for r in pricing_table(render(draft, package, tmp_path / "e.docx").path).rows]
    assert [r[2] for r in rows if r[2].startswith("Sample label")] == ["Sample label"]
    assert not any("lump sum" in r[2].lower() for r in rows)


def test_rate_lines_name_the_rate_period_but_not_the_quantity(tmp_path, package):
    draft = json.loads((CORPUS / "samples/pco_test1_draft.json").read_text())
    phrases = package.definition["presentation"]["basis_phrases"]
    assert all("{quantity}" not in phrase and "{unit}" not in phrase for phrase in phrases.values())
    rows = [[c.text for c in r.cells] for r in pricing_table(render(draft, package, tmp_path / "r.docx").path).rows]
    for line in draft["cost_lines"]:
        if line["basis"] == "hours_x_rate":
            row = next(r for r in rows if r[2].startswith(line["description"]))
            assert row[2] == f"{line['description']} ({phrases['hours_x_rate']})" and row[3] not in row[2]


def test_the_notice_and_the_authorization_both_keep_the_deliberate_repeat(tmp_path, package):
    path = rendered(tmp_path, package, SAMPLES[0]).path
    notice = Document(path).tables[1].rows[0].cells[0].text.lower()
    authorization = document_text(path).lower().split("authoriz", 1)[1]
    for text in (notice, authorization):
        assert "email" in text and "does not authorize" in text
    assert "no work" in notice and "signed" in notice
    assert "signature" in authorization or "signing" in authorization or "signed" in authorization
