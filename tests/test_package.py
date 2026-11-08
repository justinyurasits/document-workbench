"""The renderer against the real package: its sample drafts and template. All expectations come from the package."""
import copy
import json
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


def summary_rows(path):
    table = next(t for t in Document(path).tables if t.rows[0].cells[0].text == "Div")
    return {row.cells[0].text: row.cells[7].text for row in table.rows[1:] if row.cells[0]._tc is row.cells[6]._tc}


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
    table = table_starting(Document(package.template), "Div")
    rows = list(table.rows)
    assert row_property(rows[0], "tblHeader") and keeps_with_next(rows[0])
    after = {rows[i].cells[0].text: rows[i + 1] for i in range(len(rows) - 1) if rows[i].cells[0].text.startswith("{%tr for")}
    assert not keeps_with_next(after["{%tr for line in lines[:-1] %}"])
    assert keeps_with_next(after["{%tr for line in lines[-1:] %}"])
    assert keeps_with_next(after["{%tr for row in summary %}"])


def test_pricing_table_breaks_between_lines_and_keeps_the_summary_with_the_last_line(tmp_path, package):
    draft = long_draft(SAMPLES[0])
    result = render(draft, package, tmp_path / "long.docx")
    rows = list(table_starting(Document(result.path), "Div").rows)
    count = len(draft["cost_lines"])
    header, lines, summary = rows[0], rows[1:1 + count], rows[1 + count:]
    assert row_property(header, "tblHeader") and row_property(header, "cantSplit") and keeps_with_next(header)
    assert all(row_property(row, "cantSplit") for row in lines + summary)
    assert all(p.paragraph_format.keep_together for row in lines + summary for p in row_paragraphs(row))
    assert not any(keeps_with_next(row) for row in lines[:-1])
    assert keeps_with_next(lines[-1]) and all(keeps_with_next(row) for row in summary)
    assert summary[-1].cells[0].text == "Total"


def test_signature_block_never_splits(tmp_path, package):
    document = Document(render(long_draft(SAMPLES[0]), package, tmp_path / "long.docx").path)
    paragraphs = document.paragraphs
    start = next(i for i, p in enumerate(paragraphs) if p.text == "TERMS")
    end = next(i for i, p in enumerate(paragraphs) if p.text == "SIGNATURES")
    block = paragraphs[start:end + 1]
    assert any(p.text.startswith("Authorization.") for p in block)
    assert all(p.paragraph_format.keep_with_next for p in block)
    assert all(p.paragraph_format.keep_together for p in block[1:-1])
    rows = list(table_starting(document, "Party").rows)
    header, signers = rows[0], rows[1:]
    assert len(signers) == 3 and row_property(header, "tblHeader") and keeps_with_next(header)
    assert all(row_property(row, "cantSplit") for row in rows)
    assert all(p.paragraph_format.keep_together for row in rows for p in row_paragraphs(row))
    assert all(keeps_with_next(row) for row in signers[:-1])
    assert not keeps_with_next(signers[-1])     # the block ends here; the attachments may move on

