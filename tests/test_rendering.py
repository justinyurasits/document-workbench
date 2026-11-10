"""The renderer against a fictional company. Every number here comes from that company's fixture."""
import json
from decimal import Decimal
import re

import pytest
from docx import Document

from construction_os.rendering import CHECKLIST_TITLE, document_text, load_package, render, render_checklist, unfilled_fields
from docx_helpers import parties, pricing_table, signature_blocks, signature_table, summary_rows, summary_table


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
    assert "House markup: Company fee 2%, Overhead 8% and Profit 5%, on the direct-cost subtotal." in text
    assert "Example Builders" not in text.split("NO WORK")[0]           # the company name stays out of the letter
    assert "per the Contract" not in text and "per Contract" not in text


def test_markup_sentence_cites_a_supplied_contract(tmp_path, package, draft):
    contract_terms(draft)
    text = document_text(run(tmp_path, package, draft).path)
    assert "Markup per Sample clause 9: Overhead 6% and Profit 4%" in text and "House markup" not in text


# Signatures

def test_signatures_by_party_before_client(tmp_path, package, draft):
    assert parties(run(tmp_path, package, draft).path) == ["EXAMPLE BUILDERS", "OWNER"]   # Contractor on the left


def test_signatures_follow_the_setting_when_after_client(tmp_path, package, draft):
    package.values["AUTH-WRITTEN.cosign_timing"] = "after_client"
    result = run(tmp_path, package, draft)
    assert parties(result.path) == ["OWNER", "EXAMPLE BUILDERS"]
    assert "signature_order" in result.kinds()


def test_company_block_has_the_executive_name_and_title(tmp_path, package, draft):
    assert signature_blocks(run(tmp_path, package, draft).path)[0] == ("EXAMPLE BUILDERS", "Alex Sample", "President")


def test_the_contractor_and_owner_sign_side_by_side(tmp_path, package, draft):
    table = signature_table(run(tmp_path, package, draft).path)
    lines = [[row.cells[0].text, row.cells[3].text] for row in table.rows[1:]]
    assert lines == [["Signature", "Signature"], ["Printed name", "Printed name"], ["Title", "Title"], ["Date", "Date"]]


def test_the_preparer_is_in_the_header_not_a_signature_block(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    assert "PREPARED BY" not in parties(result.path)
    assert grid(result.path)["Prepared by"] == "[Project manager name], Project Manager"
    assert "Prepared by: Which project manager prepared this?" in result.needed
    package.values["role.ProjectManager"]["name"] = "Pat Planner"
    assert grid(run(tmp_path, package, draft).path)["Prepared by"] == "Pat Planner, Project Manager"


def test_requested_by_owner_only_for_owner_directed_changes(tmp_path, package, draft):
    assert grid(run(tmp_path, package, draft).path)["Requested by"] == "Owner"
    draft["header"]["cause_category"] = {"status": "known", "value": "Field Condition"}
    draft["description"] = {"status": "known", "value": "The Contractor proposes to repair a sample outlet."}
    assert "Requested by" not in grid(run(tmp_path, package, draft).path)


def test_no_architect_row_when_false(tmp_path, package, draft):
    assert "Architect" not in parties(run(tmp_path, package, draft).path)


def test_architect_row_when_true(tmp_path, package, draft):
    draft["architect_signs"] = {"value": True, "source": "contract_term", "reference": {"status": "known", "value": "Sample clause 3"}}
    result = run(tmp_path, package, draft)
    assert parties(result.path) == ["EXAMPLE BUILDERS", "OWNER", "ARCHITECT"]   # the architect below
    assert any("architect" in item for item in result.needed)


def test_architect_row_marked_when_unknown(tmp_path, package, draft):
    draft["architect_signs"] = {"status": "unknown", "question": "Does the architect sign this PCO?"}
    result = run(tmp_path, package, draft)
    assert parties(result.path) == ["EXAMPLE BUILDERS", "OWNER", "ARCHITECT (IF REQUIRED BY CONTRACT)"]
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


def test_deleted_scope_appears_only_when_there_is_some(tmp_path, package, draft):
    assert "Deleted scope" not in document_text(run(tmp_path, package, draft).path)        # known and empty
    draft["scope"]["deleted_scope"] = {"status": "unknown", "question": "Is anything removed?"}
    result = run(tmp_path, package, draft)
    assert "Deleted scope" not in document_text(result.path) and "Deleted scope: Is anything removed?" in result.needed
    draft["scope"]["deleted_scope"] = {"status": "known", "value": ["The original outlet."]}
    text = document_text(run(tmp_path, package, draft).path)
    assert "Deleted scope" in text and "The original outlet." in text


def test_permit_trade_raises_a_question(tmp_path, package, draft):
    result = run(tmp_path, package, draft)
    assert "permit_question" in result.kinds() and any("Electrical permit" in i for i in result.needed)


def test_no_permit_question_when_the_quote_includes_it(tmp_path, package, draft):
    draft["cost_lines"][1]["permit"] = "included_in_quote"
    assert "permit_question" not in run(tmp_path, package, draft).kinds()


def test_pricing_line_has_division_category_and_no_attachment_column(tmp_path, package, draft):
    table = pricing_table(run(tmp_path, package, draft).path)
    assert [c.text for c in table.rows[0].cells] == ["Div.", "Category", "Description", "Qty", "Unit", "Unit price", "Amount"]
    row = [c.text for c in table.rows[2].cells]
    assert row[:3] == ["26", "Trade partner", "Sample electrical"]     # a subcontract line gets no basis phrase


def test_schedule_with_no_extension_is_one_row_and_a_sentence(tmp_path, package, draft):
    path = run(tmp_path, package, draft).path
    table = table_with(path, "Schedule impact")
    assert [[c.text for c in r.cells] for r in table.rows] == [["Schedule impact", "No change to the completion date"]]
    text = document_text(path)
    assert "No schedule impact, if authorized by [date] so the trades stay in sequence." in text
    assert "Critical path" not in text


def test_schedule_sentence_with_a_date(tmp_path, package, draft):
    draft["schedule"]["authorization_by"] = {"status": "known", "value": "2030-02-01"}
    assert "No schedule impact, if authorized by February 1, 2030 so the trades stay in sequence." in \
        document_text(run(tmp_path, package, draft).path)


def test_schedule_with_added_days_keeps_every_row(tmp_path, package, draft):
    draft["schedule"]["extension_days"] = {"status": "known", "value": 5}
    draft["schedule"]["contract_duration_days"] = {"status": "known", "value": 200}
    path = run(tmp_path, package, draft).path
    rows = [r.cells[0].text for r in table_with(path, "Calendar days").rows]
    assert rows == ["Calendar days", "Critical path affected", "Completion date"]
    text = document_text(path)
    assert "Assumes approval by [date] so the trades stay in sequence." in text and "No schedule impact" not in text


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
    assert [r[2] for r in rows] == ["Sample carpentry (at the house hourly rate)", "Sample electrical",
                                    "Sample materials", "Patch", "Permit (at cost)"]   # an estimate gets no phrase


def test_a_negative_total_is_a_deduct_in_words_and_figures(tmp_path, package, draft):
    draft["cost_lines"][1]["unit_price"]["value"] = -3000
    path = run(tmp_path, package, draft).path
    rows = pricing_table(path).rows
    assert rows[-1].cells[0].text == "Change total: deduct" and rows[-1].cells[6].text == "$1,955.00"
    assert "for a deduction of one thousand nine hundred fifty-five dollars and 00/100 ($1,955.00)" in document_text(path)


def test_an_incomplete_total_leaves_words_and_figures_blank(tmp_path, package, draft):
    draft["cost_lines"][1]["unit_price"] = {"status": "unknown", "question": "What is the quote?"}
    path = run(tmp_path, package, draft).path
    assert summary_rows(path)["Total"] == "[total]" and "for [total in words] ([total])" in document_text(path)


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


# The PCO stands alone: backup is filed, never attached (CHG-53 2)

def section_titles(path):
    return [p.text for p in Document(path).paragraphs if p.text.isupper() or p.text.startswith("5.")]


def test_backup_and_unsupplied_items_are_never_exhibits(tmp_path, package, draft):
    draft["attachments"] = [
        {"letter": "A", "description": "Sample quote", "supplied": True, "kind": "quote"},
        {"letter": "B", "description": "Owner email", "supplied": True, "kind": "correspondence"},
        {"letter": "C", "description": "Layout or drawings", "supplied": False, "kind": "drawing"},
        {"letter": "D", "description": "Old-style entry"}]
    result = run(tmp_path, package, draft)
    text = document_text(result.path)
    assert "Exhibit" not in text and "5. ATTACHMENTS" not in text
    assert "Layout or drawings: supply one if the Owner should see it." in result.needed


def test_attachments_appear_only_for_a_supplied_drawing(tmp_path, package, draft):
    path = run(tmp_path, package, draft).path
    titles = section_titles(path)
    assert "5. ATTACHMENTS" not in titles and titles[titles.index("AUTHORIZATION AND SIGNATURES") - 1] != "5. ATTACHMENTS"
    draft["attachments"] = [{"letter": "A", "description": "Layout or drawings (plan.pdf)", "supplied": True, "kind": "drawing"}]
    text = document_text(run(tmp_path, package, draft).path)
    assert "5. ATTACHMENTS" in text and "☒ Exhibit A — Layout or drawings (plan.pdf)" in text


def test_one_written_quote_item_per_quoted_line(tmp_path, package, draft):
    second = dict(draft["cost_lines"][1], description="More electrical")
    second["evidence"] = dict(second["evidence"], source="pm_notes")
    draft["cost_lines"][1]["evidence"]["source"] = "pm_notes"
    draft["cost_lines"].append(second)
    items = [i for i in run(tmp_path, package, draft).needed if i.startswith("Written quote on file")]
    assert items == ["Written quote on file: Electrician's quote (Sample electrical)",
                     "Written quote on file: Electrician's quote (More electrical)"]


def test_a_supplied_quote_needs_no_written_quote_item(tmp_path, package, draft):
    draft["cost_lines"][1]["evidence"]["source"] = "supplied_quote"
    assert not any(i.startswith("Written quote on file") for i in run(tmp_path, package, draft).needed)


# The PCO is a letter from the Contractor to the Owner

def test_description_is_one_paragraph_opening_with_the_request(tmp_path, package, draft):
    paragraphs = [p.text for p in Document(run(tmp_path, package, draft).path).paragraphs]
    opening = "As the Owner asked (per [email / meeting] dated [date]), the Contractor proposes to add a sample outlet."
    assert paragraphs.count(opening) == 1
    assert not any(p.startswith("Reason") or p.startswith("As the Owner asked") and p != opening for p in paragraphs)


def test_a_known_request_is_woven_into_the_description(tmp_path, package, draft):
    draft["request_record"] = {"status": "known", "value": "email dated January 10, 2030"}
    assert ("As the Owner asked (per email dated January 10, 2030), the Contractor proposes to add a sample outlet."
            in document_text(run(tmp_path, package, draft).path))


def test_other_causes_print_the_description_as_written(tmp_path, package, draft):
    draft["header"]["cause_category"] = {"status": "known", "value": "Field Condition"}
    draft["description"] = {"status": "known", "value": "The Contractor proposes to repair a sample outlet."}
    text = document_text(run(tmp_path, package, draft).path)
    assert "The Contractor proposes to repair a sample outlet." in text and "As the Owner asked" not in text


def test_request_on_file_is_worth_confirming_unless_supplied(tmp_path, package, draft):
    assert "Request on file: how and when the Owner asked." in run(tmp_path, package, draft).needed
    draft["backup"].append({"kind": "correspondence", "description": "owner-email.txt"})
    assert "Request on file: how and when the Owner asked." not in run(tmp_path, package, draft).needed


def test_internal_notes_reach_the_checklist_never_the_pco(tmp_path, package, draft):
    draft["internal_notes"] = ["The carpentry is the PM's estimate of about eight hours."]
    result = run(tmp_path, package, draft)
    assert "Note for the PM: The carpentry is the PM's estimate of about eight hours." in result.needed
    assert "estimate of about eight hours" not in document_text(result.path)


INTERNAL_WORDS = re.compile(r"\b(PM|quotes?|subcontractors?|company carpenter|client)\b", re.I)


def owner_text(path):
    """The body of the Owner's PCO, without the header, footer and signature blocks."""
    document = Document(path)
    return "\n".join([p.text for p in document.paragraphs] +
                     [c.text for t in document.tables[:-1] for r in t.rows for c in r.cells])


def test_no_internal_words_on_the_owners_pco(tmp_path, package, draft):
    draft["architect_signs"] = {"status": "unknown", "question": "Does the architect sign?"}
    draft["internal_notes"] = ["The subcontractor's quote covers the outlet; the PM estimated the carpentry."]
    assert not INTERNAL_WORDS.search(document_text(run(tmp_path, package, draft).path).replace("Prepared by", ""))


def test_the_company_name_stays_out_of_the_letter(tmp_path, package, draft):
    """Only the cover, the header, the notice box, the signature blocks and the footer name the company."""
    document = Document(run(tmp_path, package, draft).path)
    paragraphs = [p.text for p in document.paragraphs]
    letter = [t for t in paragraphs[paragraphs.index("1. DESCRIPTION OF POTENTIAL CHANGE"):] if not t.startswith("NO WORK")]
    letter += [c.text for r in pricing_table(document).rows for c in r.cells]
    assert not any("Example Builders" in text or "EXAMPLE BUILDERS" in text for text in letter)
    assert any("the Contractor" in text for text in letter)


# Owner selections (PRO-34): referenced by ID, never attached

def select(draft, status, ident=None, when=None):
    draft["cost_lines"][1]["selection"] = {
        "items": "Fixtures and trims", "status": status,
        "id": {"status": "known", "value": ident} if ident else {"status": "unknown", "question": "Which selection?"},
        "date": {"status": "known", "value": when} if when else {"status": "unknown", "question": "When approved?"}}


@pytest.mark.parametrize("status,ident,when,line,item", [
    ("approved", "SEL-12", "2030-01-08", "Fixtures and trims per Owner Selection SEL-12, approved January 8, 2030.", None),
    ("approved_as_noted", "SEL-12", "2030-01-08",
     "Fixtures and trims per Owner Selection SEL-12, approved as noted, January 8, 2030.",
     "Owner selection: Confirm the notes on Owner Selection SEL-12 are reflected in this PCO."),
    ("issued_with_pco", "SEL-12", None,
     "Fixtures and trims per Owner Selection SEL-12, issued with this PCO for the Owner's signature.", None),
    ("pending", "SEL-12", None, "Fixtures and trims per Owner Selection SEL-12, [approved on (date) / issued with this PCO].",
     "Owner selection: Owner Selection SEL-12 is pending. Get it approved, or issue it with this PCO."),
    ("unknown", None, None, "Fixtures and trims per Owner Selection [ID], [approved on (date) / issued with this PCO].",
     "Owner selection: Which Owner Selection covers fixtures and trims, and is it approved?"),
])
def test_selections_are_referenced_in_the_scope(tmp_path, package, draft, status, ident, when, line, item):
    select(draft, status, ident, when)
    result = run(tmp_path, package, draft)
    text = document_text(result.path)
    assert line in text and "Exhibit" not in text
    selection_items = [i for i in result.needed if i.startswith("Owner selection")]
    assert selection_items == ([item] if item else [])


def test_an_unknown_selection_is_asked_before_sending(tmp_path, package, draft):
    select(draft, "unknown")
    sending, _ = checklist_lines(tmp_path, package, draft)
    assert "☐  Owner selection: Which Owner Selection covers fixtures and trims, and is it approved?" in sending


# Checklist fixes from the live run

def test_a_default_used_for_some_trades_counts_for_every_trade(tmp_path, package, draft):
    default = package.definition["clarification_prompts"][0]["default"]["text"]
    draft["clarifications_and_exclusions"] = [
        {"prompt": "concealed_conditions", "trades": ["electrical"], "disposition": "excluded", "text": default},
        {"prompt": "permits", "trades": ["all"], "disposition": "clarified", "text": "Permits are as stated."}]
    result = run(tmp_path, package, draft)
    assert not [f for f in result.flags if f.kind == "prompt_not_considered"]


def test_questions_the_checklist_already_asks_are_dropped(tmp_path, package, draft):
    draft["cost_lines"][1]["evidence"]["source"] = "pm_notes"
    draft["open_questions"] = ["Can you supply the written quotes?",
                               "Does the electrician's quote include the electrical permit?",
                               "Has the Executive approved this under APPROVAL-SPEND?",
                               "Is the outlet GFCI?"]
    sending, confirming = checklist_lines(tmp_path, package, draft)
    items = sending + confirming
    assert "☐  Is the outlet GFCI?" in items
    assert not any(q in i for i in items for q in ("supply the written quotes", "include the electrical permit",
                                                   "approved this under"))


# PCO layout v2

def texts(path):
    return [p.text for p in Document(path).paragraphs]


def test_first_page_is_branded_and_later_pages_carry_the_running_header(tmp_path, package, draft):
    document = Document(run(tmp_path, package, draft).path)
    section = document.sections[0]
    assert section.different_first_page_header_footer
    assert not section.first_page_header.paragraphs[0].text.strip()
    assert section.header.paragraphs[0].text.endswith("PCO-007 | January 15, 2030")
    cover = [p.text for p in document.paragraphs][:4]
    assert cover[1:] == ["EXAMPLE BUILDERS", "POTENTIAL CHANGE ORDER", "PCO-007 · January 15, 2030"]
    assert document.inline_shapes and package.tagline not in "\n".join(cover)


def test_the_work_includes_follows_the_description_and_scope_is_stated_once(tmp_path, package, draft):
    paragraphs = texts(run(tmp_path, package, draft).path)
    start = paragraphs.index("1. DESCRIPTION OF POTENTIAL CHANGE")
    assert paragraphs[start + 2] == "This includes:" and "Included" not in paragraphs and "Added scope" not in paragraphs


def test_basis_of_quantities_only_when_present(tmp_path, package, draft):
    assert "How we measured" not in texts(run(tmp_path, package, draft).path)
    draft["basis_of_quantities"] = ["One (1) outlet box, standard depth."]
    paragraphs = texts(run(tmp_path, package, draft).path)
    assert paragraphs[paragraphs.index("How we measured") + 1].endswith("One (1) outlet box, standard depth.")


def add_line(draft, **changes):
    line = json.loads(json.dumps(draft["cost_lines"][1]))
    line.update(changes)
    draft["cost_lines"].append(line)
    return line


def money(value):
    return f"${Decimal(value):,.2f}"


def test_by_division_subtotals_add_up_to_the_subtotal(tmp_path, package, draft):
    package.values["PRICING-PRESENTATION"] = "by_division"
    add_line(draft, description="Second electrical", unit_price={"status": "known", "value": 333.33})
    add_line(draft, description="Permit", category="permits_and_fees", basis="actual_fee", trade="permit",
             division={"status": "known", "value": "01"}, unit_price={"status": "known", "value": 125.5})
    add_line(draft, description="Unassigned item", division={"status": "unknown", "question": "Which division?"},
             unit_price={"status": "known", "value": 10.01})
    result = run(tmp_path, package, draft)
    rows = summary_rows(result.path)
    groups = {label: amount for label, amount in rows.items() if label.endswith("total") and label.startswith(("Div", "Unassigned"))}
    assert set(groups) == {"Div 06 total", "Div 26 total", "Unassigned total"}
    assert "Div 01 total" not in groups and "Division not yet assigned" in document_text(result.path)  # materials line, unknown div
    work = sum(Decimal(v.strip("$").replace(",", "")) for v in groups.values())
    assert money(work) == rows["Subtotal"] == money(result.priced.subtotal)
    assert rows["At cost total"] == money(result.priced.pass_through) == "$125.50"
    assert rows["Total"] == money(result.priced.total)


def test_by_division_shows_general_conditions_and_the_subtotal_before_markup(tmp_path, package, draft):
    package.values["PRICING-PRESENTATION"] = "by_division"
    draft["schedule"]["extension_days"] = {"status": "known", "value": 14}
    draft["schedule"]["contract_duration_days"] = {"status": "known", "value": 100}
    add_line(draft, category="general_conditions", basis="weeks_x_rate", role="Project Manager", trade="general",
             description="Project management", unit_price={"status": "not_applicable", "reason": "Rate table."},
             quantity={"status": "not_applicable", "reason": "From the extension."},
             evidence={"source": "rate_table", "reference": {"status": "known", "value": "Company rate"},
                       "date": {"status": "not_applicable", "reason": "Current."}})
    result = run(tmp_path, package, draft)
    rows = summary_rows(result.path)
    gc = sum(l.amount for l in result.priced.lines if l.category == "general_conditions")
    assert rows["Site overhead"] == money(gc) == "$4,200.00"
    assert rows["Before markup"] == money(result.priced.subtotal)
    assert Decimal(rows["Subtotal"].strip("$").replace(",", "")) + gc == result.priced.subtotal


def test_flat_is_one_table(tmp_path, package, draft):
    package.values["PRICING-PRESENTATION"] = "flat"
    path = run(tmp_path, package, draft).path
    assert summary_table(path) is None and "Div 26 · Electrical" not in document_text(path)
    assert summary_rows(path)["Total"] == "$2,645.00"


def test_summary_only_puts_the_lines_in_the_checklist(tmp_path, package, draft):
    package.values["PRICING-PRESENTATION"] = "summary_only"
    result = run(tmp_path, package, draft)
    text = document_text(result.path)
    assert "$1,000.00" not in text and summary_rows(result.path)["Total"] == "$2,645.00"
    with pytest.raises(AssertionError):
        pricing_table(result.path)
    checklist = document_text(render_checklist(result, draft, package, tmp_path / "check.docx"))
    assert "Line detail (PM only)" in checklist and "26 · Trade partner · Sample electrical: 1 LS × $1,000.00 = $1,000.00" in checklist


def test_an_unknown_amount_is_never_shown_as_zero(tmp_path, package, draft):
    package.values["PRICING-PRESENTATION"] = "by_division"
    draft["cost_lines"][1]["unit_price"] = {"status": "unknown", "question": "What is the quote?"}
    rows = summary_rows(run(tmp_path, package, draft).path)
    assert "$0.00" not in rows.values()
    assert rows["Div 26 total"] == rows["Subtotal"] == "[amount]" and rows["Total"] == "[total]"
    assert rows["Div 06 total"] == "$800.00"                    # a group whose lines are all known still adds up


@pytest.mark.parametrize("layout", ["by_division", "flat", "summary_only"])
def test_no_total_is_ever_rounded(tmp_path, package, draft, layout):
    package.values["PRICING-PRESENTATION"] = layout
    draft["cost_lines"][1]["unit_price"]["value"] = 46017.69            # brings the total to $54,415.34
    result = run(tmp_path, package, draft)
    assert result.priced.total == Decimal("54415.34")
    text = document_text(result.path)
    assert summary_rows(result.path)["Total"] == "$54,415.34"
    assert "fifty-four thousand four hundred fifteen dollars and 34/100 ($54,415.34)" in text
    assert not re.search(r"\$54,(4|5)00\.00", text) and "rounded" not in text.lower()


def test_activities_only_when_days_are_added(tmp_path, package, draft):
    draft["schedule"]["activities"] = [{"activity": "Rough-in", "duration_days": 1.5}, {"activity": "Trim", "duration_days": 1}]
    assert "Rough-in" not in document_text(run(tmp_path, package, draft).path)          # zero days: unchanged
    draft["schedule"]["extension_days"] = {"status": "known", "value": 3}
    draft["schedule"]["contract_duration_days"] = {"status": "known", "value": 200}
    table = table_with(run(tmp_path, package, draft).path, "Task")
    assert [[c.text for c in r.cells] for r in table.rows] == [["Task", "Days"], ["Rough-in", "1.5 days"], ["Trim", "1 day"]]


def test_section_4_groups_appear_only_with_items(tmp_path, package, draft):
    draft["scope"]["assumptions"] = [{"kind": "access_logistics", "text": "Access during normal hours."},
                                     "Plain strings are scope notes."]
    draft["scope"]["not_included"] = ["Painting beyond the work area."]
    paragraphs = texts(run(tmp_path, package, draft).path)
    start = paragraphs.index("4. ASSUMPTIONS AND CLARIFICATIONS")
    titles = [t for t in paragraphs[start:] if t in ("Scope notes", "Site conditions", "Access", "Excluded", "Standard terms")]
    assert titles == ["Scope notes", "Access", "Excluded", "Standard terms"]           # no site conditions given
    assert any(t.endswith("Painting beyond the work area.") for t in paragraphs)


def test_exclusions_never_replace_the_standard_clarifications(tmp_path, package, draft):
    draft["scope"]["not_included"] = ["Painting beyond the work area."]
    text = document_text(run(tmp_path, package, draft).path)
    for item in draft["clarifications_and_exclusions"]:
        if item["disposition"] != "not_applicable":
            assert item["text"] in text


def test_section_order_with_and_without_attachments(tmp_path, package, draft):
    order = ["1. DESCRIPTION OF POTENTIAL CHANGE", "2. PRICING", "3. SCHEDULE IMPACT", "4. ASSUMPTIONS AND CLARIFICATIONS",
             "AUTHORIZATION AND SIGNATURES"]
    paragraphs = texts(run(tmp_path, package, draft).path)
    assert [t for t in paragraphs if t in order + ["5. ATTACHMENTS"]] == order
    draft["attachments"] = [{"letter": "A", "description": "Layout (plan.pdf)", "supplied": True, "kind": "drawing"}]
    paragraphs = texts(run(tmp_path, package, draft).path)
    assert [t for t in paragraphs if t in order + ["5. ATTACHMENTS"]] == order[:4] + ["5. ATTACHMENTS", order[4]]
