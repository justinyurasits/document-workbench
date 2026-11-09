from pathlib import Path

import pytest

EXAMPLE_DRAFT = Path(__file__).parent / "fixtures/example_package/samples/pco_example_draft.json"

ENTRY = """id: {id}
kind: {kind}
value_type: {vt}
schema: {schema}
default: {default}
constraints: {constraints}
stricter_is: lower
authority: {{document: POL-01, section: "1"}}
change_approver: Policy Owner
onboarding: {{group: test, found_in: [contracts], question: "Test?"}}
"""


def write_entry(corpus, id, kind="parameter", vt="object", schema="{notify_at: percent, stop_at: percent}",
                default="{notify_at: 70, stop_at: 90}",
                constraints="{notify_at: {min: 50, max: 95}, rule: notify_at < stop_at}"):
    (corpus / "registry" / f"{id}.yaml").write_text(
        ENTRY.format(id=id, kind=kind, vt=vt, schema=schema, default=default, constraints=constraints))


@pytest.fixture
def corpus(tmp_path) -> Path:
    """A small valid package."""
    for folder in ("registry", "docs", "definitions", "company/pricing"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "docs/POL-01_Core.md").write_text("### 1. AUTH-WRITTEN — Written\n\n### 12. APPROVAL-SPEND — Spend\n")
    (tmp_path / "docs/CHG-53_PCO.md").write_text("# PCO\n")
    (tmp_path / "docs/FCM-03_Change.md").write_text("# Change\n")
    write_entry(tmp_path, "NTE-CHECKPOINTS")
    write_entry(tmp_path, "CE-OPEN-WINDOW", vt="duration", schema="duration", default="48 hours", constraints="{}")
    write_entry(tmp_path, "RATES", vt="list", schema="{items: {role: text, rate: money}}", default="[]",
                constraints="{min_items: 1, unique_by: role}")
    write_entry(tmp_path, "company.name", kind="identity", vt="text", schema="text", default="null", constraints="{}")
    write_entry(tmp_path, "AUTH-WRITTEN.cosign_timing", vt="text", schema="{enum: [before_client, after_client]}",
                default="before_client", constraints="{}")
    write_entry(tmp_path, "MARKUP-PRESENTATION", vt="text", schema="{enum: [itemized, combined, hidden]}",
                default="itemized", constraints="{}")
    (tmp_path / "company/profile.yaml").write_text("company:\n  name: Test Builders\n")
    (tmp_path / "company/pricing/rates.yaml").write_text("rates:\n  - {role: Carpenter, rate: 90}\n")
    (tmp_path / "company/config.yaml").write_text(
        "identity: {profile: company/profile.yaml}\n"
        "values:\n"
        "  NTE-CHECKPOINTS: {notify_at: 70, stop_at: 90}\n"
        "  CE-OPEN-WINDOW: 48 hours\n"
        "  RATES: {from: company/pricing/rates.yaml, key: rates}\n"
        "  AUTH-WRITTEN.cosign_timing: before_client\n"
        "  MARKUP-PRESENTATION: itemized\n")
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples/pco_example_draft.json").write_text(EXAMPLE_DRAFT.read_text())
    (tmp_path / "definitions/CHG-53-DEF.yaml").write_text(
        "id: CHG-53-DEF\nprocedure: CHG-53\ndepends_on:\n"
        "  - {id: AUTH-WRITTEN, applies_when: always}\n"
        "  - {id: FCM-03, applies_when: always}\n"
        "  - {id: NTE-CHECKPOINTS, applies_when: nte_present}\n"
        "sections:\n  - {id: signatures, fields: [executive_cosign, client, contractor_pm]}\n")
    return tmp_path


@pytest.fixture
def example_root(tmp_path) -> Path:
    """The fictional company's package, with its template built."""
    import shutil

    from fixtures.build_template import build

    root = tmp_path / "example_package"
    shutil.copytree(Path(__file__).parent / "fixtures" / "example_package", root)
    build(root)
    return root


@pytest.fixture
def example_draft_text() -> str:
    return (Path(__file__).parent / "fixtures/example_package/samples/pco_example_draft.json").read_text()


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    """CI never calls the API: no test may build the real Claude client or load .env."""
    import anthropic
    import dotenv

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to construct the real Claude client")

    def no_env(*args, **kwargs):
        raise AssertionError("a test tried to load .env")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", refuse)
    monkeypatch.setattr(dotenv, "load_dotenv", no_env)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
