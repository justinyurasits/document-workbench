"""Say each thing once: the wording checks on a proposal, and what code adds. Fictional fixture company only."""
import copy
import json

import pytest

from construction_os.drafting.wording import line_errors, measurement_errors, repeat_errors, wording_errors
from construction_os.rendering import document_text, load_package
from test_estimating import known, last_feedback, proposal, run, store  # noqa: F401  (store is a fixture)


@pytest.fixture
def package(example_root):
    return load_package(example_root)


def labelled(description, **changes):
    """The estimating proposal with one more line: a quantity-1 LS line with this description."""
    base = proposal()
    line = {"category": "materials", "trade": "carpentry", "division": known("06"), "description": description,
            "quantity": known(1), "unit": known("LS"), "basis": "estimate", "permit": "not_applicable",
            "amount": {"status": "unknown", "question": "What does it cost?"}}
    line.update(changes)
    base["cost_lines"].append(line)
    return base


# Line descriptions are short labels

@pytest.mark.parametrize("description", ["Carpentry: install trim", "carpentry - install trim", "Woodwork – install trim",
                                         "WOODWORK: install trim"])
def test_a_division_or_trade_prefix_is_rejected(package, description):
    errors = line_errors(labelled(description), package.definition)
    assert len(errors) == 1 and "starts with" in errors[0]


def test_a_division_name_that_is_part_of_the_label_is_fine(package):
    assert line_errors(labelled("Woodwork repairs at the stair"), package.definition) == []


@pytest.mark.parametrize("description", ["Crew, 2 days", "Paneling, 290 SF", "Carpentry labor, six (6) hours",
                                         "Debris removal, 2 loads", "Final cleaning, lump sum", "Paneling per SF",
                                         "Trim, 40 linear feet", "Cleanup 4 hrs"])
def test_a_quantity_next_to_a_unit_or_lump_sum_is_rejected(package, description):
    errors = line_errors(labelled(description), package.definition)
    assert len(errors) == 1 and "quantity or unit" in errors[0]


@pytest.mark.parametrize("description", ["1/2-inch drywall", "1x4 trim"])
def test_product_sizes_on_a_quantity_one_lump_sum_line_pass(package, description):
    """The loose rule: a number is rejected only next to a unit word, so product sizes don't cause retries."""
    assert wording_errors(labelled(description), package.definition) == []


def test_the_recorded_style_labels_pass(package):
    assert wording_errors(proposal(), package.definition) == []


# Measurements appear only in Basis of quantities

@pytest.mark.parametrize("where,text", [
    ("description", "the Contractor proposes to line two pantries, each 6 ft by 4 ft."),
    ("inclusion", "Line both pantries, about six by four feet."),
    ("assumption", "Ceilings are 8' high."),
    ("line", "Paneling for the 6 × 4 pantries"),
])
def test_a_measurement_outside_basis_of_quantities_is_rejected(where, text):
    wrong = proposal()
    if where == "description":
        wrong["description"] = known(text)
    elif where == "inclusion":
        wrong["scope"]["inclusions"]["value"].append(text)
    elif where == "assumption":
        wrong["scope"]["assumptions"].append({"kind": "existing_conditions", "text": text})
    else:
        wrong["cost_lines"][2]["description"] = text
    errors = measurement_errors(wrong)
    assert len(errors) == 1 and "Basis of quantities" in errors[0]


def test_sizes_are_fine_when_there_are_no_measurements():
    plain = proposal()
    plain["description"] = known("the Contractor proposes to line two pantries, each 6 ft by 4 ft.")
    plain.pop("measurements")
    assert measurement_errors(plain) == []


def test_a_number_that_is_not_a_measurement_is_fine():
    fine = proposal()
    fine["scope"]["inclusions"]["value"].append("Install two (2) shelving systems with 12 ft of rod.")
    assert measurement_errors(fine) == []


# Assumptions never repeat the scope

def test_an_assumption_that_repeats_an_inclusion_is_rejected(package):
    wrong = proposal()
    wrong["scope"]["inclusions"]["value"].append("Protect the floors and clean up daily.")
    wrong["scope"]["assumptions"].append({"kind": "access_logistics", "text": "protect the floors, and clean up daily"})
    errors = repeat_errors(wrong, package.definition)
    assert len(errors) == 1 and "repeats an inclusion" in errors[0]


def test_an_assumption_that_only_overlaps_an_inclusion_is_fine(package):
    fine = proposal()
    fine["scope"]["inclusions"]["value"].append("Protect the floors and clean up daily.")
    fine["scope"]["assumptions"].append({"kind": "access_logistics", "text": "Work is done on weekdays."})
    assert repeat_errors(fine, package.definition) == []


def test_writing_the_field_measurement_sentence_is_rejected(package):
    wrong = proposal()
    sentence = package.definition["estimating"]["field_measurement_assumption"]
    wrong["scope"]["assumptions"].append({"kind": "scope", "text": sentence})
    assert "code adds this sentence" in repeat_errors(wrong, package.definition)[0]


# Code adds the field-measurement assumption, from the package

def test_measurements_add_the_packages_field_measurement_assumption(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, proposal())
    draft = json.loads(result.draft_path.read_text())
    sentence = package.definition["estimating"]["field_measurement_assumption"]
    assert {"kind": "scope", "text": sentence} in draft["scope"]["assumptions"]
    text = document_text(result.docx_path)
    assert sentence in text and text.count("6 ft × 4 ft") == 1                    # the size only in Basis of quantities


def test_no_measurements_no_field_measurement_assumption(store, package, tmp_path):
    plain = proposal()
    plain.pop("measurements")
    for line in plain["cost_lines"]:
        if "takeoff" in line:
            line.pop("takeoff")
            line["quantity"] = known(2)
            line["quantity_basis"] = "two pantries"
    result, _ = run(store, package, tmp_path, plain)
    sentence = package.definition["estimating"]["field_measurement_assumption"]
    assert sentence not in document_text(result.docx_path)


# The model is told, and tries again

@pytest.mark.parametrize("change,error", [
    (lambda p: p["cost_lines"][2].update(description="Carpentry: paneling"), 'starts with "Carpentry"'),
    (lambda p: p["cost_lines"][0].update(description="Crew, 1 day"), "states a quantity or unit"),
    (lambda p: p.update(reason=known("Each pantry is 6 ft by 4 ft.")), "repeats a measurement"),
    (lambda p: p["scope"]["assumptions"].append({"kind": "access_logistics", "text": "Line two (2) pantries in beadboard."}),
     "repeats an inclusion"),
])
def test_the_model_is_told_what_to_fix(store, package, tmp_path, change, error):
    wrong = copy.deepcopy(proposal())
    change(wrong)
    result, client = run(store, package, tmp_path, wrong, proposal())
    assert error in last_feedback(client) and len(result.attempts) == 2


# No "lump sum" phrase: estimate lines get none, whatever the unit

@pytest.mark.parametrize("unit", ["SF", "EA", "LOAD", "LS"])
def test_an_estimate_line_gets_no_basis_phrase(store, package, tmp_path, unit):
    base = labelled("Trim", unit=known(unit), amount={"status": "unknown", "question": "What does it cost?"})
    result, _ = run(store, package, tmp_path, base)
    rows = [line for line in document_text(result.docx_path).splitlines() if line.startswith("Trim")]
    assert rows and all("(" not in row for row in rows)
