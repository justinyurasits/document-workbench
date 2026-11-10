"""Live evaluation of LLM drafting against the package's acceptance cases. Calls the real Claude API.

Never run in CI and not a test (pytest only collects tests/). Run it yourself:

    python scripts/live_eval.py --corpus ../construction-os-corpus [--model claude-sonnet-5-5] [--effort high]
                                [--cases 1,4] [--keep DIR]

Each draft case's request.md is drafted through the same pipeline as the CLI, in a throwaway project store
(or --keep DIR), and its priced totals and flags are compared with expected.yaml. A case with
pricing_structure (estimating from cost history) is graded on structure, not a total: every required scope item
has a line, every line has a source, the arithmetic is exact, estimates are on the PM checklist, and general
conditions follow the threshold rule. Advice cases are skipped until step 7. Contract terms aren't applied until step 6, so a case whose expected pricing comes from a
contract is graded against its company-standard total and must raise contract_terms_not_applied.
"""
import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import yaml

from construction_os.drafting import DraftingError
from construction_os.drafting.cli import draft_request, real_client
from construction_os.drafting.config import Settings
from construction_os.pricing.general_conditions import gc_applies
from construction_os.pricing.money import cents
from construction_os.projects import FileProjectStore
from construction_os.rendering import document_text, load_package


def money(value):
    return f"{Decimal(value):,.2f}" if value is not None else "—"


def compare(expected, result):
    """(checks, notes): each check is (name, expected, got, passed)."""
    exact = expected["exact"]
    pricing = exact["pricing"]
    priced = result.priced
    contract = "explains_difference" in exact
    got_lines = sorted(l.amount for l in priced.lines if l.included and l.amount is not None)
    want_lines = sorted(Decimal(l["amount"]) for l in pricing["lines"] + pricing["pass_through"])
    kinds = Counter(f.kind for f in result.flags)
    checks = [("line amounts", ", ".join(map(money, want_lines)), ", ".join(map(money, got_lines)), got_lines == want_lines),
              ("complete", True, priced.complete, priced.complete)]
    if contract:
        company = exact["explains_difference"]
        checks += [
            ("total (company standard)", money(company["company_pricing_total"]), money(priced.total),
             priced.complete and priced.total == Decimal(company["company_pricing_total"])),
            ("markup percent", company["company_markup_percent"], f"{sum(c.rate for c in priced.components):g}",
             sum(c.rate for c in priced.components) == Decimal(company["company_markup_percent"])),
            ("contract_terms_not_applied", "flagged first", f"{kinds['contract_terms_not_applied']} flag(s)",
             bool(result.flags) and result.flags[0].kind == "contract_terms_not_applied"),
        ]
    else:
        components = [(c.name, c.rate, c.amount) for c in priced.components]
        want = [(m["name"], Decimal(m["percent"]), Decimal(m["amount"])) for m in pricing["markup"]]
        checks += [
            ("subtotal", money(pricing["subtotal"]), money(priced.subtotal), priced.subtotal == Decimal(pricing["subtotal"])),
            ("markup components", "; ".join(f"{n} {r:g}% {money(a)}" for n, r, a in want),
             "; ".join(f"{n} {r:g}% {money(a)}" for n, r, a in components), components == want),
            ("total", money(pricing["total"]), money(priced.total),
             priced.complete and priced.total == Decimal(pricing["total"])),
            ("no contract flag", 0, kinds["contract_terms_not_applied"], kinds["contract_terms_not_applied"] == 0),
        ]
    return checks, kinds


def compare_structure(expected, result, package):
    """(checks, notes) for a case graded on structure, not a total."""
    want = expected["exact"]["pricing_structure"]
    priced = result.priced
    draft = json.loads(Path(result.draft_path).read_text())
    rows = [(line, draft["cost_lines"][line.index]) for line in priced.lines]
    kinds = Counter(f.kind for f in result.flags)
    checks = [("complete", True, priced.complete, priced.complete)]

    def division(row):
        value = row.get("division")
        return str(value.get("value") if isinstance(value, dict) else value or "").zfill(2)

    for item in want["required_scope"]:
        hits = [line.description for line, row in rows if line.included
                and (not item.get("division") or division(row) == item["division"])
                and (not item.get("category") or line.category == item["category"])
                and any(word in line.description.lower() for word in item["any_of"])]
        checks.append((f"scope: {item['item']}", "a line", "; ".join(hits) or "none", bool(hits)))

    allowed = set(want["every_line_sourced"])
    unsourced = [f"{line.description} ({line.evidence.source if line.evidence else 'none'})"
                 for line, _ in rows if line.included and (not line.evidence or line.evidence.source not in allowed)]
    checks.append(("every line sourced", ", ".join(sorted(allowed)), "; ".join(unsourced) or "all", not unsourced))

    wrong = [f"{line.description}: {line.quantity} × {line.unit_price} ≠ {line.amount}" for line, _ in rows
             if line.included and None not in (line.quantity, line.unit_price, line.amount)
             and cents(line.quantity * line.unit_price) != line.amount]
    markup = sum((c.amount for c in priced.components), Decimal(0))
    discount = priced.discount.amount if priced.discount else Decimal(0)
    sums = priced.total == priced.subtotal + markup - discount + priced.pass_through
    checks.append(("arithmetic exact", "every line and the total multiply and add",
                   "; ".join(wrong) or ("yes" if sums else "the total doesn't add up"), not wrong and sums))

    if want.get("estimated_quantities_on_checklist"):
        estimated = [m for m in draft.get("measurements") or [] if m.get("source") == "estimate"]
        text = document_text(result.checklist_path)
        missing = [m["label"] for m in estimated if m["label"] not in text]
        if estimated and "Confirm field dimensions before sending." not in text:
            missing.append("Confirm field dimensions before sending.")
        checks.append(("estimates on the checklist", "every estimated measurement", "; ".join(missing) or "all",
                       not missing))

    applies, _, _ = gc_applies(draft["schedule"], package.values)
    gc = [line for line, _ in rows if line.category == "general_conditions"]
    included = [line for line in gc if line.included]
    rule = not included if applies is not True else all(
        line.evidence and line.evidence.source == "rate_table" for line in included)
    checks.append(("general conditions rule", "included only past the threshold, role × duration at rate",
                   f"threshold {'passed' if applies else 'not passed' if applies is False else 'undecided'}; "
                   f"{len(included)} of {len(gc)} GC line(s) included", rule))
    return checks, kinds


def usage(attempts):
    totals = Counter()
    for attempt in attempts:
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            totals[key] += (attempt.get("usage") or {}).get(key) or 0
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--model", help="Claude model ID (default: DRAFT_MODEL or the built-in default)")
    parser.add_argument("--effort", help="effort level (default: DRAFT_EFFORT or high)")
    parser.add_argument("--cases", help="comma-separated case ids to run, e.g. 1,4 (default: all)")
    parser.add_argument("--keep", help="project store root to keep the drafts in (default: a temporary folder)")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus)
    package = load_package(corpus)
    root = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="pco-live-eval-"))
    store = FileProjectStore(root, forbidden=[package.root])
    client = real_client(Settings(model=args.model or "", effort=args.effort or ""))
    wanted = {int(c) for c in args.cases.split(",")} if args.cases else None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    print(f"Model {client.settings.model}, effort {client.settings.effort}; drafts in {root}\n")
    failures = 0
    for case in sorted(p for p in (corpus / "tests/acceptance/cases").iterdir() if p.is_dir()):
        expected = yaml.safe_load((case / "expected.yaml").read_text())
        if wanted and expected["id"] not in wanted:
            continue
        print(f"== Case {expected['id']}: {expected['title']}")
        if expected["mode"] == "advice":
            print("   skipped: advice (step 7)\n")
            continue
        started = time.monotonic()
        try:
            result = draft_request(package, store, f"eval-{case.name}-{stamp}", None,   # name unknown: [Project name]
                                   case / "request.md", [], client)
        except DraftingError as error:
            failures += 1
            print(f"   FAILED to draft after {time.monotonic() - started:.1f}s:\n   {error}\n")
            continue
        elapsed = time.monotonic() - started
        structure = "pricing_structure" in expected["exact"]
        checks, kinds = (compare_structure(expected, result, package) if structure else compare(expected, result))
        for name, want, got, passed in checks:
            print(f"   {'PASS' if passed else 'FAIL'}  {name}: expected {want}; got {got}")
        failures += sum(1 for *_, passed in checks if not passed)
        tokens = usage(result.attempts)
        served = sorted({a["model"] for a in result.attempts})
        print(f"   flags: {dict(kinds)}")
        print(f"   attempts {len(result.attempts)}, served by {', '.join(served)}, {elapsed:.1f}s; tokens in "
              f"{tokens['input_tokens']}, out {tokens['output_tokens']}, cache read "
              f"{tokens['cache_read_input_tokens']}, cache write {tokens['cache_creation_input_tokens']}")
        print(f"   {result.docx_path}")
        print(f"   {result.checklist_path}" + ("  (total not graded: " + money(result.priced.total) + ")"
                                             if structure else "") + "\n")
    print("All checks passed." if not failures else f"{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
