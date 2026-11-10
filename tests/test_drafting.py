"""LLM drafting (build step 5) with a fake Claude client returning recorded turns. No test calls the API.

The fictional Example Builders package prices the carpenter at 100/hour, with 2% + 8% + 5% markup on all
categories. Its numbers come from the fixture, never from here.
"""
import copy
import io
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfWriter

from construction_os.drafting import DraftingError, FakeDraftingClient, draft_pco, text_turn, tool_turn
from construction_os.drafting import cli
from construction_os.drafting.client import ModelTurn
from construction_os.projects import FileProjectStore, ProjectIsolationError
from construction_os.rendering import document_text, load_package
from construction_os.validate import draft_schema_errors
from docx_helpers import parties, summary_rows

CORPUS = Path(os.environ.get("CORPUS", Path(__file__).resolve().parents[2] / "construction-os-corpus"))
CASES = CORPUS / "tests" / "acceptance" / "cases"
RECORDED = Path(__file__).parent / "fixtures" / "drafting"
NOTES = ("Client wants two new outlets on the garage's north wall. Sparkline Electric (a fictional sub) quoted "
         "$640 for the outlets, wiring and permit. Our carpenter needs about 8 hours to open and patch the wall. "
         "No schedule impact.")


def D(value):
    return Decimal(str(value))


@pytest.fixture
def package(example_root):
    return load_package(example_root)


@pytest.fixture
def store(tmp_path):
    return FileProjectStore(tmp_path / "projects")


def add_notes(project, tmp_path, text=NOTES, name="notes.txt"):
    path = tmp_path / f"{project.id}-{name}"
    path.write_text(text)
    return project.add_input("notes", path)


def known(value):
    return {"status": "known", "value": value}


def unknown(question):
    return {"status": "unknown", "question": question}


def proposal():
    return {
        "header": {
            "project_address": unknown("What is the project address?"),
            "owner": unknown("Who is the owner?"),
            "contract_reference": unknown("Which contract does this change?"),
            "location": known("Garage, north wall"),
            "trades": known(["Electrical", "Carpentry"]),
            "change_event_ref": unknown("Which change event?"),
            "requested_approval_date": unknown("By when?"),
            "cause_category": known("Owner-Directed"),
        },
        "description": known("the Contractor proposes to add two (2) outlets on the north wall of the garage. This work "
                             "is not included in the Contract scope of work."),
        "reason": known("The client asked for them."),
        "scope": {"inclusions": known(["Two outlets with wiring", "Open and patch the wall"]),
                  "deleted_scope": known([]), "assumptions": ["Carpenter hours are the PM's estimate."]},
        "clarifications_and_exclusions": [
            {"prompt": "concealed_conditions", "trades": ["all"], "disposition": "excluded",
             "text": "Concealed conditions found on opening are excluded."},
            {"prompt": "permits", "trades": ["electrical"], "disposition": "clarified",
             "text": "The electrical permit is included in the electrical quote."},
            {"prompt": "permits", "trades": ["carpentry"], "disposition": "not_applicable", "text": "No permit."},
        ],
        "pricing_basis": "lump_sum",
        "nte_amount": {"status": "not_applicable", "reason": "Lump sum."},
        "cost_lines": [
            {"category": "subcontractors", "trade": "electrical", "division": known("26"),
             "description": "Electrical: two outlets and wiring", "quantity": known(1), "unit": known("LS"),
             "basis": "quote", "permit": "included_in_quote",
             "amount": {"status": "cited", "input": "notes-1", "excerpt": "quoted $640 for the outlets", "value": 640}},
            {"category": "labor", "role": "Carpenter", "trade": "carpentry", "division": known("06"),
             "description": "Carpenter: open and patch the wall", "quantity": known(8), "unit": known("HR"),
             "basis": "hours_x_rate", "permit": "not_applicable"},
        ],
        "schedule": {"extension_days": known(0),
                     "contract_duration_days": {"status": "not_applicable", "reason": "No extension."},
                     "critical_path": known("no"), "completion_date": known("No change"),
                     "authorization_by": unknown("By what date must the client authorize this?")},
        "architect_signs": unknown("Does the architect sign this PCO?"),
        "contract_language": [],
        "open_questions": ["Which outlet type did the client choose?"],
    }


def run(store, package, tmp_path, *proposals, project_id="P-001", text=NOTES):
    project = store.open(project_id) if store.exists(project_id) else store.create(project_id, "Garage outlets")
    record = add_notes(project, tmp_path, text)
    client = FakeDraftingClient([p if isinstance(p, ModelTurn) else tool_turn(p) for p in proposals])
    return draft_pco(project, package, [record], client), client, project


def flag_kinds(result):
    return [f.kind for f in result.flags]


def last_feedback(client):
    content = client.requests[-1]["messages"][-1]["content"]
    return content if isinstance(content, str) else content[0]["content"]


# End to end

def test_valid_output_is_priced_rendered_and_recorded(store, package, tmp_path):
    result, client, project = run(store, package, tmp_path, proposal())
    assert result.folder.name == "v001" and len(client.requests) == 1
    assert draft_schema_errors(result.draft_path) == []
    assert [line.amount for line in result.priced.lines] == [D("640.00"), D("800.00")]
    assert (result.priced.subtotal, result.priced.markup, result.priced.total) == (D("1440.00"), D("216.00"), D("1656.00"))
    assert result.docx_path.exists() and result.checklist_path.exists()
    assert summary_rows(result.docx_path)["Total"] == "$1,656.00"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["model"]["requested"] == "fake-model" and manifest["model"]["prompt_version"].startswith("pco-draft-1+")
    assert manifest["definition"]["sha256"] and manifest["definition"]["version"] is None
    assert manifest["inputs"][0]["id"] == "notes-1" and len(manifest["inputs"][0]["sha256"]) == 64
    sources = {s["id"]: s for s in manifest["sources"]}
    assert sources["AUTH-WRITTEN"]["kind"] == "rule" and sources["AUTH-WRITTEN"]["applies"] is True
    assert sources["TAX-TREATMENT"]["applies"] is False and sources["APPROVAL-SPEND"]["applies"] is True
    assert manifest["parameters"]["MARKUP-PRESENTATION"] == "itemized"
    assert manifest["pricing"]["total"] == "1656.00" and manifest["attempts"][0]["errors"] == []


def test_each_draft_is_a_new_version(store, package, tmp_path):
    run(store, package, tmp_path, proposal())
    later = proposal()
    later["cost_lines"][0]["amount"]["input"] = "notes-2"     # the second request's notes are a new input
    second, _, project = run(store, package, tmp_path, later)
    assert second.folder.name == "v002" and project.drafts() == ["v001", "v002"]


def test_the_model_is_asked_from_the_package_and_never_sees_rates(store, package, tmp_path):
    _, client, _ = run(store, package, tmp_path, proposal())
    system = "\n".join(block["text"] for block in client.requests[0]["system"])
    assert "Carpenter (per hour)" in system and "Project Manager (per week)" in system
    assert "Example Builders starts changed work only after" in system          # the fixture's AUTH-WRITTEN
    assert "concealed_conditions" in system and "Field Condition" in system      # the fixture's definition
    assert "rate: 100" not in system and "2100" not in system and "Company fee" not in system
    assert client.requests[0]["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert client.requests[0]["tools"][0]["name"] == "propose_pco_draft"


# Validation and the single retry

def test_invalid_output_then_a_successful_retry(store, package, tmp_path):
    broken = proposal()
    del broken["description"]
    result, client, _ = run(store, package, tmp_path, broken, proposal())
    assert len(client.requests) == 2 and len(result.attempts) == 2
    feedback = client.requests[1]["messages"][-1]["content"][0]
    assert feedback["type"] == "tool_result" and feedback["is_error"] and "description" in feedback["content"]
    assert client.requests[1]["messages"][-2]["role"] == "assistant"
    assert result.priced.total == D("1656.00")


def test_two_invalid_outputs_stop_with_a_clear_error(store, package, tmp_path):
    broken = proposal()
    del broken["description"]
    with pytest.raises(DraftingError, match="failed validation 2 times; no draft was written") as error:
        run(store, package, tmp_path, broken, copy.deepcopy(broken))
    assert "attempt 1" in str(error.value) and "attempt 2" in str(error.value) and "description" in str(error.value)
    assert store.open("P-001").drafts() == []


def test_no_tool_call_is_retried_with_a_reminder(store, package, tmp_path):
    result, client, _ = run(store, package, tmp_path, text_turn("Here is the draft..."), proposal())
    assert "Call propose_pco_draft exactly once" in client.requests[1]["messages"][-1]["content"]
    assert result.priced.complete


def test_a_refusal_stops_without_a_retry(store, package, tmp_path):
    refused = ModelTurn(content=[], stop_reason="refusal", model="fake-model")
    with pytest.raises(DraftingError, match="declined"):
        run(store, package, tmp_path, refused, proposal())


# Prices in the model's output are rejected, never used

def test_a_unit_price_from_the_model_is_rejected_and_the_table_rate_is_used(store, package, tmp_path):
    priced = proposal()
    priced["cost_lines"][1]["unit_price"] = known(150)
    result, client, _ = run(store, package, tmp_path, priced, proposal())
    assert "unit_price" in last_feedback(client)
    carpenter = result.priced.lines[1]
    assert (carpenter.unit_price, carpenter.amount) == (D(100), D("800.00"))
    assert "150" not in result.draft_path.read_text()


def test_a_total_from_the_model_is_rejected(store, package, tmp_path):
    priced = proposal()
    priced["total"] = 2000
    _, client, _ = run(store, package, tmp_path, priced, proposal())
    assert "total" in last_feedback(client)


def test_an_amount_on_a_rate_table_line_is_rejected(store, package, tmp_path):
    priced = proposal()
    priced["cost_lines"][1]["amount"] = {"status": "cited", "input": "notes-1", "excerpt": "about 8 hours", "value": 8}
    _, client, _ = run(store, package, tmp_path, priced, proposal())
    assert "takes its rate from the company table" in last_feedback(client)


# Roles, quantities and cited amounts

def test_a_role_not_in_the_table_is_rejected(store, package, tmp_path):
    invented = proposal()
    invented["cost_lines"][1]["role"] = "Finish Carpenter"
    honest = proposal()
    del honest["cost_lines"][1]["role"]
    honest["open_questions"].append("Which role prices the wall work?")
    result, client, _ = run(store, package, tmp_path, invented, honest)
    assert "'Finish Carpenter' isn't in the labor roles list" in last_feedback(client)
    assert result.priced.lines[1].amount is None and not result.priced.complete
    assert "unsupported_price" in flag_kinds(result) and "incomplete_pricing" in flag_kinds(result)
    assert "Which role prices the wall work?" in document_text(result.checklist_path)


def test_an_unknown_quantity_leaves_the_line_blank_with_a_question(store, package, tmp_path):
    vague = proposal()
    vague["cost_lines"][1]["quantity"] = unknown("How many carpenter hours will the wall take?")
    result, _, _ = run(store, package, tmp_path, vague)
    assert result.priced.lines[1].amount is None and not result.priced.complete
    assert "incomplete_pricing" in flag_kinds(result)
    assert "How many carpenter hours will the wall take?" in document_text(result.checklist_path)


def test_an_excerpt_not_in_the_input_is_rejected(store, package, tmp_path):
    misquoted = proposal()
    misquoted["cost_lines"][0]["amount"]["excerpt"] = "quoted $640 for everything"
    _, client, _ = run(store, package, tmp_path, misquoted, proposal())
    assert "isn't in notes-1 word for word" in last_feedback(client)


def test_a_figure_not_written_as_a_plain_number_is_rejected(store, package, tmp_path):
    text = NOTES.replace("$640", "2.85k")
    shorthand = proposal()
    shorthand["cost_lines"][0]["amount"] = {"status": "cited", "input": "notes-1", "excerpt": "quoted 2.85k",
                                            "value": 2850}
    honest = proposal()
    honest["cost_lines"][0]["amount"] = unknown("The electrical quote says 2.85k: what is the exact amount?")
    result, client, _ = run(store, package, tmp_path, shorthand, honest, text=text)
    assert "isn't a number written in the excerpt" in last_feedback(client)
    assert result.priced.lines[0].amount is None
    assert "what is the exact amount?" in document_text(result.checklist_path)


def test_a_quote_mentioned_only_in_the_notes_is_pm_notes_and_asks_for_the_quote(store, package, tmp_path):
    result, _, _ = run(store, package, tmp_path, proposal())
    line = json.loads(result.draft_path.read_text())["cost_lines"][0]
    assert (line["basis"], line["evidence"]["source"]) == ("quote", "pm_notes")
    draft = json.loads(result.draft_path.read_text())
    assert line["attachment"]["status"] == "not_applicable" and draft["attachments"] == [] and draft["backup"] == []
    assert "Written quote on file: Electrician's quote" in document_text(result.checklist_path)
    assert "Exhibit" not in document_text(result.docx_path)


def test_a_supplied_quote_file_is_cited_as_a_supplied_quote_with_a_letter(store, package, tmp_path):
    project = store.create("P-002", "Garage")
    record = add_notes(project, tmp_path)
    quote = tmp_path / "sparkline-quote.txt"
    quote.write_text("Sparkline Electric (fictional). Garage outlets: $640.00 including permit.")
    quote_record = project.add_input("quote", quote)
    cited = proposal()
    cited["cost_lines"][0]["amount"] = {"status": "cited", "input": "quote-1", "excerpt": "Garage outlets: $640.00",
                                        "value": 640}
    result = draft_pco(project, package, [record, quote_record], FakeDraftingClient([tool_turn(cited)]))
    draft = json.loads(result.draft_path.read_text())
    line = draft["cost_lines"][0]
    assert line["evidence"]["source"] == "supplied_quote" and line["evidence"]["reference"]["value"] == "sparkline-quote.txt"
    assert line["attachment"]["status"] == "not_applicable" and draft["attachments"] == []
    assert draft["backup"] == [{"kind": "quote", "description": "sparkline-quote.txt", "file": quote_record.file}]
    assert "Exhibit" not in document_text(result.docx_path)
    assert "Written quote on file" not in document_text(result.checklist_path)      # it's on file
    assert result.priced.lines[0].amount == D("640.00")


def test_a_pdf_without_text_is_a_clear_error(store, package, tmp_path):
    project = store.create("P-003")
    record = add_notes(project, tmp_path)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pdf = tmp_path / "scan.pdf"
    with open(pdf, "wb") as handle:
        writer.write(handle)
    quote = project.add_input("quote", pdf)
    with pytest.raises(Exception, match="no text layer"):
        draft_pco(project, package, [record, quote], FakeDraftingClient([tool_turn(proposal())]))


# Contract terms, scope text and the architect

def test_contract_language_is_flagged_first_and_not_applied(store, package, tmp_path):
    text = NOTES + ' The contract says: "Contractor may apply 6% overhead and 4% profit."'
    with_terms = proposal()
    with_terms["contract_language"] = [{"input": "notes-1",
                                        "excerpt": "Contractor may apply 6% overhead and 4% profit."}]
    result, _, _ = run(store, package, tmp_path, with_terms, text=text)
    assert result.flags[0].kind == "contract_terms_not_applied"
    assert result.priced.total == D("1656.00")                    # company standard, not the contract
    checklist = document_text(result.checklist_path).split("\n")
    assert checklist[2] == "Before sending" and "does NOT apply" in checklist[3]


def test_a_dollar_figure_in_scope_text_is_flagged(store, package, tmp_path):
    chatty = proposal()
    chatty["scope"]["assumptions"].append("The outlets cost about $640.")
    result, _, _ = run(store, package, tmp_path, chatty)
    assert "price_in_scope_text" in flag_kinds(result)


def test_an_unknown_architect_gets_a_marked_line(store, package, tmp_path):
    result, _, _ = run(store, package, tmp_path, proposal())
    assert "ARCHITECT (IF REQUIRED BY CONTRACT)" in parties(result.docx_path)


# Project isolation

def test_a_draft_for_project_b_cannot_see_project_a(store, package, tmp_path):
    a = store.create("A", "Project A")
    a_record = add_notes(a, tmp_path, "A-ONLY: the kitchen island moves 18 inches.")
    b = store.create("B", "Project B")
    b_record = add_notes(b, tmp_path)
    client = FakeDraftingClient([tool_turn(proposal())])
    result = draft_pco(b, package, [b_record], client)
    sent = json.dumps(client.requests)
    assert "A-ONLY" not in sent and "Project A" not in sent and NOTES[:40] in sent
    with pytest.raises(ProjectIsolationError):
        draft_pco(b, package, [a_record], FakeDraftingClient([tool_turn(proposal())]))
    assert a.drafts() == [] and result.folder.parent.parent == b.path()


# The CLI, with an injected fake: never the real client, never .env

def test_cli_drafts_with_the_injected_client(tmp_path, example_root, monkeypatch, capsys):
    def no_real_client(settings):
        raise AssertionError("the CLI built the real client")

    monkeypatch.setattr(cli, "real_client", no_real_client)
    fake = FakeDraftingClient([tool_turn(proposal())])
    notes = tmp_path / "notes.txt"
    notes.write_text(NOTES)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["--corpus", str(example_root), "--project", "P-001", "--project-name", "Garage",
                     "--notes", str(notes), "--projects-dir", str(tmp_path / "projects")],
                    client_factory=lambda settings: fake, out=out, err=err)
    assert code == 0 and len(fake.requests) == 1, err.getvalue()
    printed = out.getvalue()
    assert "PCO:" in printed and "pco.docx" in printed and "Manifest:" in printed and "Total: $1,656.00" in printed
    assert "[missing_input]" in printed


def test_cli_reports_a_drafting_failure(tmp_path, example_root):
    broken = proposal()
    del broken["description"]
    notes = tmp_path / "notes.txt"
    notes.write_text(NOTES)
    err = io.StringIO()
    code = cli.main(["--corpus", str(example_root), "--project", "P-001", "--notes", str(notes),
                     "--projects-dir", str(tmp_path / "projects")],
                    client_factory=lambda settings: FakeDraftingClient([tool_turn(broken), tool_turn(broken)]),
                    out=io.StringIO(), err=err)
    assert code == 2 and "Drafting failed" in err.getvalue()


# The package's own cases, with recorded model output (skipped without the package)

needs_corpus = pytest.mark.skipif(not CASES.is_dir(), reason="package not found; set CORPUS")


def corpus_case(store, name, recorded):
    corpus = load_package(CORPUS)
    project = store.create(name)
    record = project.add_input("notes", CASES / name / "request.md")
    client = FakeDraftingClient([tool_turn(json.loads((RECORDED / recorded).read_text()))])
    return draft_pco(project, corpus, [record], client)


@needs_corpus
def test_case_1_through_the_pipeline_totals_the_expected_amount(store):
    result = corpus_case(store, "01_added_lighting", "case1_proposal.json")
    assert result.priced.total == D("5614.40") and "contract_terms_not_applied" not in flag_kinds(result)


@needs_corpus
def test_case_4_through_the_pipeline_prices_at_company_standard_and_flags_the_contract(store):
    result = corpus_case(store, "04_exterior_door_contract_pricing", "case4_proposal.json")
    assert result.priced.total == D("6129.00")
    assert result.flags[0].kind == "contract_terms_not_applied"


def test_tests_cannot_build_the_real_client():
    from construction_os.drafting import AnthropicDraftingClient, Settings

    with pytest.raises(AssertionError, match="real Claude client"):
        AnthropicDraftingClient(Settings())


# PCO polish: approved clarification defaults, exhibits, the request record and the checklist

FIXTURE_DEFAULT = "Example Builders excludes hidden conditions found on opening; they are priced separately."


def test_a_default_clarification_is_inserted_verbatim(store, package, tmp_path):
    defaulted = proposal()
    defaulted["clarifications_and_exclusions"][0] = {"prompt": "concealed_conditions", "trades": ["all"],
                                                     "use_default": True}
    result, _, _ = run(store, package, tmp_path, defaulted)
    item = json.loads(result.draft_path.read_text())["clarifications_and_exclusions"][0]
    assert item == {"prompt": "concealed_conditions", "trades": ["all"], "disposition": "excluded", "text": FIXTURE_DEFAULT}
    assert FIXTURE_DEFAULT in document_text(result.docx_path)


@pytest.mark.parametrize("item,error", [
    ({"prompt": "permits", "trades": ["all"], "use_default": True}, "has no company default"),
    ({"prompt": "concealed_conditions", "trades": ["all"], "use_default": True, "text": "Reworded."}, "leave disposition and text out"),
    ({"prompt": "concealed_conditions", "trades": ["all"]}, "give disposition and text"),
])
def test_a_misused_default_is_rejected(store, package, tmp_path, item, error):
    wrong = proposal()
    wrong["clarifications_and_exclusions"][0] = item
    _, client, _ = run(store, package, tmp_path, wrong, proposal())
    assert error in last_feedback(client)


def test_supplied_correspondence_and_drawings_are_checked_exhibits(store, package, tmp_path):
    project = store.create("P-010", "Garage")
    notes = add_notes(project, tmp_path)
    email = tmp_path / "owner-email.txt"
    email.write_text("Please add two outlets in the garage. — the Owner")
    drawing = tmp_path / "layout.png"
    drawing.write_bytes(b"\x89PNG not really an image")
    records = [notes, project.add_input("correspondence", email), project.add_input("drawing", drawing)]
    client = FakeDraftingClient([tool_turn(proposal())])
    result = draft_pco(project, package, records, client)
    text = document_text(result.docx_path)
    assert "☒ Exhibit A — Layout or drawings (layout.png)" in text and "owner-email" not in text
    assert [b["kind"] for b in json.loads(result.draft_path.read_text())["backup"]] == ["correspondence"]
    assert "Request on file" not in document_text(result.checklist_path)
    sent = json.dumps(client.requests)
    assert "Please add two outlets" in sent and "not really an image" not in sent    # drawings aren't sent


def test_a_requested_layout_is_an_unchecked_exhibit(store, package, tmp_path):
    wants_layout = proposal()
    wants_layout["exhibits_needed"] = ["layout_or_drawings"]
    result, _, _ = run(store, package, tmp_path, wants_layout)
    assert "Exhibit" not in document_text(result.docx_path)
    assert "Layout or drawings: supply one if the Owner should see it." in document_text(result.checklist_path)


def test_an_unknown_exhibit_is_rejected(store, package, tmp_path):
    wrong = proposal()
    wrong["exhibits_needed"] = ["site_photos"]
    _, client, _ = run(store, package, tmp_path, wrong, proposal())
    assert "'site_photos' isn't one of the exhibits that can be requested" in last_feedback(client)


def test_a_known_request_record_is_stated(store, package, tmp_path):
    recorded = proposal()
    recorded["request_record"] = known("email dated January 10, 2030")
    result, _, _ = run(store, package, tmp_path, recorded)
    assert "As the Owner asked (per email dated January 10, 2030), the Contractor proposes" in document_text(result.docx_path)


def test_cli_takes_correspondence_and_drawings(tmp_path, example_root):
    notes, email, drawing = tmp_path / "notes.txt", tmp_path / "email.txt", tmp_path / "plan.pdf"
    notes.write_text(NOTES)
    email.write_text("Owner email.")
    drawing.write_bytes(b"%PDF-1.4 not parsed")
    fake = FakeDraftingClient([tool_turn(proposal())])
    out = io.StringIO()
    code = cli.main(["--corpus", str(example_root), "--project", "P-001", "--notes", str(notes),
                     "--correspondence", str(email), "--drawing", str(drawing),
                     "--projects-dir", str(tmp_path / "projects")], client_factory=lambda s: fake, out=out, err=io.StringIO())
    assert code == 0
    kinds = [r.kind for r in FileProjectStore(tmp_path / "projects").open("P-001").inputs()]
    assert kinds == ["notes", "correspondence", "drawing"]


@needs_corpus
def test_case_1_checklist_has_one_item_per_fact(store):
    result = corpus_case(store, "01_added_lighting", "case1_proposal.json")
    lines = [l for l in document_text(result.checklist_path).split("\n") if l.startswith("☐")]
    normalized = [" ".join(l.lower().split()) for l in lines]
    assert len(set(normalized)) == len(normalized)
    assert sum("owner" in l and "who is" in l for l in normalized) == 1


@needs_corpus
def test_live_script_leaves_the_project_name_unknown(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import live_eval

    names = []

    def capture(package, store, project_id, project_name, *args, **kwargs):
        names.append(project_name)
        raise DraftingError("stopped by the test")

    monkeypatch.setattr(live_eval, "draft_request", capture)
    from types import SimpleNamespace

    from construction_os.drafting import Settings
    monkeypatch.setattr(live_eval, "real_client", lambda settings: SimpleNamespace(settings=Settings()))
    live_eval.main(["--corpus", str(CORPUS), "--cases", "1", "--keep", str(tmp_path / "store")])
    assert names == [None]


# The letter and Owner selections

def test_a_description_that_does_not_continue_the_request_is_rejected(store, package, tmp_path):
    wrong = proposal()
    wrong["description"] = known("Add two outlets to the garage.")
    _, client, _ = run(store, package, tmp_path, wrong, proposal())
    assert 'starting "the Contractor proposes"' in last_feedback(client)


def with_selection(base, status="unknown", ident=None, when=None):
    base["cost_lines"][0]["selection"] = {
        "items": "Outlets and cover plates", "status": status,
        "id": known(ident) if ident else unknown("Which Owner Selection covers the outlets?"),
        "date": known(when) if when else unknown("When was it approved?")}
    return base


def test_a_selection_id_or_date_not_in_the_inputs_is_rejected(store, package, tmp_path):
    invented = with_selection(proposal(), "approved", "SEL-99", "2030-01-08")
    _, client, _ = run(store, package, tmp_path, invented, proposal())
    feedback = last_feedback(client)
    assert "ID 'SEL-99' isn't in the inputs" in feedback and "date 2030-01-08 isn't in the inputs" in feedback


def test_a_selection_stated_in_the_notes_is_referenced(store, package, tmp_path):
    text = NOTES + " The Owner approved selection SEL-7 on January 8, 2030."
    result, _, _ = run(store, package, tmp_path, with_selection(proposal(), "approved", "SEL-7", "2030-01-08"), text=text)
    assert "Outlets and cover plates per Owner Selection SEL-7, approved January 8, 2030." in document_text(result.docx_path)
    sources = {s["id"]: s for s in json.loads(result.manifest_path.read_text())["sources"]}
    assert sources["PRO-34"]["applies"] is True


def test_a_supplied_selection_is_issued_with_the_pco_and_filed_never_attached(store, package, tmp_path):
    project = store.create("P-020", "Garage")
    notes = add_notes(project, tmp_path, NOTES + " Selection SEL-7 goes to the Owner with this PCO.")
    sheet = tmp_path / "SEL-7.xlsx"
    sheet.write_bytes(b"not read by the model")
    records = [notes, project.add_input("selection", sheet)]
    client = FakeDraftingClient([tool_turn(with_selection(proposal(), "pending", "SEL-7"))])
    result = draft_pco(project, package, records, client)
    draft = json.loads(result.draft_path.read_text())
    assert draft["cost_lines"][0]["selection"]["status"] == "issued_with_pco"
    assert [b["kind"] for b in draft["backup"]] == ["selection"] and draft["attachments"] == []
    text = document_text(result.docx_path)
    assert "per Owner Selection SEL-7, issued with this PCO for the Owner's signature." in text and "Exhibit" not in text
    assert "not read by the model" not in json.dumps(client.requests)


def test_cli_takes_a_selection(tmp_path, example_root):
    notes, sheet = tmp_path / "notes.txt", tmp_path / "sel.pdf"
    notes.write_text(NOTES)
    sheet.write_bytes(b"%PDF-1.4")
    code = cli.main(["--corpus", str(example_root), "--project", "P-001", "--notes", str(notes), "--selection", str(sheet),
                     "--projects-dir", str(tmp_path / "projects")],
                    client_factory=lambda s: FakeDraftingClient([tool_turn(proposal())]), out=io.StringIO(), err=io.StringIO())
    assert code == 0
    assert [r.kind for r in FileProjectStore(tmp_path / "projects").open("P-001").inputs()] == ["notes", "selection"]


@needs_corpus
def test_case_1_has_one_written_quote_item_per_quoted_line(store):
    result = corpus_case(store, "01_added_lighting", "case1_proposal.json")
    text = document_text(result.checklist_path)
    assert [l for l in text.split("\n") if "Written quote on file" in l] == [
        "☐  Written quote on file: Electrician's quote", "☐  Written quote on file: Drywall subcontractor's quote",
        "☐  Written quote on file: Painter's quote"]
    assert "Exhibit" not in document_text(result.docx_path)


@needs_corpus
def test_case_1_live_run_false_items_and_duplicates_are_gone(store):
    """Shaped like the Nov 9 live run: defaults scoped to some trades, and questions the checklist already asks."""
    result = corpus_case(store, "01_added_lighting", "case1_live_shape_proposal.json")
    assert not [f for f in result.flags if f.kind == "prompt_not_considered"]
    text = document_text(result.checklist_path)
    assert "supply the written quotes" not in text and "include the electrical permit and inspection" not in text
    assert "spending limit under APPROVAL-SPEND" not in text
    assert "protection of the living room floor" in text          # a real question stays


# Layout v2: section 4 groups and job-specific exclusions

def test_grouped_assumptions_and_exclusions_reach_the_draft(store, package, tmp_path):
    grouped = proposal()
    grouped["scope"]["assumptions"] = [{"kind": "access_logistics", "text": "Access to the garage during normal hours."},
                                       {"kind": "existing_conditions", "text": "The wall framing is sound."}]
    grouped["scope"]["not_included"] = ["Painting beyond the patched area.", "Work outside the garage."]
    result, _, _ = run(store, package, tmp_path, grouped)
    draft = json.loads(result.draft_path.read_text())
    assert draft["scope"]["not_included"] == ["Painting beyond the patched area.", "Work outside the garage."]
    text = document_text(result.docx_path)
    assert "Access to the garage during normal hours." in text and "Work outside the garage." in text
    assert "Concealed conditions found on opening are excluded." in text or "hidden conditions" in text


def test_an_assumption_with_an_unknown_group_is_rejected(store, package, tmp_path):
    wrong = proposal()
    wrong["scope"]["assumptions"] = [{"kind": "weather", "text": "Dry weather."}]
    _, client, _ = run(store, package, tmp_path, wrong, proposal())
    assert "scope/assumptions/0" in last_feedback(client)
