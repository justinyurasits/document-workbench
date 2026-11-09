"""python -m construction_os.draft --corpus PATH --project ID --notes FILE [--quote FILE ...]

Drafts one PCO into the project store and prints where the outputs are and the flags. The real Claude client
is built only here, from the environment (ANTHROPIC_API_KEY; .env is loaded if present). Tests pass a
client_factory instead and never load .env.
"""
import argparse
import sys

from construction_os.projects import FileProjectStore, ProjectError
from construction_os.rendering import load_package

from .config import Settings
from .inputs import InputError
from .pipeline import DraftingError, draft_pco


def parse(argv):
    parser = argparse.ArgumentParser(prog="python -m construction_os.draft", description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, help="the company package (for now, the corpus checkout)")
    parser.add_argument("--project", required=True, help="project ID; created if it doesn't exist")
    parser.add_argument("--project-name", help="project name, used when the project is created")
    parser.add_argument("--notes", required=True, help="the PM's notes, as a text file")
    parser.add_argument("--quote", action="append", default=[], help="a supplied quote (.txt or .pdf); repeatable")
    parser.add_argument("--correspondence", action="append", default=[],
                        help="the Owner's request (email or meeting notes, .txt or .pdf); repeatable")
    parser.add_argument("--drawing", action="append", default=[], help="a layout or drawing (any file); repeatable")
    parser.add_argument("--projects-dir", help="project store root (default: PROJECTS_DIR or ~/construction-os/projects)")
    parser.add_argument("--model", help="Claude model ID (default: DRAFT_MODEL or the built-in default)")
    parser.add_argument("--effort", help="effort level (default: DRAFT_EFFORT or high)")
    return parser.parse_args(argv)


def real_client(settings):
    from dotenv import load_dotenv

    from .client import AnthropicDraftingClient

    load_dotenv()
    return AnthropicDraftingClient(Settings.from_env(settings.model, settings.effort))


def draft_request(package, store, project_id, project_name, notes, quotes, client, correspondence=(), drawings=()):
    project = store.open(project_id) if store.exists(project_id) else store.create(project_id, project_name)
    records = [project.add_input("notes", notes)] + [project.add_input("quote", quote) for quote in quotes]
    records += [project.add_input("correspondence", path) for path in correspondence]
    records += [project.add_input("drawing", path) for path in drawings]
    return draft_pco(project, package, records, client)


def main(argv=None, client_factory=None, out=sys.stdout, err=sys.stderr):
    args = parse(argv)
    settings = Settings(model=args.model or "", effort=args.effort or "")
    factory = client_factory or real_client
    try:
        package = load_package(args.corpus)
        store = FileProjectStore(args.projects_dir, forbidden=[package.root])
        client = factory(settings)
        result = draft_request(package, store, args.project, args.project_name, args.notes, args.quote, client,
                               args.correspondence, args.drawing)
    except (DraftingError, ProjectError, InputError, OSError, ValueError) as error:
        print(f"Drafting failed: {error}", file=err)
        return 2
    print(f"Draft {result.folder.name} for project {args.project}:", file=out)
    for label, path in (("PCO", result.docx_path), ("PM checklist", result.checklist_path),
                        ("Structured draft", result.draft_path), ("Manifest", result.manifest_path)):
        print(f"  {label}: {path}", file=out)
    total = f"${result.priced.total:,.2f}" if result.priced.complete else "[incomplete]"
    print(f"Total: {total}", file=out)
    print(f"Flags ({len(result.flags)}):", file=out)
    for flag in result.flags:
        print(f"  - [{flag.kind}] {flag.message}", file=out)
    return 0
