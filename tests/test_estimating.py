"""Estimating from cost history (build step 5c), with the fictional company's small cost history only.

A draft priced from rough notes is a suggestion: every line has one source, code does every number (prices from
cost history at COST-RANGE-POINT, takeoffs, waste), and the PM checklist shows what to check.
"""
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from construction_os.drafting import FakeDraftingClient, draft_pco, tool_turn
from construction_os.drafting.client import ModelTurn
from construction_os.drafting.takeoff import compute
from construction_os.pricing import cost_history
from construction_os.projects import FileProjectStore
from construction_os.rendering import document_text, load_package

FIXTURE = Path(__file__).parent / "fixtures" / "example_package"
NOTES = ("The Owner wants two pantries lined in pine beadboard, each about six by four feet, with a shelving system in each. "
         "Protect the floors and clean up daily. Demo the old shelving first.")


def D(value):
    return Decimal(str(value))


@pytest.fixture
def package(example_root):
    return load_package(example_root)


@pytest.fixture
def store(tmp_path):
    return FileProjectStore(tmp_path / "projects")


def known(value):
    return {"status": "known", "value": value}


def unknown(question):
    return {"status": "unknown", "question": question}


def na(reason="From the takeoff."):
    return {"status": "not_applicable", "reason": reason}


def line(category, description, unit, basis, division="06", trade="carpentry", **extra):
    base = {"category": category, "trade": trade, "division": known(division), "description": description,
            "quantity": na(), "unit": known(unit), "basis": basis, "permit": "not_applicable"}
    base.update(extra)
    return base


def proposal():
    """A fictional two-pantry job, estimated the way the instructions ask."""
    return {
        "header": {"project_address": unknown("Address?"), "owner": unknown("Who is the owner?"),
                   "contract_reference": unknown("Contract?"), "location": known("Two pantries"),
                   "trades": known(["Demolition", "Carpentry"]), "change_event_ref": unknown("CE?"),
                   "requested_approval_date": unknown("By when?"), "cause_category": known("Owner-Directed")},
        "description": known("the Contractor proposes to line two (2) pantries in beadboard with a shelving system in each."),
        "reason": known("The Owner asked for beadboard pantries."),
        "scope": {"inclusions": known(["Line two (2) pantries in beadboard.", "Install a shelving system in each pantry."]),
                  "deleted_scope": known([]), "assumptions": []},
        "clarifications_and_exclusions": [
            {"prompt": "concealed_conditions", "trades": ["all"], "use_default": True},
            {"prompt": "permits", "trades": ["all"], "disposition": "not_applicable", "text": "No permit."}],
        "pricing_basis": "lump_sum", "nte_amount": na("Lump sum."),
        "measurements": [{"id": "pantry", "label": "Each of two (2) pantries", "count": 2, "length_ft": 6, "width_ft": 4,
                          "height_ft": 8, "source": "estimate", "basis": "about six by four feet in the notes; "
                                                                           "an 8 ft ceiling assumed"}],
        "cost_lines": [
            line("labor", "Remove the existing shelving", "DAY", "rate_x_duration", division="02",
                 trade="demolition", role="Crew", duration={"days": 1, "basis": "two small pantries"}),
            line("materials", "Demo bags, blades and PPE", "LS", "unit_cost", division="02", trade="demolition",
                 quantity=known(1), cost_item={"id": "CH-fx000004", "component": "material"}),
            line("materials", "Beadboard paneling, material", "SF", "unit_cost",
                 cost_item={"id": "CH-fx000001", "component": "material"},
                 takeoff={"parts": [{"measurement": "pantry", "kind": "wall_area", "walls": ["length", "length", "width"]},
                                    {"measurement": "pantry", "kind": "ceiling_area"}], "waste_class": "lumber_wood"}),
            line("labor", "Install beadboard paneling", "HR", "hours_x_rate", role="Carpenter",
                 duration={"hours": 12, "basis": "about six hours a pantry"}),
            line("materials", "Pantry shelving systems", "EA", "unit_cost",
                 cost_item={"id": "CH-fx000008", "component": "material"},
                 takeoff={"parts": [{"measurement": "pantry", "kind": "count"}]}),
            line("materials", "Floor protection", "ROLL", "unit_cost", division="01", trade="general",
                 quantity=known(2), quantity_basis="one roll a pantry path",
                 cost_item={"id": "CH-fx000005", "component": "material"}),
        ],
        "schedule": {"extension_days": known(0), "contract_duration_days": na("No extension."),
                     "critical_path": known("no"), "completion_date": known("No change"),
                     "authorization_by": unknown("By when?")},
        "architect_signs": unknown("Does the architect sign?"),
        "contract_language": [],
        "open_questions": [],
    }


def run(store, package, tmp_path, *turns, project_id="P-1", notes=NOTES):
    project = store.open(project_id) if store.exists(project_id) else store.create(project_id, "Pantries")
    path = tmp_path / f"{project_id}-notes.txt"
    path.write_text(notes)
    record = project.add_input("notes", path)
    client = FakeDraftingClient([t if isinstance(t, ModelTurn) else tool_turn(t) for t in turns])
    return draft_pco(project, package, [record], client), client


def last_feedback(client):
    content = client.requests[-1]["messages"][-1]["content"]
    return content if isinstance(content, str) else " ".join(c["content"] for c in content)


def lines_of(result):
    return {l.description: l for l in result.priced.lines}


# Cost history: retrieval and the range point

def test_candidates_filter_by_division_and_unit(package):
    found = cost_history.candidates(package, "06", "SF", "beadboard paneling")
    assert [i.id for i in found] == ["CH-fx000001"]
    assert cost_history.candidates(package, "02", "SF", "beadboard paneling") == []


def test_a_real_unit_cost_item_ranks_above_an_allowance(package):
    found = cost_history.candidates(package, "06", None, "beadboard paneling package")
    assert [i.id for i in found][:2] == ["CH-fx000001", "CH-fx000002"]          # the allowance matches more words
    assert found[1].allowance


def test_excluded_items_are_never_offered_and_offers_carry_no_prices(package):
    assert "CH-fx000010" not in [i.id for i in cost_history.candidates(package, "01", "HR", "site cleaning")]
    offer = cost_history.offer(cost_history.items(package)["CH-fx000001"])
    assert set(offer) == {"id", "description", "category", "division", "unit", "components", "data_points",
                          "allowance_derived"}
    assert "5" not in json.dumps({k: v for k, v in offer.items() if k != "id"})


def test_candidates_are_capped(package):
    assert len(cost_history.candidates(package, None, None, "", limit=3)) == 3


@pytest.mark.parametrize("point,expected", [("low", "5.00"), ("mid", "6.00"), ("high", "7.00")])
def test_the_range_point_sets_the_price(package, point, expected):
    package.values["COST-RANGE-POINT"] = point
    assert cost_history.price(cost_history.items(package)["CH-fx000001"], "material", package) == D(expected)


def test_a_single_point_item_uses_its_one_value_at_every_point(package):
    item = cost_history.items(package)["CH-fx000004"]
    for point in ("low", "mid", "high"):
        package.values["COST-RANGE-POINT"] = point
        assert cost_history.price(item, "material", package) == D(300)


# Takeoff math (code, never the model)

MEASURE = {"pantry": {"id": "pantry", "count": 2, "length_ft": 6, "width_ft": 4, "height_ft": 8}}


@pytest.mark.parametrize("parts,unit,expected", [
    ([{"measurement": "pantry", "kind": "floor_area"}], "SF", "48.00"),
    ([{"measurement": "pantry", "kind": "wall_area", "walls": ["length", "length", "width"]}], "SF", "256.00"),
    ([{"measurement": "pantry", "kind": "wall_area"}], "SF", "320.00"),
    ([{"measurement": "pantry", "kind": "perimeter"}], "LF", "40.00"),
    ([{"measurement": "pantry", "kind": "count"}], "EA", "2.00"),
    ([{"measurement": "pantry", "kind": "floor_area"}], "SQ", "0.48"),
])
def test_takeoff_quantities(package, parts, unit, expected):
    quantity, formula = compute({"parts": parts}, MEASURE, package.values, unit)
    assert quantity == D(expected) and formula


def test_takeoff_adds_the_waste_factor_from_config(package):
    takeoff = {"parts": [{"measurement": "pantry", "kind": "wall_area", "walls": ["length", "length", "width"]},
                         {"measurement": "pantry", "kind": "ceiling_area"}], "waste_class": "lumber_wood"}
    quantity, formula = compute(takeoff, MEASURE, package.values, "SF")
    assert quantity == D("334.40")                                     # (256 + 48) × 1.10
    assert "total 304 SF" in formula and "+ 10% waste (lumber_wood) = 334.4 SF" in formula
    package.values["WASTE-FACTORS"][0]["percent"] = 15
    assert compute(takeoff, MEASURE, package.values, "SF")[0] == D("349.60")


@pytest.mark.parametrize("takeoff,unit,error", [
    ({"parts": [{"measurement": "pantry", "kind": "floor_area"}]}, "LF", "can't be converted"),
    ({"parts": [{"measurement": "nope", "kind": "floor_area"}]}, "SF", "isn't in measurements"),
    ({"parts": [{"measurement": "pantry", "kind": "floor_area"}], "waste_class": "glass"}, "SF", "isn't in the company"),
    ({"parts": [{"measurement": "pantry", "kind": "floor_area"}, {"measurement": "pantry", "kind": "perimeter"}]}, "SF",
     "mix"),
])
def test_takeoff_errors(package, takeoff, unit, error):
    with pytest.raises(ValueError, match=error):
        compute(takeoff, MEASURE, package.values, unit)


def test_a_wall_area_needs_a_height(package):
    with pytest.raises(ValueError, match="height"):
        compute({"parts": [{"measurement": "m", "kind": "wall_area"}]},
                {"m": {"id": "m", "count": 1, "length_ft": 5, "width_ft": 4}}, package.values, "SF")


# The estimated draft, end to end

def test_an_estimated_draft_has_a_source_and_a_price_on_every_line(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, proposal())
    lines = lines_of(result)
    assert (lines["Remove the existing shelving"].quantity,
            lines["Remove the existing shelving"].amount) == (D(1), D("1500.00"))      # Crew 1 DAY at the table
    assert lines["Demo bags, blades and PPE"].amount == D("300.00")
    paneling = lines["Beadboard paneling, material"]
    assert (paneling.quantity, paneling.unit_price, paneling.amount) == (D("334.4"), D("6.00"), D("2006.40"))
    assert lines["Install beadboard paneling"].amount == D("1200.00")                   # 12 HR × 100
    assert lines["Pantry shelving systems"].amount == D("800.00")                    # 2 EA × 400 (mid)
    assert lines["Floor protection"].amount == D("240.00")                    # 2 ROLL × 120 (mid)
    assert result.priced.complete
    assert {l.evidence.source for l in result.priced.lines} == {"rate_table", "cost_history"}


def test_measurements_and_durations_fill_the_draft(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, proposal())
    draft = json.loads(result.draft_path.read_text())
    assert draft["basis_of_quantities"] == ["Each of two (2) pantries: approximately 6 ft × 4 ft, 8 ft high."]
    assert draft["schedule"]["activities"] == [{"activity": "Remove the existing shelving", "duration_days": 1}]
    assert draft["cost_lines"][2]["takeoff"]["result"] == "334.40"


def test_the_checklist_lists_every_estimate_and_cost_history_price(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, proposal())
    text = document_text(result.checklist_path)
    assert "Confirm field dimensions before sending." in text.split("Worth confirming")[0]     # Before sending
    assert "Estimated measurement: Each of two (2) pantries, 6 × 4 ft, 8 ft high. Basis: about six by four feet" in text
    assert "Quantity from takeoff (estimated measurements): Beadboard paneling, material: 2 × (6 + 6 + 4 ft)" in text
    assert "Estimated duration: Remove the existing shelving: 1 days. Basis: two small pantries" in text
    assert "Estimated quantity: Floor protection: 2 ROLL. Basis: one roll a pantry path" in text
    assert ('Beadboard paneling, material: Cost history CH-fx000001: "Pine beadboard paneling", '
            "material $6.00/SF at the mid point; range $5.00–$7.00; 3 data points.") in text
    assert "Demo bags, blades and PPE: Cost history CH-fx000004" in text and "no range; 1 data point." in text


def test_the_owners_pco_shows_none_of_the_estimating_detail(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, proposal())
    text = document_text(result.docx_path)
    for word in ("CH-fx", "data point", "cost history", "waste", "Estimated", "Confirm field dimensions"):
        assert word not in text
    assert "Each of two (2) pantries: approximately 6 ft × 4 ft, 8 ft high." in text           # basis of quantities


def test_an_allowance_derived_price_is_marked_on_the_checklist(store, package, tmp_path):
    allowance = proposal()
    allowance["cost_lines"][2] = line("materials", "Beadboard paneling package", "LS", "unit_cost",
                                      quantity=known(1), cost_item={"id": "CH-fx000002", "component": "material"})
    result, _ = run(store, package, tmp_path, allowance)
    text = document_text(result.checklist_path)
    assert "allowance-derived (a past lump allowance, not a contract allowance)" in text
    assert "allowance" not in document_text(result.docx_path).lower()


def test_a_measurement_from_the_notes_needs_no_confirmation(store, package, tmp_path):
    from_notes = proposal()
    from_notes["measurements"][0].update(source="notes", input="notes-1", excerpt="each about six by four feet")
    from_notes["measurements"][0].pop("basis")
    result, _ = run(store, package, tmp_path, from_notes)
    text = document_text(result.checklist_path)
    assert "Confirm field dimensions" not in text and "(measurements from the notes)" in text


# What the checks reject

@pytest.mark.parametrize("change,error", [
    (lambda p: p["cost_lines"][2]["cost_item"].update(id="CH-nope"), "doesn't exist"),
    (lambda p: p["cost_lines"][2].update(unit=known("LF")), "is per SF"),
    (lambda p: p["cost_lines"][3].update(cost_item={"id": "CH-fx000001", "component": "material"}), "rate table"),
    (lambda p: p["cost_lines"][2]["cost_item"].update(id="CH-fx000003"), "has no material price"),
    (lambda p: p["cost_lines"][2]["cost_item"].update(id="CH-fx000010"), "is excluded"),
    (lambda p: p["cost_lines"][2].update(basis="estimate"), "basis unit_cost"),
    (lambda p: p["cost_lines"][2].update(quantity=known(300)), "quantity comes from the takeoff"),
    (lambda p: p["measurements"][0].pop("basis"), "needs its basis"),
    (lambda p: p["measurements"][0].update(source="notes", input="notes-1", excerpt="six by six"), "word for word"),
    (lambda p: p["cost_lines"][2]["takeoff"].update(waste_class="glass"), "waste class 'glass'"),
    (lambda p: p["cost_lines"][0]["duration"].pop("days") or p["cost_lines"][0]["duration"].update(hours=8),
     "duration in days"),
])
def test_what_the_checks_reject(store, package, tmp_path, change, error):
    wrong = proposal()
    change(wrong)
    _, client = run(store, package, tmp_path, wrong, proposal())
    assert error in last_feedback(client)


def test_a_typed_amount_on_a_cost_history_line_is_rejected(store, package, tmp_path):
    notes = NOTES + " Beadboard is $9 a foot."
    wrong = proposal()
    wrong["cost_lines"][2]["amount"] = {"status": "cited", "input": "notes-1", "excerpt": "Beadboard is $9 a foot.", "value": 9}
    _, client = run(store, package, tmp_path, wrong, proposal(), notes=notes)
    assert "takes its price from the company's cost history; remove amount" in last_feedback(client)


# Self-performed vs subcontracted

def test_installed_subcontract_work_is_one_line_from_the_item_total(store, package, tmp_path):
    sub = proposal()
    sub["cost_lines"].append(line("subcontractors", "Sink rough-in", "EA", "unit_cost", division="22",
                                  trade="plumbing", quantity=known(1), cost_item={"id": "CH-fx000011", "component": "total"}))
    result, _ = run(store, package, tmp_path, sub)
    assert lines_of(result)["Sink rough-in"].amount == D("2000.00")
    assert "Possible double count" not in document_text(result.checklist_path)


def test_installed_work_beside_company_labor_in_the_same_trade_is_flagged(store, package, tmp_path):
    double = proposal()
    double["cost_lines"].append(line("subcontractors", "Installed beadboard", "SF", "unit_cost",
                                     quantity=known(100), cost_item={"id": "CH-fx000001", "component": "total"}))
    result, _ = run(store, package, tmp_path, double)
    assert "Possible double count" in document_text(result.checklist_path)


# General conditions: role × duration at rate, never more than the extension

def with_gc(base, days=None, extension=14):
    base["schedule"].update(extension_days=known(extension), contract_duration_days=known(100))
    gc = line("general_conditions", "Site supervision", "WK", "weeks_x_rate", division="01", trade="general",
              role="Superintendent")
    if days:
        gc["duration"] = {"days": days, "basis": "part-time on site"}
    base["cost_lines"].append(gc)
    return base


def test_general_conditions_default_to_the_extension(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, with_gc(proposal()))
    gc = lines_of(result)["Site supervision"]
    assert (gc.quantity, gc.amount) == (D("2.00"), D("3600.00"))                     # 14 days, 2 weeks × 1,800


def test_a_role_may_have_its_own_shorter_duration(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, with_gc(proposal(), days=7))
    gc = lines_of(result)["Site supervision"]
    assert (gc.quantity, gc.amount) == (D("1.00"), D("1800.00"))
    assert "Estimated duration: Site supervision: 7 days. Basis: part-time on site" in document_text(result.checklist_path)


def test_a_role_duration_beyond_the_extension_is_rejected(store, package, tmp_path):
    _, client = run(store, package, tmp_path, with_gc(proposal(), days=20), with_gc(proposal()))
    assert "20 days is more than the 14-day extension" in last_feedback(client)


def test_general_conditions_keep_the_threshold_rule(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, with_gc(proposal(), extension=5))
    assert not lines_of(result)["Site supervision"].included                         # 5 of 100 days: not more than 10%


# The model looks items up through a tool, and never sees prices in the prompt

def lookup_turn(query, block_id="look_1"):
    return ModelTurn(content=[{"type": "tool_use", "id": block_id, "name": "find_cost_items", "input": query}],
                     stop_reason="tool_use", model="fake-model")


def test_lookups_answer_with_candidates_then_the_proposal_is_checked(store, package, tmp_path):
    result, client = run(store, package, tmp_path,
                         lookup_turn({"division": "06", "unit": "SF", "query": "beadboard paneling"}), proposal())
    answer = client.requests[1]["messages"][-1]["content"][0]
    assert answer["type"] == "tool_result" and json.loads(answer["content"])[0]["id"] == "CH-fx000001"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["cost_history_lookups"] == [{"query": {"division": "06", "unit": "SF", "query": "beadboard paneling"},
                                                 "returned": ["CH-fx000001"]}]
    assert len(result.attempts) == 1                                              # lookups aren't attempts


def test_lookups_are_capped(store, package, tmp_path):
    turns = [lookup_turn({"query": "beadboard"}, f"look_{n}") for n in range(21)] + [proposal()]
    _, client = run(store, package, tmp_path, *turns)
    last_lookup_answer = client.requests[21]["messages"][-1]["content"][0]
    assert last_lookup_answer["is_error"] and "lookup limit (20)" in last_lookup_answer["content"]


def test_the_prompt_has_waste_classes_but_no_cost_history_or_prices(store, package, tmp_path):
    _, client = run(store, package, tmp_path, proposal())
    system = "\n".join(block["text"] for block in client.requests[0]["system"])
    assert "Waste classes for takeoffs" in system and "lumber_wood" in system
    assert "estimating:" in system and "breakdown:" in system                   # the company's line breakdown style
    assert "CH-fx" not in system and "beadboard paneling" not in system and "percent: 10" not in system
    assert {t["name"] for t in client.requests[0]["tools"]} == {"propose_pco_draft", "find_cost_items"}


# The success test: one value in the package changes the totals, with no code edit

def test_changing_the_range_point_or_a_waste_factor_changes_the_total(tmp_path, example_root):
    def total(root):
        store = FileProjectStore(tmp_path / f"projects-{root.name}-{len(list(tmp_path.iterdir()))}")
        result, _ = run(store, load_package(root), tmp_path, proposal())
        return result.priced.total

    base = total(example_root)
    config = example_root / "company/config.yaml"
    config.write_text(config.read_text().replace("COST-RANGE-POINT: mid", "COST-RANGE-POINT: high"))
    higher = total(example_root)
    config.write_text(config.read_text().replace("{class: lumber_wood, percent: 10,", "{class: lumber_wood, percent: 15,"))
    more_waste = total(example_root)
    assert base < higher < more_waste


# The live script grades an estimating case on structure, not a total (checked here offline, with the fake client)

STRUCTURE = {"exact": {"pricing_structure": {
    "required_scope": [{"item": "demolition", "division": "02", "any_of": ["demolition", "remove"]},
                       {"item": "paneling material", "division": "06", "category": "materials", "any_of": ["beadboard"]},
                       {"item": "paneling installation", "division": "06", "category": "labor", "any_of": ["beadboard"]}],
    "every_line_sourced": ["rate_table", "cost_history", "supplied_quote", "pm_notes", "estimate"],
    "arithmetic": "exact", "estimated_quantities_on_checklist": True}}}


def live_eval():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import live_eval
    return live_eval


def test_the_live_script_passes_a_well_structured_estimate(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, with_gc(proposal()))
    checks, _ = live_eval().compare_structure(STRUCTURE, result, package)
    assert [name for name, *_, passed in checks if not passed] == []
    assert "general conditions rule" in [name for name, *_ in checks]


def test_the_live_script_fails_a_missing_scope_item_or_an_unlisted_source(store, package, tmp_path):
    result, _ = run(store, package, tmp_path, proposal())
    expected = copy.deepcopy(STRUCTURE)
    expected["exact"]["pricing_structure"]["required_scope"].append(
        {"item": "dust barriers", "division": "01", "any_of": ["dust barrier"]})
    expected["exact"]["pricing_structure"]["every_line_sourced"] = ["rate_table"]
    checks, _ = live_eval().compare_structure(expected, result, package)
    assert {name for name, *_, passed in checks if not passed} == {"scope: dust barriers", "every line sourced"}
