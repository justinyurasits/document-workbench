"""The pricing engine (build step 4). Company numbers come from the package, never from this file:
the acceptance cases read the corpus's expected.yaml, and the rest use the fictional fixture company."""
import json
import os
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from construction_os.pricing import find_evidence, price
from construction_os.rendering import document_text, load_package, render, render_checklist
from construction_os.validate import draft_schema_errors
from docx_helpers import pricing_table, summary_rows

CORPUS = Path(os.environ.get("CORPUS", Path(__file__).resolve().parents[2] / "construction-os-corpus"))
CASES = CORPUS / "tests" / "acceptance" / "cases"
FIXTURE = Path(__file__).parent / "fixtures" / "example_package"
needs_corpus = pytest.mark.skipif(not CASES.is_dir(), reason="package not found; set CORPUS")


def D(value):
    return Decimal(str(value))


def codes(priced, line=None):
    return [f.code for f in priced.flags if line is None or f.line == line]


@pytest.fixture
def fixture_root(tmp_path):
    root = tmp_path / "example_package"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def company(fixture_root):
    return load_package(fixture_root)


@pytest.fixture
def draft():
    return json.loads((FIXTURE / "samples/pco_example_draft.json").read_text())


def line(category, description, quantity, unit, unit_price, basis, source, reference="PM notes", role=None, trade="carpentry"):
    known = lambda v: {"status": "known", "value": v}
    unknown = {"status": "unknown", "question": "?"}
    result = {
        "category": category, "trade": trade, "division": known("01"), "description": description,
        "quantity": known(quantity) if quantity is not None else unknown,
        "unit": known(unit), "unit_price": known(unit_price) if unit_price is not None else unknown,
        "basis": basis,
        "evidence": {"source": source, "reference": known(reference) if reference else unknown,
                     "date": {"status": "not_applicable", "reason": "n/a"}},
        "attachment": {"status": "not_applicable", "reason": "None."}, "permit": "not_applicable",
    }
    if role:
        result["role"] = role
    return result


def schedule(draft, extension, duration):
    draft["schedule"]["extension_days"] = {"status": "known", "value": extension}
    draft["schedule"]["contract_duration_days"] = (
        {"status": "known", "value": duration} if duration is not None else {"status": "unknown", "question": "How long?"})
    return draft


def with_gc(draft):
    draft["cost_lines"] += [
        line("general_conditions", "Project management", None, "WK", None, "weeks_x_rate", "rate_table", "Company GC rate",
             role="Project Manager", trade="general"),
        line("general_conditions", "Supervision", None, "WK", None, "weeks_x_rate", "rate_table", "Company GC rate",
             role="Superintendent", trade="general"),
    ]
    return draft


# The acceptance cases, read from the package

def case(name):
    return yaml.safe_load((CASES / name / "expected.yaml").read_text())


def corpus_draft(name):
    return json.loads((CORPUS / name).read_text())


def assert_matches(priced, pricing):
    passed = [l for l in priced.lines if l.included and l.category in priced.pass_through_categories]
    marked = [l for l in priced.lines if l.included and l.category not in priced.pass_through_categories]
    assert len(marked) == len(pricing["lines"])
    for got, want in zip(marked, pricing["lines"]):
        assert got.category == want["category"] and got.amount == D(want["amount"])
        if "hours" in want:
            assert got.quantity == D(want["hours"]) and got.unit_price == D(want["rate"])
    assert [(l.category, l.amount) for l in passed] == [(p["category"], D(p["amount"])) for p in pricing["pass_through"]]
    assert priced.subtotal == D(pricing["subtotal"])
    assert [(c.name, c.rate, c.amount) for c in priced.components] == \
           [(m["name"], D(m["percent"]), D(m["amount"])) for m in pricing["markup"]]
    assert priced.markup == D(pricing["markup_total"])
    assert priced.total == D(pricing["total"]) and priced.complete


@needs_corpus
def test_case_1_added_lighting_is_exact():
    expected = case("01_added_lighting")
    priced = price(corpus_draft(expected["sample_draft"]), load_package(CORPUS))
    assert_matches(priced, expected["exact"]["pricing"])
    assert priced.total == D("5614.40")
    assert not [l for l in priced.lines if l.category == "general_conditions"] and "gc_missing_lines" not in codes(priced)


@needs_corpus
def test_case_4_contract_pricing_is_exact():
    expected = case("04_exterior_door_contract_pricing")
    exact = expected["exact"]
    priced = price(corpus_draft(expected["sample_draft"]), load_package(CORPUS))
    assert_matches(priced, exact["pricing"])
    assert priced.total == D("5737.00")
    assert priced.contract_reference and all(c.evidence.source == "contract" for c in priced.components)
    assert not exact["pricing"]["compounded"]
    assert all(c.amount == (priced.subtotal * c.rate / 100).quantize(D("0.01")) for c in priced.components)


@needs_corpus
def test_case_4_at_company_pricing_explains_the_difference():
    expected = case("04_exterior_door_contract_pricing")
    priced = price(corpus_draft(expected["sample_draft"]), load_package(CORPUS), use_contract=False)
    difference = expected["exact"]["explains_difference"]
    assert priced.total == D(difference["company_pricing_total"]) == D("6129.00")
    assert sum(c.rate for c in priced.components) == D(difference["company_markup_percent"])
    assert priced.contract_reference is None and all(c.evidence.source != "contract" for c in priced.components)


@needs_corpus
def test_case_4_carpenter_lump_sum_is_carried_without_hours():
    expected = case("04_exterior_door_contract_pricing")
    priced = price(corpus_draft(expected["sample_draft"]), load_package(CORPUS))
    carpenter = next(l for l in priced.lines if l.category == "labor")
    assert (carpenter.quantity, carpenter.unit_price, carpenter.amount) == (D(1), D(900), D("900.00"))
    assert carpenter.evidence.source == "pm_notes" and not codes(priced, carpenter.index)


@needs_corpus
def test_worked_example_from_the_design():
    priced = price(corpus_draft("samples/pco_sample_draft.json"), load_package(CORPUS))
    expected = yaml.safe_load((CORPUS / "samples/expected.yaml").read_text())["pco_sample_draft.json"]
    assert (priced.subtotal, priced.markup, priced.total) == (D(expected["subtotal"]), D(expected["markup"]), D(expected["total"]))
    assert priced.total == D("2625.70")


@needs_corpus
def test_case_1_carpenter_rate_comes_from_the_rate_table():
    priced = price(corpus_draft("samples/pco_test1_draft.json"), load_package(CORPUS))
    carpenter = next(l for l in priced.lines if l.category == "labor")
    assert carpenter.evidence.source == "rate_table" and carpenter.evidence.reference == "LABOR-BILLING-RATES, Carpenter"


# The fixture company: 2% + 8% + 5% on all categories, Carpenter 100/hour, GC PM 2100/week, Superintendent 1800/week

def test_fixture_draft_totals(company, draft):
    priced = price(draft, company)
    assert [l.amount for l in priced.lines] == [D("800.00"), D("1000.00"), D("500.00")]
    assert (priced.subtotal, priced.markup, priced.total) == (D("2300.00"), D("345.00"), D("2645.00"))
    assert priced.flags == []


# General conditions

def test_gc_exactly_at_the_threshold_is_not_added(company, draft):
    priced = price(with_gc(schedule(draft, 10, 100)), company)
    assert [l.included for l in priced.lines[3:]] == [False, False]
    assert codes(priced).count("gc_not_applicable") == 2 and priced.total == D("2645.00")


def test_gc_just_over_the_threshold_is_added_and_prorated(company, draft):
    priced = price(with_gc(schedule(draft, 11, 100)), company)
    pm, superintendent = priced.lines[3:]
    assert pm.quantity == D("1.57") and pm.unit_price == D(2100) and pm.amount == D("3297.00")
    assert superintendent.quantity == D("1.57")             # 11 / 7 = 1.5714, to two decimals, not whole weeks
    assert superintendent.amount == D("2826.00")            # 1.57 × 1800
    assert all(l.quantity * l.unit_price == l.amount for l in (pm, superintendent))
    assert superintendent.evidence.reference == "GC-WEEKLY-RATES, Superintendent"
    assert priced.subtotal == D("8423.00")
    assert [c.amount for c in priced.components] == [D("168.46"), D("673.84"), D("421.15")]
    assert priced.total == D("9686.45") and priced.complete and not codes(priced)


def test_gc_threshold_crossed_with_no_gc_lines_is_flagged(company, draft):
    priced = price(schedule(draft, 15, 100), company)
    assert codes(priced) == ["gc_missing_lines"] and priced.total == D("2645.00")


def test_gc_lines_without_the_threshold_crossed_are_flagged_and_excluded(company, draft):
    priced = price(with_gc(schedule(draft, 5, 100)), company)
    flagged = [f.line for f in priced.flags if f.code == "gc_not_applicable"]
    assert flagged == [3, 4] and priced.total == D("2645.00") and len(priced.amounts) == 3


def test_gc_unknown_duration_is_flagged_and_not_guessed(company, draft):
    priced = price(with_gc(schedule(draft, 10, None)), company)
    assert "gc_duration_unknown" in codes(priced)
    assert [l.amount for l in priced.lines[3:]] == [None, None] and not priced.complete


def test_gc_unknown_duration_with_no_gc_lines_is_flagged(company, draft):
    priced = price(schedule(draft, 10, None), company)
    assert codes(priced) == ["gc_duration_unknown"] and priced.complete


def test_gc_role_without_a_rate_is_flagged(company, draft):
    draft = with_gc(schedule(draft, 20, 100))
    draft["cost_lines"][4]["role"] = "Estimator"
    priced = price(draft, company)
    assert codes(priced, 4) == ["gc_rate_not_found", "unsupported_price"] and not priced.complete


def test_gc_quantity_that_differs_from_the_extension_is_flagged(company, draft):
    draft = with_gc(schedule(draft, 14, 100))
    draft["cost_lines"][3]["quantity"] = {"status": "known", "value": 3}
    priced = price(draft, company)
    assert codes(priced, 3) == ["gc_quantity_differs"] and priced.lines[3].amount == D("4200.00")


# Labor and evidence

def test_labor_rate_is_looked_up_from_the_table(company, draft):
    draft["cost_lines"][0]["unit_price"] = {"status": "unknown", "question": "What rate?"}
    carpenter = price(draft, company).lines[0]
    assert (carpenter.unit_price, carpenter.amount) == (D(100), D("800.00"))
    assert carpenter.evidence.source == "rate_table" and carpenter.evidence.unit == "hour"


def test_labor_rate_that_disagrees_uses_the_table_and_flags(company, draft):
    draft["cost_lines"][0]["unit_price"]["value"] = 95
    priced = price(draft, company)
    assert priced.lines[0].amount == D("800.00") and codes(priced, 0) == ["labor_rate_differs"]


def test_labor_lump_sum_with_no_hours_is_carried_as_given(company, draft):
    draft["cost_lines"].append(line("labor", "Carpenter labor", 1, "LS", 900, "estimate", "pm_notes", role="Carpenter"))
    priced = price(draft, company)
    assert (priced.lines[3].quantity, priced.lines[3].amount) == (D(1), D("900.00"))
    assert priced.lines[3].evidence.source == "pm_notes" and not codes(priced)


def test_labor_with_no_role_and_no_stated_source_is_blank_and_flagged(company, draft):
    del draft["cost_lines"][0]["role"]
    priced = price(draft, company)
    assert priced.lines[0].amount is None and codes(priced, 0) == ["unsupported_price"] and not priced.complete


def test_labor_with_no_table_row_keeps_a_price_whose_source_is_named(company, draft):
    draft["cost_lines"].append(line("labor", "Installer", 4, "HR", 80, "hours_x_rate", "pm_notes", "PM notes", role="Installer"))
    priced = price(draft, company)
    assert priced.lines[3].amount == D("320.00") and not codes(priced)


def test_labor_with_an_unnamed_source_is_blank(company, draft):
    draft["cost_lines"].append(line("labor", "Installer", 4, "HR", 80, "hours_x_rate", "pm_notes", None, role="Installer"))
    priced = price(draft, company)
    assert priced.lines[3].amount is None and codes(priced, 3) == ["unsupported_price"]


def test_labor_estimate_that_disagrees_with_the_table_is_carried_and_flagged(company, draft):
    draft["cost_lines"].append(line("labor", "Carpenter estimate", 2, "HR", 95, "estimate", "pm_notes", role="Carpenter"))
    priced = price(draft, company)
    assert priced.lines[3].amount == D("190.00") and codes(priced, 3) == ["labor_rate_differs"]


def test_labor_unit_that_differs_from_the_table_is_not_converted(company, draft):
    draft["cost_lines"].append(line("labor", "Crew", 8, "HR", 200, "hours_x_rate", "rate_table", "Company rate", role="Crew"))
    priced = price(draft, company)
    assert codes(priced, 3) == ["labor_unit_mismatch", "unsupported_price"] and priced.lines[3].amount is None


def test_find_evidence_returns_table_rows_then_the_drafts_own(company, draft):
    found = find_evidence(draft["cost_lines"][0], company, draft)
    assert [(e.source, e.rate, e.unit) for e in found] == [("rate_table", D(100), "hour"), ("rate_table", D(100), "hour")]
    assert found[0].reference == "LABOR-BILLING-RATES, Carpenter" and found[1].reference == "Company rate"
    assert find_evidence(draft["cost_lines"][1], company, draft)[0].source == "supplied_quote"


def test_every_line_keeps_distinguishable_evidence(company, draft):
    sources = [l.evidence.source for l in price(draft, company).lines]
    assert sources == ["rate_table", "supplied_quote", "pm_notes"]


# Markup, pass-through, tax, discount

def test_permit_passes_through_after_markup(company, draft):
    draft["cost_lines"].append(line("permits_and_fees", "Permit", 1, "LS", 200, "actual_fee", "pm_notes", trade="permit"))
    priced = price(draft, company)
    assert (priced.subtotal, priced.pass_through, priced.total) == (D("2300.00"), D("200.00"), D("2845.00"))


def test_components_apply_only_to_the_categories_they_name(company, draft):
    company.values["MARKUP-STRUCTURE"] = [{"name": "Labor fee", "rate": 10, "applies_to": ["labor"]},
                                          {"name": "Overhead", "rate": 5, "applies_to": ["all"]}]
    priced = price(draft, company)
    assert [c.amount for c in priced.components] == [D("80.00"), D("115.00")]


def test_a_tax_line_is_flagged(company, draft):
    draft["cost_lines"].append(line("materials", "Sales tax on materials", 1, "LS", 40, "estimate", "pm_notes"))
    priced = price(draft, company)
    assert codes(priced, 3) == ["tax_line"] and priced.lines[3].amount == D("40.00")


def test_no_tax_flag_when_the_company_shows_tax_separately(company, draft):
    company.values["TAX-TREATMENT"] = "separate_line"
    draft["cost_lines"].append(line("materials", "Sales tax", 1, "LS", 40, "estimate", "pm_notes"))
    assert not codes(price(draft, company))


def enable_discount(company):
    company.values["REPEAT-CLIENT-DISCOUNT"] = {"enabled": True, "rate": 5}


def test_discount_is_off_for_the_fixture_company(company, draft):
    draft["repeat_client"] = {"status": "known", "value": True}
    priced = price(draft, company)
    assert priced.discount is None and priced.total == D("2645.00")


def test_discount_applies_last_to_subtotal_plus_markup_only(company, draft):
    enable_discount(company)
    draft["repeat_client"] = {"status": "known", "value": True}
    draft["cost_lines"].append(line("permits_and_fees", "Permit", 1, "LS", 200, "actual_fee", "pm_notes", trade="permit"))
    priced = price(draft, company)
    assert priced.discount.amount == D("132.25")                  # 5% of 2645.00, not of the permit
    assert priced.total == D("2645.00") - D("132.25") + D("200.00")


def test_discount_not_applied_to_a_new_client(company, draft):
    enable_discount(company)
    draft["repeat_client"] = {"status": "known", "value": False}
    priced = price(draft, company)
    assert priced.discount is None and not codes(priced)


def test_discount_with_an_unknown_client_is_flagged(company, draft):
    enable_discount(company)
    priced = price(draft, company)
    assert priced.discount is None and codes(priced) == ["repeat_client_unknown"]


# The success test: one value in the package changes the totals, with no code edit

def test_changing_the_package_changes_every_affected_total(fixture_root, draft):
    before = price(draft, load_package(fixture_root))
    config = fixture_root / "company/config.yaml"
    config.write_text(config.read_text().replace("{name: Overhead, rate: 8,", "{name: Overhead, rate: 9,"))
    rates = fixture_root / "company/pricing/rates.yaml"
    rates.write_text(rates.read_text().replace('{role: "Carpenter", unit: hour, rate: 100,', '{role: "Carpenter", unit: hour, rate: 110,'))
    after = price(draft, load_package(fixture_root))
    assert [l.amount for l in before.lines] == [D("800.00"), D("1000.00"), D("500.00")]
    assert [l.amount for l in after.lines] == [D("880.00"), D("1000.00"), D("500.00")]
    assert [c.amount for c in after.components] == [D("47.60"), D("214.20"), D("119.00")]
    assert (after.subtotal, after.total) == (D("2380.00"), D("2760.80")) != (before.subtotal, before.total)


# The draft schema and the renderer

def test_a_draft_with_roles_gc_lines_and_repeat_client_is_valid(tmp_path, draft):
    draft = with_gc(schedule(draft, 11, 100))
    draft["repeat_client"] = {"status": "unknown", "question": "Is this a repeat client?"}
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft))
    assert draft_schema_errors(path) == []


def summary(path):
    return summary_rows(path), pricing_table(path)


def test_renderer_shows_prorated_gc_and_the_engines_rate(tmp_path, example_root, draft):
    draft = with_gc(schedule(draft, 11, 100))
    draft["cost_lines"][0]["unit_price"]["value"] = 95
    result = render(draft, load_package(example_root), tmp_path / "out.docx")
    rows, table = summary(result.path)
    assert rows["Total"] == "$9,686.45"
    texts = [[c.text for c in r.cells] for r in table.rows]
    assert any(r[2] == "Supervision (1.57 weeks at the house weekly rate)" and "1.57" in r and "$2,826.00" in r
               for r in texts)
    assert any(r[2] == "Sample carpentry (8 hours at the house rate)" and "$100.00" in r for r in texts)
    checklist = document_text(render_checklist(result, draft, load_package(example_root), tmp_path / "check.docx"))
    assert "differs from the company rate" in checklist


def test_renderer_leaves_excluded_gc_out_of_the_table(tmp_path, example_root, draft):
    draft = with_gc(schedule(draft, 5, 100))
    result = render(draft, load_package(example_root), tmp_path / "out.docx")
    rows, _ = summary(result.path)
    assert rows["Total"] == "$2,645.00" and "Supervision" not in document_text(result.path)
    assert "gc_not_applicable" in result.kinds()


def test_renderer_shows_the_discount(tmp_path, example_root, draft):
    package = load_package(example_root)
    enable_discount(package)
    draft["repeat_client"] = {"status": "known", "value": True}
    rows, _ = summary(render(draft, package, tmp_path / "out.docx").path)
    assert rows["Repeat-client discount (5%)"] == "-$132.25" and rows["Total"] == "$2,512.75"
