"""What the model is given from the company package. Loaded by ID from the definition, never searched.

The model sees the PCO definition, the rules and documents in its depends_on (each with its applies_when),
the values of the parameters that shape the wording, and the roles in the company's rate tables. It never
sees a rate, a markup or a discount: those are the pricing engine's.
"""
import hashlib
import re
from dataclasses import dataclass, field

import yaml

from construction_os.validate import DEFINITION, load_registry

from .config import INSTRUCTIONS

PRICE_PARAMETERS = {"LABOR-BILLING-RATES", "GC-WEEKLY-RATES", "MARKUP-STRUCTURE", "REPEAT-CLIENT-DISCOUNT",
                    "COST-HISTORY", "COST-RANGE-POINT", "WASTE-FACTORS"}   # the model sees roles and classes, never prices
ROLE_TABLES = {"labor": "LABOR-BILLING-RATES", "general_conditions": "GC-WEEKLY-RATES"}


@dataclass
class Source:
    id: str
    kind: str           # rule | document | parameter | identity | procedure | definition | missing
    path: str | None = None
    sha256: str | None = None
    applies_when: str | None = None


@dataclass
class DraftingContext:
    instructions: str
    package_text: str
    roles: dict                                   # category -> {role: unit}
    definition: dict
    definition_sha256: str
    sources: list = field(default_factory=list)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rule_sections(policy_text):
    """POL-01's rules by ID: each '### N. ID — Title' heading up to the next heading of that level or higher."""
    sections = {}
    matches = list(re.finditer(r"^### \d+\. ([A-Z][A-Z-]+) —.*$", policy_text, re.M))
    for match in matches:
        end = re.compile(r"^#{1,3} ", re.M).search(policy_text, match.end())
        sections[match.group(1)] = policy_text[match.start():end.start() if end else len(policy_text)].strip()
    return sections


def documents_by_id(root):
    docs = root / "docs"
    return {path.name.split("_", 1)[0]: path for path in sorted(docs.glob("*.md"))} if docs.is_dir() else {}


def roles(package):
    return {category: {row["role"]: row.get("unit") for row in package.values.get(key) or []}
            for category, key in ROLE_TABLES.items()}


def build_context(package):
    root = package.root
    definition_text = (root / DEFINITION).read_text(encoding="utf-8")
    definition = yaml.safe_load(definition_text)
    documents = documents_by_id(root)
    policy = next((path for doc_id, path in documents.items() if doc_id == "POL-01"), None)
    policy_text = policy.read_text(encoding="utf-8") if policy else ""
    rules = rule_sections(policy_text)
    registry = load_registry(root) if (root / "registry").is_dir() else {}
    sources = [Source(definition["id"], "definition", DEFINITION, digest(definition_text))]
    parts = [f"# PCO definition ({definition['id']})\n\n```yaml\n{definition_text.strip()}\n```"]

    procedure = definition.get("procedure")
    if procedure in documents:
        text = documents[procedure].read_text(encoding="utf-8")
        sources.append(Source(procedure, "procedure", rel(documents[procedure], root), digest(text)))
        parts.append(f"# Procedure {procedure}\n\n{text.strip()}")

    parameters = []
    for item in definition.get("depends_on") or []:
        dep, when = item["id"], item.get("applies_when")
        heading = f"applies when: {when}" + (f"; audience: {item['audience']}" if item.get("audience") else "")
        if dep in rules:
            sources.append(Source(dep, "rule", rel(policy, root), digest(rules[dep]), when))
            parts.append(f"# Rule {dep} (POL-01; {heading})\n\n{rules[dep]}")
        elif dep in documents:
            text = documents[dep].read_text(encoding="utf-8")
            sources.append(Source(dep, "document", rel(documents[dep], root), digest(text), when))
            parts.append(f"# Document {dep} ({heading})\n\n{text.strip()}")
        elif dep in package.values or dep in registry:
            sources.append(Source(dep, "parameter", applies_when=when))
            if shapes_wording(dep, registry) and dep in package.values:
                parameters.append(f"- {dep} ({heading}): {flow(package.values[dep])}")
        elif dep.startswith("company."):
            sources.append(Source(dep, "identity", applies_when=when))
        else:
            sources.append(Source(dep, "missing", applies_when=when))
    if parameters:
        parts.append("# Company settings that shape the draft\n\n" + "\n".join(parameters))

    table = roles(package)
    lines = ["# Roles in the company's rate tables (rates are applied by code and not shown)"]
    for category, entries in table.items():
        listed = "\n".join(f"- {role} (per {unit})" for role, unit in entries.items()) or "- (none)"
        lines.append(f"\n{category} roles:\n{listed}")
    classes = [row["class"] for row in package.values.get("WASTE-FACTORS") or []]
    if classes:
        lines.append("\nWaste classes for takeoffs (the percentages are applied by code): " + ", ".join(classes))
    parts.append("\n".join(lines))
    return DraftingContext(INSTRUCTIONS.read_text(encoding="utf-8"), "\n\n".join(parts), table, definition,
                           digest(definition_text), sources)


def shapes_wording(key, registry):
    """Settings the model may see: plain parameters, never prices, identities or the people in role mappings."""
    if key in PRICE_PARAMETERS or key.startswith(("company.", "role.")):
        return False
    entry = registry.get(key, (None, {}))[1]
    return not isinstance(entry, dict) or entry.get("kind", "parameter") == "parameter"


def flow(value):
    return yaml.safe_dump(value, default_flow_style=True, width=1000).strip().removesuffix("...").strip()


def rel(path, root):
    return str(path.relative_to(root)) if path else None
