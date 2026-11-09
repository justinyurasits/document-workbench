"""The renderer against a fictional company. Every number here comes from that company's fixture."""
import json
import re

import pytest
from docx import Document

from construction_os.rendering import CHECKLIST_TITLE, document_text, load_package, render, render_checklist, unfilled_fields
from docx_helpers import parties, pricing_table, signature_blocks, summary_rows


@pytest.fixture
def package(example_root):
    return load_package(example_root)


@pytest.fixture
def draft(example_draft_text):
    return json.loads(example_draft_text)


def run(tmp_path, package, draft):
    return render(draft, package, tmp_path / "out.docx")


def table_with(path, label):
    for table in Document(path).tables:
        if table.rows[0].cells[0].text == label:
            return table
    raise AssertionError(f"no table starting {label}")


def grid(path):
    table = Document(path).tables[0]
    return {row.cells[i].text: row.cells[i + 1].text for row in table.rows for i in (0, 2)}


# Fields and totals

def test_every_placeholder_is_filled(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    assert unfilled_fields(result.path) == [] and "unfilled_field" not in result.kinds()


def test_fixture_template_has_fields(package):
    assert unfilled_fields(package.template)


def test_a_missing_field_stops_the_render(tmp_path, package, draft):
    del draft["header"]["project"]
    with pytest.raises(KeyError):
        run(tmp_path, package, draft)


def test_totals_come_from_the_company_settings(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    rows = summary_rows(result.path)
    assert rows["Subtotal"] == "$2,300.00" and rows["Company fee (2%)"] == "$46.00"
    assert rows["Overhead (8%)"] == "$184.00" and rows["Profit (5%)"] == "$115.00" and rows["Total"] == "$2,645.00"


def test_changing_a_setting_changes_the_total(tmp_path, package, draft):
    package.values["MARKUP-STRUCTURE"][0]["rate"] = 3
    assert summary_rows(run(tmp_path, package, draft).path)["Total"] == "$2,668.00"


def add_permit(draft):
    draft["cost_lines"].append({**draft["cost_lines"][2], "category": "permits_and_fees", "trade": "permit",
                                "description": "Sample permit", "unit_price": {"status": "known", "value": 200}, "basis": "actual_fee"})


def contract_terms(draft):
    draft["contract_terms"] = {"status": "known", "reference": "Sample clause 9", "pass_through": ["permits_and_fees"],
                               "markup": [{"name": "Overhead", "rate": 6, "applies_to": ["labor", "materials", "subcontractors"]},
                                          {"name": "Profit", "rate": 4, "applies_to": ["labor", "materials", "subcontractors"]}]}


def test_contract_terms_replace_company_markup(tmp_path, package, draft):
    add_permit(draft)
    contract_terms(draft)
    result = run(tmp_path, package, draft)
    rows = summary_rows(result.path)
    assert rows["Overhead (6%)"] == "$138.00" and rows["Profit (4%)"] == "$92.00" and rows["Total"] == "$2,730.00"
    assert "Company fee" not in document_text(result.path)
    assert rows["Permits and fees, passed through without markup"] == "$200.00"
    assert any("$2,845.00" in f.message for f in result.flags if f.kind == "contract_pricing")


def test_permits_marked_up_when_the_setting_says_so(tmp_path, package, draft):
    package.values["PERMIT-TREATMENT"] = "marked_up"
    add_permit(draft)
    rows = summary_rows(run(tmp_path, package, draft).path)
    assert rows["Subtotal"] == "$2,500.00" and "Permits and fees, passed through without markup" not in rows


# Markup presentation

def test_combined_shows_one_line(tmp_path, package, draft):
    package.values["MARKUP-PRESENTATION"] = "combined"
    draft["markup_presentation"]["value"] = "combined"
    rows = summary_rows(run(tmp_path, package, draft).path)
    assert rows["Markup"] == "$345.00" and "Overhead (8%)" not in rows and rows["Total"] == "$2,645.00"


def test_hidden_shows_only_the_total(tmp_path, package, draft):
    package.values["MARKUP-PRESENTATION"] = "hidden"
    draft["markup_presentation"]["value"] = "hidden"
    path = run(tmp_path, package, draft).path
    assert summary_rows(path) == {"Change total: add": "$2,645.00", "Total": "$2,645.00"}
    assert "$800.00" not in document_text(path)


def test_component_not_visible_rolls_into_markup(tmp_path, package, draft):
    package.values["MARKUP-STRUCTURE"][0]["client_visible"] = False
    rows = summary_rows(run(tmp_path, package, draft).path)
    assert rows["Markup"] == "$46.00" and "Company fee (2%)" not in rows and rows["Total"] == "$2,645.00"


def test_draft_that_differs_from_the_setting_uses_the_setting_and_flags(tmp_path, package, draft):
    draft["markup_presentation"]["value"] = "combined"
    result = run(tmp_path, package, draft)
    assert "Overhead (8%)" in summary_rows(result.path) and "markup_presentation" in result.kinds()


def test_contract_term_sets_the_presentation(tmp_path, package, draft):
    draft["markup_presentation"] = {"value": "combined", "source": "contract_term",
                                    "reference": {"status": "known", "value": "Sample clause 7"}}
    result = run(tmp_path, package, draft)
    assert "Markup" in summary_rows(result.path) and "markup_presentation" not in result.kinds()


def test_markup_sentence_cites_company_pricing_not_a_contract(tmp_path, package, draft):
    text = document_text(run(tmp_path, package, draft).path)
    assert "Markup per Example Builders' standard pricing: Company fee 2%, Overhead 8% and Profit 5%" in text
    assert "per the Contract" not in text and "per Contract" not in text


def test_markup_sentence_cites_a_supplied_contract(tmp_path, package, draft):
    contract_terms(draft)
    text = document_text(run(tmp_path, package, draft).path)
    assert "Markup per Sample clause 9: Overhead 6% and Profit 4%" in text and "standard pricing" not in text


# Signatures

def test_signatures_by_party_before_client(tmp_path, package, draft):
    assert parties(run(tmp_path, package, draft).path) == ["EXAMPLE BUILDERS", "OWNER", "PREPARED BY"]


def test_signatures_follow_the_setting_when_after_client(tmp_path, package, draft):
    package.values["AUTH-WRITTEN.cosign_timing"] = "after_client"
    result = run(tmp_path, package, draft)
    assert parties(result.path) == ["OWNER", "EXAMPLE BUILDERS", "PREPARED BY"]
    assert "signature_order" in result.kinds()


def test_company_block_has_the_executive_name_and_title(tmp_path, package, draft):
    assert signature_blocks(run(tmp_path, package, draft).path)[0] == ("EXAMPLE BUILDERS", "Alex Sample", "President")


def test_every_block_has_signature_name_title_and_date_lines(tmp_path, package, draft):
    table = Document(run(tmp_path, package, draft).path).tables[-1]
    labels = [row.cells[0].text for row in table.rows if row.cells[0]._tc is not row.cells[1]._tc]
    assert labels == ["Signature", "Printed name", "Title", "Date"] * 3


def test_no_architect_row_when_false(tmp_path, package, draft):
    assert "Architect" not in parties(run(tmp_path, package, draft).path)


def test_architect_row_when_true(tmp_path, package, draft):
    draft["architect_signs"] = {"value": True, "source": "contract_term", "reference": {"status": "known", "value": "Sample clause 3"}}
    result = run(tmp_path, package, draft)
    assert parties(result.path) == ["EXAMPLE BUILDERS", "OWNER", "ARCHITECT", "PREPARED BY"]
    assert any("architect" in item for item in result.needed)


def test_architect_row_marked_when_unknown(tmp_path, package, draft):
    draft["architect_signs"] = {"status": "unknown", "question": "Does the architect sign this PCO?"}
    result = run(tmp_path, package, draft)
    assert parties(result.path) == ["EXAMPLE BUILDERS", "OWNER", "ARCHITECT (IF REQUIRED BY CONTRACT)", "PREPARED BY"]
    assert "Architect: Does the architect sign this PCO?" in result.needed


# Bracketed blanks and the checklist

def test_unconfirmed_fields_are_bracketed_blanks(tmp_path, package, draft):
    fields = grid(run(tmp_path, package, draft).path)
    assert fields["Owner"] == "[Owner name]" and fields["Address"] == "[Project address]" and fields["Change event"] == "[CE-###]"


def test_confirmed_and_not_applicable_fields_show(tmp_path, package, draft):
    fields = grid(run(tmp_path, package, draft).path)
    assert fields["PCO number"] == "PCO-007" and fields["Date"] == "January 15, 2030" and fields["Contract"] == "Not applicable"


def test_blank_pco_number_shows_the_pattern(tmp_path, package, draft):
    draft["header"]["pco_number"] = {"status": "unknown", "question": "Which number?"}
    path = run(tmp_path, package, draft).path
    assert grid(path)["PCO number"] == "PCO-[###]" and "PCO-[###] | January 15, 2030" in document_text(path)


def test_an_unknown_quote_date_is_asked_in_the_checklist(tmp_path, package, draft):
    assert "Sample electrical, quote date: What is the quote date?" in run(tmp_path, package, draft).needed


def test_unknown_price_leaves_the_total_blank_and_flags(tmp_path, package, draft):
    # The quoted line: a labor line priced as hours × rate takes the company rate instead (tests/test_pricing.py).
    draft["cost_lines"][1]["unit_price"] = {"status": "unknown", "question": "What's the rate?"}
    result = run(tmp_path, package, draft)
    assert summary_rows(result.path)["Total"] == "[total]" and "incomplete_pricing" in result.kinds()


def test_questions_never_appear_in_the_client_document(tmp_path, package, draft):
    text = document_text(run(tmp_path, package, draft).path)
    assert "Who is the owner?" not in text and "NEEDED BEFORE" not in text.upper() and "to be confirmed" not in text.lower()


def checklist(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    return document_text(render_checklist(result, draft, package, tmp_path / "out_checklist.docx")), result


def test_checklist_holds_every_open_question_and_flag(tmp_path, package, draft):
    add_permit(draft)
    contract_terms(draft)
    text, result = checklist(tmp_path, package, draft)
    assert text.startswith(CHECKLIST_TITLE) and CHECKLIST_TITLE == "PM checklist — not for the owner"
    assert "Owner: Who is the owner?" in text and "A sample open question." in text
    assert "Electrical permit" in text and "Company pricing would give" in text
    for flag in result.flags:
        assert flag.message.split(": ", 1)[-1] in text


def test_checklist_is_a_separate_file(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    out = render_checklist(result, draft, package, tmp_path / "c.docx")
    assert out != result.path and out.exists() and result.path.exists()


def test_placeholder_flag_fires_and_does_not_block(tmp_path, package, draft):
    package.profile["company"]["licenses"]["contractor_registration"] = "REG (PLACEHOLDER)"
    result = run(tmp_path, package, draft)
    assert result.path.exists() and any("PLACEHOLDER" in f.message for f in result.flags if f.kind == "placeholder")
    assert "PLACEHOLDER" in checklist(tmp_path, package, draft)[0]


def test_no_placeholder_flag_when_the_profile_is_real(tmp_path, package, draft):
    assert "placeholder" not in run(tmp_path, package, draft).kinds()


# Other sections

def test_identity_comes_from_the_profile(tmp_path, package, draft):
    package.profile["company"].update(name="Other Builders", legal_name="Other Builders Inc.", address="2 Other St.")
    text = document_text(run(tmp_path, package, draft).path)
    assert "OTHER BUILDERS" in text and "Other Builders Inc." in text and "2 Other St." in text and "Example" not in text


def test_header_identity_comes_from_the_definition(tmp_path, package, draft):
    assert grid(run(tmp_path, package, draft).path)["Registration"] == "REG-EXAMPLE"


def test_clarifications_show_and_skip_not_applicable(tmp_path, package, draft):
    text = document_text(run(tmp_path, package, draft).path)
    assert "Sample concealed conditions" in text and "No permit for this trade" not in text


def test_every_prompt_for_every_trade_is_checked(tmp_path, package, draft):
    draft["clarifications_and_exclusions"] = draft["clarifications_and_exclusions"][1:]
    messages = [f.message for f in run(tmp_path, package, draft).flags if f.kind == "prompt_not_considered"]
    assert "Concealed conditions: not considered for electrical." in messages


def test_deleted_scope_none(tmp_path, package, draft):
    text = document_text(run(tmp_path, package, draft).path).split("\n")
    assert text[text.index("Deleted") + 1] == "None"


def test_permit_trade_raises_a_question(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    assert "permit_question" in result.kinds() and any("Electrical permit" in i for i in result.needed)


def test_no_permit_question_when_the_quote_includes_it(tmp_path, package, draft):
    draft["cost_lines"][1]["permit"] = "included_in_quote"
    assert "permit_question" not in run(tmp_path, package, draft).kinds()


def test_pricing_line_has_division_category_basis_and_letter(tmp_path, package, draft):
    row = [c.text for c in pricing_table(run(tmp_path, package, draft).path).rows[2].cells]
    assert row[:2] == ["26", "Trade partner"] and row[2] == "Sample electrical (per electrician's quote)" and row[3] == "A"


def test_line_pointing_to_a_missing_attachment_is_flagged(tmp_path, package, draft):
    draft["cost_lines"][1]["attachment"] = {"status": "known", "value": "Z"}
    assert "attachment_letter" in run(tmp_path, package, draft).kinds()


def test_attachments_are_lettered_exhibits(tmp_path, package, draft):
    assert "☐ Exhibit A — Sample quote" in document_text(run(tmp_path, package, draft).path)


def test_schedule_with_no_extension_is_one_row_and_a_sentence(tmp_path, package, draft):
    path = run(tmp_path, package, draft).path
    table = table_with(path, "Schedule impact")
    assert [[c.text for c in r.cells] for r in table.rows] == [["Schedule impact", "No change to the completion date"]]
    text = document_text(path)
    assert "No schedule impact, if authorized by [date]." in text and "Critical path" not in text


def test_schedule_sentence_with_a_date(tmp_path, package, draft):
    draft["schedule"]["authorization_by"] = {"status": "known", "value": "2030-02-01"}
    assert "No schedule impact, if authorized by February 1, 2030." in document_text(run(tmp_path, package, draft).path)


def test_schedule_with_added_days_keeps_every_row(tmp_path, package, draft):
    draft["schedule"]["extension_days"] = {"status": "known", "value": 5}
    draft["schedule"]["contract_duration_days"] = {"status": "known", "value": 200}
    path = run(tmp_path, package, draft).path
    rows = [r.cells[0].text for r in table_with(path, "Calendar days").rows]
    assert rows == ["Calendar days", "Critical path affected", "Completion date", "Condition"]
    assert "No schedule impact" not in document_text(path)


def test_schedule_with_added_days(tmp_path, package, draft):
    draft["schedule"]["extension_days"] = {"status": "known", "value": 3}
    assert table_with(run(tmp_path, package, draft).path, "Calendar days").rows[0].cells[1].text == "3 calendar days added"


def test_validity_uses_the_company_setting(tmp_path, package, draft):
    assert grid(run(tmp_path, package, draft).path)["Valid through"] == "January 29, 2030"


# Presentation (PCO polish), with the fictional company's own wording

def test_category_labels_and_basis_phrases_come_from_the_package(tmp_path, package, draft):
    draft["cost_lines"].append({**draft["cost_lines"][1], "trade": "drywall", "description": "Patch",
                                "attachment": {"status": "not_applicable", "reason": "None."}})
    draft["cost_lines"].append({**draft["cost_lines"][2], "category": "permits_and_fees", "basis": "actual_fee",
                                "description": "Permit", "trade": "permit"})
    rows = [[c.text for c in r.cells] for r in pricing_table(run(tmp_path, package, draft).path).rows[1:6]]
    assert [r[1] for r in rows] == ["Own crew", "Trade partner", "Materials", "Trade partner", "Permits and fees"]
    assert [r[2] for r in rows] == ["Sample carpentry (8 hours at the house rate)",
                                    "Sample electrical (per electrician's quote)", "Sample materials (estimate)",
                                    "Patch (per drywall quote)", "Permit (actual fee)"]


def test_a_negative_total_is_a_deduct_in_words_and_figures(tmp_path, package, draft):
    draft["cost_lines"][1]["unit_price"]["value"] = -3000
    path = run(tmp_path, package, draft).path
    rows = pricing_table(path).rows
    assert rows[-1].cells[0].text == "Change total: deduct" and rows[-1].cells[7].text == "$1,955.00"
    assert "for a deduction of one thousand nine hundred fifty-five dollars and 00/100 ($1,955.00)" in document_text(path)


def test_an_incomplete_total_leaves_words_and_figures_blank(tmp_path, package, draft):
    draft["cost_lines"][1]["unit_price"] = {"status": "unknown", "question": "What is the quote?"}
    path = run(tmp_path, package, draft).path
    assert summary_rows(path)["Total"] == "[total]" and "for [total in words] ([total])" in document_text(path)


def test_an_exhibit_is_checked_only_when_its_file_was_supplied(tmp_path, package, draft):
    draft["attachments"] = [{"letter": "A", "description": "Sample quote", "supplied": True, "kind": "quote"},
                            {"letter": "B", "description": "Second quote", "supplied": False, "kind": "quote"},
                            {"letter": "C", "description": "Owner request correspondence", "kind": "correspondence"},
                            {"letter": "D", "description": "Old-style entry"}]
    result = run(tmp_path, package, draft)
    lines = [l for l in document_text(result.path).split("\n") if "Exhibit" in l]
    assert lines == ["☒ Exhibit A — Sample quote", "☐ Exhibit B — Second quote",
                     "☐ Exhibit C — Owner request correspondence", "☐ Exhibit D — Old-style entry"]
    assert "Exhibit A (Sample quote): Attach the file." not in result.needed
    assert "Exhibit B (Second quote): Attach the file." in result.needed


def test_the_request_record_line(tmp_path, package, draft):
    assert "Requested by the Owner (per [email / meeting] dated [date])." in document_text(run(tmp_path, package, draft).path)
    draft["request_record"] = {"status": "known", "value": "email dated January 10, 2030"}
    assert "Requested by the Owner (per email dated January 10, 2030)." in document_text(run(tmp_path, package, draft).path)
    draft["header"]["cause_category"] = {"status": "known", "value": "Field Condition"}
    assert "Requested by the Owner" not in document_text(run(tmp_path, package, draft).path)


def test_running_header_carries_a_known_pco_number_and_the_date(tmp_path, package, draft):
    header = Document(run(tmp_path, package, draft).path).sections[0].header.paragraphs[0].text
    assert header.endswith("PCO-007 | January 15, 2030")


def test_a_known_owner_is_printed_in_the_owner_block(tmp_path, package, draft):
    draft["header"]["owner"] = {"status": "known", "value": "Pat Homeowner"}
    assert signature_blocks(run(tmp_path, package, draft).path)[1] == ("OWNER", "Pat Homeowner", "")


def test_the_pco_never_says_client(tmp_path, package, draft):
    draft["architect_signs"] = {"status": "unknown", "question": "Does the architect sign?"}
    assert not re.search(r"\bclient", document_text(run(tmp_path, package, draft).path), re.I)


# The checklist: two tiers from the definition, one item per fact

def checklist_lines(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    text = document_text(render_checklist(result, draft, package, tmp_path / "check.docx"))
    lines = text.split("\n")
    sending = lines[lines.index("Before sending") + 1:lines.index("Worth confirming")]
    confirming = [line for line in lines[lines.index("Worth confirming") + 1:] if line]
    return sending, confirming


def test_checklist_tiers_come_from_the_definition(tmp_path, package, draft):
    draft["header"]["project_address"] = {"status": "unknown", "question": "What is the project address?"}
    sending, confirming = checklist_lines(tmp_path, package, draft)
    assert "☐  Address: What is the project address?" in sending
    assert "☐  PCCO number: Which prime contract change order number will incorporate this PCO once it is executed?" in confirming
    package.definition["checklist"]["before_sending"] = []
    sending, confirming = checklist_lines(tmp_path, package, draft)
    assert sending == ["None"] and "☐  Address: What is the project address?" in confirming


def test_restated_and_repeated_questions_appear_once(tmp_path, package, draft):
    draft["open_questions"] = ["Who is the owner?", "What is the owner's name?", "What's the project address?",
                               "A sample open question.", "A sample open question!", "Is the outlet GFCI?"]
    sending, confirming = checklist_lines(tmp_path, package, draft)
    items = sending + confirming
    assert len({re.sub(r"[^a-z0-9 ]", "", i.lower()) for i in items}) == len(items)
    assert sum("owner" in i.lower() and "who is" in i.lower() for i in items) == 1
    assert "☐  A sample open question." in items and "☐  Is the outlet GFCI?" in items
    assert not any("owner's name" in i or "What's the project address?" in i for i in items)
