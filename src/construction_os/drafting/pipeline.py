"""Rough PM notes in, priced Word draft out (design section 7, steps 1 to 5).

Draft (Claude, one tool call) → check → retry once with the errors → assemble → validate against the draft
schema → price → render with the existing checks → write the draft version with its manifest. Flags never
block. A proposal that fails twice stops with a clear error and writes no draft: it is never repaired.
"""
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from construction_os.rendering import render, render_checklist
from construction_os.validate import DEFINITION

from .assemble import assemble_draft
from .conditions import evaluate
from .config import prompt_version
from .context import build_context
from .inputs import input_text
from .proposal import TOOL_NAME, check_proposal, draft_schema, proposal_schema, schema_errors, tool_definition

ATTEMPTS = 2


class DraftingError(Exception):
    def __init__(self, message, attempts=()):
        super().__init__(message)
        self.attempts = list(attempts)


@dataclass
class DraftResult:
    folder: object
    draft_path: object
    docx_path: object
    checklist_path: object
    manifest_path: object
    flags: list
    priced: object
    attempts: list = field(default_factory=list)


def user_message(project, records, texts, today):
    parts = [f"Project: {project.id}" + (f" ({project.name})" if project.name else ""), f"Today: {today.isoformat()}",
             "Draft a potential change order from these inputs. Cite amounts by input id. "
             f"Call {TOOL_NAME} once with the proposal."]
    for record in records:
        parts.append(f'<input id="{record.id}" kind="{record.kind}" name="{record.original_name}">\n'
                     f"{texts[record.id].strip()}\n</input>")
    return "\n\n".join(parts)


def review(turn, schema, texts, roles):
    """(proposal, tool_use_ids, errors) for one model turn."""
    if turn.stop_reason == "refusal":
        raise DraftingError("The model declined to draft this request.")
    uses = turn.tool_uses(TOOL_NAME)
    ids = [use["id"] for use in uses]
    if turn.stop_reason == "max_tokens":
        return None, ids, ["The output was cut off at the token limit. Submit a complete proposal."]
    if not uses:
        return None, ids, [f"No {TOOL_NAME} call. Call {TOOL_NAME} exactly once with the proposal."]
    if len(uses) > 1:
        return None, ids, [f"{TOOL_NAME} was called {len(uses)} times. Call it exactly once."]
    proposal = uses[0]["input"]
    return proposal, ids, check_proposal(proposal, schema, texts, roles)


def propose(client, context, schema, texts, message):
    system = [{"type": "text", "text": context.instructions},
              {"type": "text", "text": context.package_text, "cache_control": {"type": "ephemeral"}}]
    tools = [tool_definition(schema)]
    messages = [{"role": "user", "content": message}]
    attempts = []
    for number in range(1, ATTEMPTS + 1):
        turn = client.send(system, messages, tools)
        proposal, ids, errors = review(turn, schema, texts, context.roles)
        attempts.append({"attempt": number, "model": turn.model, "stop_reason": turn.stop_reason,
                         "request_id": turn.request_id, "usage": turn.usage, "errors": errors})
        if not errors:
            return proposal, attempts, turn
        if number == ATTEMPTS:
            details = "\n".join(f"  attempt {a['attempt']}:\n" + "\n".join(f"    - {e}" for e in a["errors"])
                                for a in attempts)
            raise DraftingError(f"The model's proposal failed validation {ATTEMPTS} times; no draft was written.\n"
                                f"{details}", attempts)
        feedback = "The proposal was rejected. Fix every error and call the tool again:\n" + \
                   "\n".join(f"- {error}" for error in errors)
        messages.append({"role": "assistant", "content": turn.content})
        if ids:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "is_error": True, "content": feedback}
                for tool_id in ids]})
        else:
            messages.append({"role": "user", "content": feedback})


def draft_pco(project, package, records, client, today=None):
    """Draft, price and render one PCO from a project's inputs. Writes drafts/v###/ in the project."""
    started = time.monotonic()
    today = today or date.today()
    context = build_context(package)
    schema = proposal_schema()
    texts = {record.id: input_text(project, record) for record in records}
    proposal, attempts, _ = propose(client, context, schema, texts, user_message(project, records, texts, today))
    draft, flags = assemble_draft(proposal, project, records, package, today)
    errors = schema_errors(draft, draft_schema())
    if errors:
        raise DraftingError("The assembled draft fails the draft schema (a bug in assembly, not the model's "
                            "output):\n" + "\n".join(f"  - {e}" for e in errors), attempts)
    folder = project.new_draft_folder()
    draft_path = folder / "draft.json"
    draft_path.write_text(json.dumps(draft, indent=2, ensure_ascii=False))
    (folder / "proposal.json").write_text(json.dumps(proposal, indent=2, ensure_ascii=False))
    result = render(draft, package, folder / "pco.docx")
    result.flags[:0] = flags
    checklist = render_checklist(result, draft, package, folder / "checklist.docx")
    manifest_path = folder / "manifest.json"
    manifest = build_manifest(project, package, context, schema, records, draft, result, client, attempts,
                              folder, time.monotonic() - started)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str))
    return DraftResult(folder, draft_path, result.path, checklist, manifest_path, result.flags, result.priced,
                       attempts)


def build_manifest(project, package, context, schema, records, draft, result, client, attempts, folder, elapsed):
    priced = result.priced
    settings = getattr(client, "settings", None)
    sources = []
    for source in context.sources:
        entry = asdict(source)
        entry["applies"] = evaluate(source.applies_when, draft, package.values) if source.applies_when else None
        sources.append(entry)
    parameters = {s.id: parameter_value(package, s.id) for s in context.sources if s.kind in ("parameter", "identity")}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": {"id": project.id, "name": project.name, "draft_version": folder.name},
        "package": {"root": str(package.root.resolve()), "commit": package.reference, "uncommitted_changes": dirty(package.root)},
        "definition": {"id": context.definition["id"], "path": DEFINITION, "sha256": context.definition_sha256,
                       "version": None, "note": "Definitions have no version field yet; the sha256 identifies it."},
        "model": {"requested": client.model, "served": sorted({a["model"] for a in attempts}),
                  "effort": settings.effort if settings else None,
                  "prompt_version": prompt_version(json.dumps(schema, sort_keys=True))},
        "attempts": attempts,
        "inputs": [asdict(record) for record in records],
        "sources": sources,
        "parameters": parameters,
        "pricing": {
            "lines": [{"index": l.index, "category": l.category, "description": l.description,
                       "quantity": l.quantity, "unit_price": l.unit_price, "amount": l.amount,
                       "included": l.included, "evidence": asdict(l.evidence)} for l in priced.lines],
            "subtotal": priced.subtotal,
            "components": [{"name": c.name, "rate": c.rate, "amount": c.amount} for c in priced.components],
            "pass_through": priced.pass_through,
            "total": priced.total if priced.complete else None,
            "complete": priced.complete,
        },
        "flags": [{"kind": f.kind, "message": f.message} for f in result.flags],
        "elapsed_seconds": round(elapsed, 2),
        "outputs": {"draft": "draft.json", "proposal": "proposal.json", "docx": result.path.name,
                    "checklist": "checklist.docx"},
    }


def parameter_value(package, key):
    if key.startswith("company."):
        return package.company(*key.split(".")[1:])
    value = package.values.get(key)
    return str(value) if isinstance(value, Decimal) else value


def dirty(root):
    out = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    return None if out.returncode else bool(out.stdout.strip())
