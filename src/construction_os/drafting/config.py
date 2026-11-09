"""Drafting settings. The model ID is configuration, not a secret: set DRAFT_MODEL to change it."""
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
MAX_TOKENS = 64000
INSTRUCTIONS = Path(__file__).with_name("instructions.md")
PROMPT_SERIES = "pco-draft-1"


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    fallbacks: bool = True      # server-side refusal fallback (fallbacks="default")

    @classmethod
    def from_env(cls, model=None, effort=None):
        return cls(model=model or os.environ.get("DRAFT_MODEL") or DEFAULT_MODEL,
                   effort=effort or os.environ.get("DRAFT_EFFORT") or DEFAULT_EFFORT)


def prompt_version(schema_text):
    """Changes whenever the generic instructions or the proposal schema change."""
    digest = hashlib.sha256(INSTRUCTIONS.read_bytes() + schema_text.encode()).hexdigest()[:10]
    return f"{PROMPT_SERIES}+{digest}"
