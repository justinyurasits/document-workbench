"""Pricing flags. They never block pricing or rendering; they go to the PM's checklist."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Flag:
    code: str
    message: str
    line: int | None = None   # index into the draft's cost_lines, where the flag is about one line
