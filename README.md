# Document workbench

The Construction OS document workbench: the application that generates governed construction documents from a customer package, and reads only that package. The first customer package is `construction-os-corpus`, next to this repository.

The design docs are in [`construction-os-design`](https://github.com/justinyurasits/construction-os-design/tree/main/construction-os/document-workbench). The build spec is `../construction-os-design/construction-os/document-workbench/pco-design.md`.

Clone the three repositories side by side:

```
construction-os-corpus/
construction-os-design/
document-workbench/
```

The Python package is still named `construction_os`; renaming it is a separate change.

## Setup

```
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

## Validate a package

```
.venv/bin/python -m construction_os.validate --corpus ../construction-os-corpus
```

It reports pass or fail for five checks and exits non-zero on any failure:

- `registry_entries`: every registry file matches the entry schema.
- `config_values`: `company/config.yaml` values match their registry types and constraints.
- `definition_dependencies`: every `CHG-53-DEF` dependency resolves to a POL-01 rule, a document in `docs/`, or a registry parameter.
- `sample_draft`: every draft in the package's `samples/` validates against `schemas/pco_draft.schema.json`.
- `draft_consistency`: the definition's and each sample's signature order match `AUTH-WRITTEN.cosign_timing`, and each sample's markup presentation matches `MARKUP-PRESENTATION` unless it cites a contract term.

Any value containing `PLACEHOLDER` produces a warning, not a failure.

## Render a PCO

`construction_os.rendering.render(draft, package, out)` fills the company's Word template and returns the file and its flags. Flags never block the output. Unconfirmed fields show as bracketed blanks. `render_checklist(result, draft, package, out)` writes the PM's separate list of open questions and flags.

```
.venv/bin/python -m construction_os.rendering --corpus ../construction-os-corpus
```

This renders every draft in the package's `samples/` and its PM checklist to `samples/rendered/` (gitignored), with PDF previews if LibreOffice is installed.

## Draft a PCO from notes

```
.venv/bin/python -m construction_os.draft --corpus ../construction-os-corpus --project P-001 \
    --project-name "Smith kitchen" --notes notes.txt [--quote quote.pdf ...] \
    [--correspondence owner-email.txt ...] [--drawing layout.pdf ...]
```

Claude proposes the scope, quantities and cited amounts; code checks the proposal (one retry with the errors, then a clear error), prices it with the pricing engine, renders the PCO and the PM checklist, and writes a manifest. It prints the output paths, the total and the flags. A proposal that writes a price is rejected, not repaired. Quotes, correspondence and drawings become lettered exhibits, checked on the PCO only when the file was supplied; drawings are not sent to the model.

- **Key:** `ANTHROPIC_API_KEY`, from the environment or `.env` (loaded by the CLI).
- **Model:** `DRAFT_MODEL` or `--model` (default `claude-opus-5-5`); `DRAFT_EFFORT` or `--effort` (default `high`). Not secrets.
- **Project store:** `PROJECTS_DIR` or `--projects-dir` (default `~/construction-os/projects`). One folder per project: `project.yaml`, `inputs/` (as supplied, read-only) and `drafts/v001/` … (`draft.json`, `proposal.json`, `pco.docx`, `checklist.docx`, `manifest.json`). The store refuses a root inside the package, or inside a Git repository unless it's gitignored (`/projects/` is, here).

`scripts/live_eval.py` runs the package's acceptance cases through the real API and compares totals and flags with `expected.yaml`, with elapsed time and tokens. It is never run in CI.

## Test

```
.venv/bin/pytest
```

Unit tests use the fictional company in `tests/fixtures/`. Drafting tests use a fake Claude client with recorded output; no test builds the real client or loads `.env` (`tests/conftest.py` refuses both). Tests in `test_package.py` and `test_acceptance_cases.py` read the sample drafts and acceptance cases from the package (`CORPUS`, default `../construction-os-corpus`), and skip if it isn't there.
