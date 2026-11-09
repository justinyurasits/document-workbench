"""Live evaluation of LLM drafting against the package's acceptance cases. Calls the real Claude API.

Never run in CI and not a test (pytest only collects tests/). Run it yourself:

    python scripts/live_eval.py --corpus ../construction-os-corpus [--model claude-sonnet-5-5] [--effort high]
                                [--cases 1,4] [--keep DIR]

Each draft case's request.md is drafted through the same pipeline as the CLI, in a throwaway project store
(or --keep DIR), and its priced totals and flags are compared with expected.yaml. Advice cases are skipped
until step 7. Contract terms aren't applied until step 6, so a case whose expected pricing comes from a
contract is graded against its company-standard total and must raise contract_terms_not_applied.
"""
import argparse
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
from construction_os.projects import FileProjectStore
from construction_os.rendering import load_package


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
        checks, kinds = compare(expected, result)
        for name, want, got, passed in checks:
            print(f"   {'PASS' if passed else 'FAIL'}  {name}: expected {want}; got {got}")
        failures += sum(1 for *_, passed in checks if not passed)
        tokens = usage(result.attempts)
        served = sorted({a["model"] for a in result.attempts})
        print(f"   flags: {dict(kinds)}")
        print(f"   attempts {len(result.attempts)}, served by {', '.join(served)}, {elapsed:.1f}s; tokens in "
              f"{tokens['input_tokens']}, out {tokens['output_tokens']}, cache read "
              f"{tokens['cache_read_input_tokens']}, cache write {tokens['cache_creation_input_tokens']}")
        print(f"   {result.docx_path}\n")
    print("All checks passed." if not failures else f"{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
