"""Validate a customer package and its sample drafts.

  python -m construction_os.validate --corpus ../construction-os-corpus

Reports pass or fail per check and exits non-zero on any failure.
"""
import argparse
import json
import operator
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

REPO = Path(__file__).resolve().parents[2]
ENTRY_SCHEMA = REPO / "schemas" / "registry_entry.schema.json"
DRAFT_SCHEMA = REPO / "schemas" / "pco_draft.schema.json"
DEFINITION = "definitions/CHG-53-DEF.yaml"
CONFIG = "company/config.yaml"
DURATION = re.compile(r"^\d+ (hours?|days?|weeks?|months?)$")
RULE = re.compile(r"^(\w+) (<=|>=|<|>) (\w+)$")
COMPARE = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
PLACEHOLDER = "PLACEHOLDER"
SIGNATURE_ORDER = {
    "before_client": ["executive_cosign", "client", "contractor_pm"],
    "after_client": ["client", "executive_cosign", "contractor_pm"],
}
LEAF_TYPES = {"duration", "money", "percent", "count", "text", "role", "boolean"}
FIELD_CONSTRAINTS = {"min", "max"}
LIST_CONSTRAINTS = {"min_items", "unique_by", "items"}


@dataclass
class Result:
    name: str
    summary: str = ""
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def passed(self):
        return not self.errors


SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)   # the C loader when available: the cost history is large


def load_yaml(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=SAFE_LOADER)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_value(value, schema, where):
    """Return errors for a value against a registry schema."""
    if isinstance(schema, str):
        optional = schema.endswith("?")
        leaf = schema.rstrip("?")
        if leaf not in LEAF_TYPES:
            return [f"{where}: unknown type {leaf!r}"]
        if value is None:
            return [] if optional else [f"{where}: missing a value"]
        if leaf == "boolean":
            valid = isinstance(value, bool)
        elif leaf == "duration":
            valid = isinstance(value, str) and bool(DURATION.match(value))
        elif leaf == "money":
            valid = is_number(value) and value >= 0
        elif leaf == "percent":
            valid = is_number(value) and 0 <= value <= 100
        elif leaf == "count":
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else:
            valid = isinstance(value, str) and bool(value)
        return [] if valid else [f"{where}: {value!r} is not a {leaf}"]
    if not isinstance(schema, dict):
        return [f"{where}: schema must be a type name or a mapping"]
    if set(schema) == {"enum"}:
        return [] if value in schema["enum"] else [f"{where}: {value!r} is not one of {schema['enum']}"]
    if set(schema) == {"items"}:
        if not isinstance(value, list):
            return [f"{where}: expected a list"]
        errors = []
        for index, item in enumerate(value):
            errors += check_value(item, schema["items"], f"{where}[{index}]")
        return errors
    if not isinstance(value, dict):
        return [f"{where}: expected an object"]
    errors = [f"{where}: unexpected field {key!r}" for key in value if key not in schema]
    for key, subschema in schema.items():
        errors += check_value(value.get(key), subschema, f"{where}.{key}")
    return errors


def check_bounds(value, bounds, where):
    errors = []
    if is_number(value):
        if "min" in bounds and value < bounds["min"]:
            errors.append(f"{where}: {value} is below the minimum {bounds['min']}")
        if "max" in bounds and value > bounds["max"]:
            errors.append(f"{where}: {value} is above the maximum {bounds['max']}")
    return errors


def check_constraints(value, constraints, where):
    """Return errors for a value against a registry entry's constraints."""
    errors = check_bounds(value, constraints, where)
    if isinstance(value, list):
        if len(value) < constraints.get("min_items", 0):
            errors.append(f"{where}: needs at least {constraints['min_items']} item(s)")
        key = constraints.get("unique_by")
        if key:
            seen = [item.get(key) for item in value if isinstance(item, dict)]
            for duplicate in sorted({str(x) for x in seen if seen.count(x) > 1}):
                errors.append(f"{where}: duplicate {key} {duplicate!r}")
        for index, item in enumerate(value):
            for name, bounds in constraints.get("items", {}).items():
                if isinstance(item, dict):
                    errors += check_bounds(item.get(name), bounds, f"{where}[{index}].{name}")
    if isinstance(value, dict):
        for name, bounds in constraints.items():
            if name != "rule" and isinstance(bounds, dict):
                errors += check_bounds(value.get(name), bounds, f"{where}.{name}")
        rule = constraints.get("rule")
        if rule:
            left, symbol, right = RULE.match(rule).groups()
            a, b = value.get(left), value.get(right)
            if is_number(a) and is_number(b) and not COMPARE[symbol](a, b):
                errors.append(f"{where}: breaks the rule {rule}")
    return errors


def constraint_shape_errors(entry):
    """A constraint the validator can't apply must fail, not pass silently."""
    errors = []
    where = entry.get("id", "?")
    for key, item in entry["constraints"].items():
        if key == "rule":
            if not RULE.match(str(item)):
                errors.append(f"{where}: rule {item!r} isn't 'a < b'")
        elif key in FIELD_CONSTRAINTS | {"min_items", "unique_by"}:
            continue
        elif key == "items":
            if not isinstance(item, dict):
                errors.append(f"{where}: items constraints must be a mapping")
        elif not (isinstance(item, dict) and set(item) <= FIELD_CONSTRAINTS):
            errors.append(f"{where}: unknown constraint {key!r}")
    return errors


@dataclass
class ParseFailure:
    message: str


def load_registry(corpus):
    registry = {}
    for path in sorted((corpus / "registry").glob("*.yaml")):
        try:
            registry[path.stem] = (path, load_yaml(path))
        except yaml.YAMLError as error:
            registry[path.stem] = (path, ParseFailure(str(error).split("\n")[0]))
    return registry


def check_registry_entries(corpus):
    result = Result("registry_entries")
    registry = load_registry(corpus)
    if not registry:
        result.errors.append("registry/ has no entries")
    validator = jsonschema.Draft202012Validator(json.loads(ENTRY_SCHEMA.read_text()))
    for stem, (path, entry) in registry.items():
        if isinstance(entry, ParseFailure):
            result.errors.append(f"{path.name}: not valid YAML: {entry.message}")
            continue
        problems = sorted(validator.iter_errors(entry), key=lambda e: list(e.path))
        if problems:
            for problem in problems:
                location = ".".join(str(p) for p in problem.path) or "entry"
                result.errors.append(f"{path.name}: {location}: {problem.message}")
            continue
        if entry["id"] != stem:
            result.errors.append(f"{path.name}: id {entry['id']!r} doesn't match the file name")
        result.errors += constraint_shape_errors(entry)
        if entry["kind"] != "identity" and entry["default"] is not None:
            result.errors += check_value(entry["default"], entry["schema"], f"{stem} default")
    result.summary = f"{len(registry)} entries"
    return result


def dotted(data, path):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def null_leaves(value, where):
    if isinstance(value, dict):
        return [leaf for key, item in value.items() for leaf in null_leaves(item, f"{where}.{key}")]
    return [where] if value is None else []


def placeholder_warnings(value, where):
    """Generated documents can be checked for the same marker."""
    if isinstance(value, dict):
        return [w for key, item in value.items() for w in placeholder_warnings(item, f"{where}.{key}")]
    if isinstance(value, list):
        return [w for i, item in enumerate(value) for w in placeholder_warnings(item, f"{where}[{i}]")]
    if isinstance(value, str) and PLACEHOLDER in value:
        return [f"{where} is a placeholder: {value!r}"]
    return []


def markup_presentation_warnings(values):
    """client_visible and the presentation are separate settings that can disagree."""
    presentation = values.get("MARKUP-PRESENTATION")
    components = values.get("MARKUP-STRUCTURE")
    if not isinstance(components, list):
        return []
    visible = [c.get("client_visible") for c in components if isinstance(c, dict)]
    if presentation == "hidden" and any(visible):
        return ["MARKUP-PRESENTATION is hidden, but a markup component has client_visible true"]
    if presentation in ("itemized", "combined") and visible and not any(visible):
        return [f"MARKUP-PRESENTATION is {presentation}, but no markup component has client_visible true"]
    return []


def resolve(value, corpus, profile, where, errors):
    """Follow {from, key} and {from_profile} references, at any depth."""
    if isinstance(value, dict):
        if set(value) == {"from", "key"}:
            path = corpus / value["from"]
            if not path.exists():
                errors.append(f"{where}: {value['from']} doesn't exist")
                return None
            found = load_yaml(path)
            if not isinstance(found, dict) or value["key"] not in found:
                errors.append(f"{where}: {value['from']} has no {value['key']!r}")
                return None
            return found[value["key"]]
        if set(value) == {"from_profile"}:
            found = dotted(profile, value["from_profile"])
            if found is None:
                errors.append(f"{where}: the profile has no value for {value['from_profile']}")
            return found
        return {k: resolve(v, corpus, profile, f"{where}.{k}", errors) for k, v in value.items()}
    return value


def check_config_values(corpus):
    result = Result("config_values")
    path = corpus / CONFIG
    if not path.exists():
        result.errors.append(f"{CONFIG} doesn't exist")
        return result
    config = load_yaml(path) or {}
    profile = {}
    profile_ref = (config.get("identity") or {}).get("profile")
    if not profile_ref or not (corpus / profile_ref).exists():
        result.errors.append(f"identity.profile {profile_ref!r} doesn't exist")
    else:
        profile = load_yaml(corpus / profile_ref) or {}
    registry = load_registry(corpus)
    values = config.get("values") or {}
    for key in values:
        if key not in registry:
            result.errors.append(f"{key}: not in the registry")
    checked = 0
    resolved = {}
    for stem, (_, entry) in registry.items():
        if not isinstance(entry, dict) or "schema" not in entry:
            continue  # Reported by registry_entries.
        if entry.get("kind") == "identity":
            value = dotted(profile, stem)
            if value is None:
                result.errors.append(f"{stem}: not in the company profile")
                continue
            for leaf in null_leaves(value, stem):
                result.warnings.append(f"{leaf} is blank in the company profile")
            result.warnings += placeholder_warnings(value, stem)
        elif stem in values:
            value = resolve(values[stem], corpus, profile, stem, result.errors)
            if value is None and values[stem] is not None:
                continue
            resolved[stem] = value
            result.warnings += placeholder_warnings(value, stem)
        else:
            result.errors.append(f"{stem}: no value in {CONFIG}")
            continue
        checked += 1
        result.errors += check_value(value, entry["schema"], stem)
        result.errors += check_constraints(value, entry.get("constraints") or {}, stem)
    result.warnings += markup_presentation_warnings(resolved)
    result.summary = f"{checked} values"
    return result


def policy_rule_ids(corpus):
    files = list((corpus / "docs").glob("POL-01_*.md"))
    if not files:
        return set()
    return set(re.findall(r"^### \d+\. ([A-Z][A-Z-]+) —", files[0].read_text(encoding="utf-8"), re.M))


def document_ids(corpus):
    return {path.name.split("_", 1)[0] for path in (corpus / "docs").iterdir() if "_" in path.name}


def check_definition_dependencies(corpus):
    result = Result("definition_dependencies")
    path = corpus / DEFINITION
    if not path.exists():
        result.errors.append(f"{DEFINITION} doesn't exist")
        return result
    definition = load_yaml(path) or {}
    rules = policy_rule_ids(corpus)
    documents = document_ids(corpus)
    parameters = set(load_registry(corpus))
    dependencies = definition.get("depends_on") or []
    if not dependencies:
        result.errors.append("depends_on is empty")
    counts = {"rules": 0, "documents": 0, "parameters": 0}
    for item in dependencies:
        if not isinstance(item, dict) or not item.get("id") or not item.get("applies_when"):
            result.errors.append(f"{item!r}: needs id and applies_when")
            continue
        if item["id"] in rules:
            counts["rules"] += 1
        elif item["id"] in documents:
            counts["documents"] += 1
        elif item["id"] in parameters:
            counts["parameters"] += 1
        else:
            result.errors.append(f"{item['id']}: not a POL-01 rule, a document in docs/, or a registry parameter")
    procedure = definition.get("procedure")
    if procedure not in documents:
        result.errors.append(f"procedure {procedure!r}: not a document in docs/")
    else:
        counts["documents"] += 1
    template = definition.get("template")
    if template and not (corpus / template).exists():
        result.warnings.append(f"{template} doesn't exist yet (build step 3)")
    result.summary = f"{counts['rules']} rules, {counts['documents']} documents, {counts['parameters']} parameters"
    return result


def sample_paths(corpus):
    return sorted((Path(corpus) / "samples").glob("*_draft.json"))


def draft_schema_errors(path, schema=DRAFT_SCHEMA):
    validator = jsonschema.Draft202012Validator(json.loads(Path(schema).read_text()))
    draft = json.loads(Path(path).read_text())
    return [f"{Path(path).name}: {'.'.join(str(p) for p in problem.path) or 'draft'}: {problem.message}"
            for problem in sorted(validator.iter_errors(draft), key=lambda e: list(e.path))]


def check_sample_drafts(corpus):
    result = Result("sample_draft")
    paths = sample_paths(corpus)
    if not paths:
        result.errors.append("samples/ has no drafts")
    for path in paths:
        result.errors += draft_schema_errors(path)
    result.summary = f"{len(paths)} drafts"
    return result


def signature_fields(definition):
    for section in definition.get("sections") or []:
        if isinstance(section, dict) and section.get("id") == "signatures":
            return section.get("fields")
    return None


def check_draft_consistency(corpus):
    """The definition and the sample drafts against the company's settings."""
    result = Result("draft_consistency")
    values = (load_yaml(corpus / CONFIG) or {}).get("values") or {}
    timing = values.get("AUTH-WRITTEN.cosign_timing")
    presentation = values.get("MARKUP-PRESENTATION")
    expected = SIGNATURE_ORDER.get(timing)
    if expected is None:
        result.errors.append(f"AUTH-WRITTEN.cosign_timing: no signature order for {timing!r}")
    definition = load_yaml(corpus / DEFINITION) or {}
    if expected and signature_fields(definition) != expected:
        result.errors.append(f"{DEFINITION}: signatures {signature_fields(definition)} don't match {timing}: {expected}")
    for path in sample_paths(corpus):
        draft = json.loads(path.read_text())
        order = (draft.get("signatures") or {}).get("order")
        if expected and order != expected:
            result.errors.append(f"{path.name}: signature order {order} doesn't match {timing}: {expected}")
        markup = draft.get("markup_presentation") or {}
        if markup.get("source") != "contract_term" and markup.get("value") != presentation:
            result.errors.append(f"{path.name}: markup_presentation {markup.get('value')!r} doesn't match "
                                 f"MARKUP-PRESENTATION {presentation!r}, and no contract term is cited")
    result.summary = f"signatures {timing}, markup {presentation}"
    return result


def guarded(name, check, *args):
    """A file that can't be read fails its check; it doesn't stop the others."""
    try:
        return check(*args)
    except (yaml.YAMLError, json.JSONDecodeError, OSError) as error:
        return Result(name, "unreadable", [str(error).split("\n")[0]])


def run_checks(corpus):
    corpus = Path(corpus)
    return [guarded("registry_entries", check_registry_entries, corpus),
            guarded("config_values", check_config_values, corpus),
            guarded("definition_dependencies", check_definition_dependencies, corpus),
            guarded("sample_draft", check_sample_drafts, corpus),
            guarded("draft_consistency", check_draft_consistency, corpus)]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a customer package and its sample drafts.")
    parser.add_argument("--corpus", required=True, help="path to the customer package")
    args = parser.parse_args(argv)
    results = run_checks(args.corpus)
    for result in results:
        print(f"{'PASS' if result.passed else 'FAIL'}  {result.name}  ({result.summary})")
        for error in result.errors:
            print(f"      ERROR {error}")
        for warning in result.warnings:
            print(f"      WARN  {warning}")
    failed = sum(not r.passed for r in results)
    print(f"{len(results) - failed} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
