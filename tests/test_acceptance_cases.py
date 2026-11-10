"""Checks the package's acceptance cases themselves; grading waits for the drafting pipeline."""
import json
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from construction_os.rendering import load_package, render
from docx_helpers import summary_rows

CORPUS = Path(os.environ.get("CORPUS", Path(__file__).resolve().parents[2] / "construction-os-corpus"))
ROOT = CORPUS / "tests" / "acceptance" / "cases"
CASES = sorted(p for p in ROOT.iterdir() if p.is_dir()) if ROOT.is_dir() else []
pytestmark = pytest.mark.skipif(not CASES, reason="package acceptance cases not found; set CORPUS")


def expected(case):
    return yaml.safe_load((case / "expected.yaml").read_text())


def request(case):
    return (case / "request.md").read_text()


def cents(value):
    return Decimal(value).quantize(Decimal("0.01"))


def priced_cases():
    return [c for c in CASES if "pricing" in expected(c)["exact"]]


def test_there_are_six_cases():
    assert [expected(c)["id"] for c in CASES] == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_case_has_exact_and_judged_results(case):
    data = expected(case)
    assert data["mode"] in ("draft", "advice")
    assert data["exact"]["document"] and data["judged"]
    assert sorted(p.name for p in case.iterdir()) == ["expected.yaml", "request.md"]


@pytest.mark.parametrize("case", priced_cases(), ids=lambda c: c.name)
def test_request_does_not_give_the_answer(case):
    pricing = expected(case)["exact"]["pricing"]
    text = request(case)
    for key in ("subtotal", "markup_total", "total"):
        amount = f"{Decimal(pricing[key]):,.2f}"
        assert amount not in text and amount.removesuffix(".00") not in text


@pytest.mark.parametrize("case", priced_cases(), ids=lambda c: c.name)
def test_pricing_arithmetic(case):
    pricing = expected(case)["exact"]["pricing"]
    for line in pricing["lines"]:
        if "hours" in line:
            assert Decimal(line["hours"]) * Decimal(line["rate"]) == Decimal(line["amount"])
    subtotal = sum(Decimal(line["amount"]) for line in pricing["lines"])
    assert subtotal == Decimal(pricing["subtotal"])
    for component in pricing["markup"]:
        assert cents(subtotal * Decimal(component["percent"]) / 100) == Decimal(component["amount"])
    markup = sum(Decimal(c["amount"]) for c in pricing["markup"])
    assert markup == Decimal(pricing["markup_total"])
    passed_through = sum(Decimal(p["amount"]) for p in pricing["pass_through"])
    assert subtotal + markup + passed_through == Decimal(pricing["total"])


@pytest.mark.parametrize("case", priced_cases(), ids=lambda c: c.name)
def test_sample_draft_renders_to_the_expected_result(tmp_path, case):
    data = expected(case)
    pricing = data["exact"]["pricing"]
    result = render(json.loads((CORPUS / data["sample_draft"]).read_text()), load_package(CORPUS), tmp_path / "x.docx")
    rows = summary_rows(result.path)
    assert rows["Subtotal"] == f"${Decimal(pricing['subtotal']):,.2f}" and rows["Total"] == f"${Decimal(pricing['total']):,.2f}"
    for component in pricing["markup"]:
        assert rows[f"{component['name']} ({component['percent']}%)"] == f"${Decimal(component['amount']):,.2f}"
    assert not priced_lines_differ(pricing, result)


def priced_lines_differ(pricing, result):
    return [a for a, b in zip(sorted(Decimal(line["amount"]) for line in pricing["lines"] + pricing["pass_through"]),
                              sorted(result.priced.amounts)) if a != b]


def test_company_pricing_comparison():
    case = next(c for c in priced_cases() if "explains_difference" in expected(c)["exact"])
    exact = expected(case)["exact"]
    pricing, company = exact["pricing"], exact["explains_difference"]
    permit = Decimal(pricing["pass_through"][0]["amount"])
    total = Decimal(pricing["subtotal"]) * (1 + Decimal(company["company_markup_percent"]) / 100) + permit
    assert cents(total) == Decimal(company["company_pricing_total"])


def test_nte_context():
    case = next(c for c in CASES if "nte_context" in expected(c)["exact"])
    nte = expected(case)["exact"]["nte_context"]
    assert Decimal(nte["spent"]) / Decimal(nte["ceiling"]) * 100 == Decimal(nte["percent"])
    assert Decimal(nte["percent"]) > Decimal(nte["notice_point"])


def test_allowance():
    case = next(c for c in CASES if "allowance" in expected(c)["exact"])
    allowance = expected(case)["exact"]["allowance"]
    assert Decimal(allowance["amount"]) - Decimal(allowance["selected"]) == Decimal(allowance["under"])


def citations():
    return [(c.name, item) for c in CASES for item in expected(c)["exact"].get("citations", [])]


@pytest.mark.parametrize("name,citation", citations(), ids=lambda v: v if isinstance(v, str) else v["id"])
def test_citation_resolves(name, citation):
    target = citation["id"]
    policy = next((CORPUS / "docs").glob("POL-01_*.md")).read_text()
    if re.search(rf"^### \d+\. {re.escape(target)} —", policy, re.M):
        return
    files = list((CORPUS / "docs").glob(f"{target}_*.md"))
    assert files, f"{target} isn't a rule or a document"
    if "section" in citation:
        assert re.search(rf"^#+ (Section )?{re.escape(citation['section'])}[. ]", files[0].read_text(), re.M), \
            f"{target} has no section {citation['section']}"


@pytest.mark.parametrize("case", priced_cases(), ids=lambda c: c.name)
def test_company_values_match_the_package(case):
    values = yaml.safe_load((CORPUS / "company/config.yaml").read_text())["values"]
    rates = yaml.safe_load((CORPUS / "company/pricing/rates.yaml").read_text())["labor_billing_rates"]
    for line in expected(case)["exact"]["pricing"]["lines"]:
        if "rate" in line:
            assert any(Decimal(str(r["rate"])) == Decimal(line["rate"]) for r in rates)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_grade_the_pipeline_response(case):
    pytest.skip("the drafting pipeline doesn't exist yet (build step 5)")
