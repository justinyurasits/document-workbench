"""The one Claude call drafting makes, behind an interface so tests use recorded responses instead.

No agent framework: one tool, the proposal schema as its input schema. Claude Opus 5.5 rejects forced
tool_choice, so the call uses tool_choice auto and the instructions say to call the tool.
"""
from dataclasses import dataclass, field
from typing import Protocol

from .config import MAX_TOKENS, Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class ModelTurn:
    content: list                       # the assistant's content blocks as dicts, appended back unchanged on a retry
    stop_reason: str
    model: str                          # the model that served the turn (differs from the request after a fallback)
    request_id: str | None = None
    usage: dict = field(default_factory=dict)

    def tool_uses(self, name):
        return [block for block in self.content if block.get("type") == "tool_use" and block.get("name") == name]


class DraftingClient(Protocol):
    model: str

    def send(self, system: list, messages: list, tools: list) -> ModelTurn: ...


class AnthropicDraftingClient:
    """The real client. The API key comes from the environment (ANTHROPIC_API_KEY), read by the SDK."""

    def __init__(self, settings: Settings, sdk_client=None):
        import anthropic

        self.settings = settings
        self.model = settings.model
        self._client = sdk_client or anthropic.Anthropic()

    def send(self, system, messages, tools):
        params = dict(model=self.settings.model, max_tokens=MAX_TOKENS, system=system, messages=messages,
                      tools=[{**tool, "eager_input_streaming": True} for tool in tools],
                      tool_choice={"type": "auto"}, thinking={"type": "adaptive"},
                      output_config={"effort": self.settings.effort})
        if self.settings.fallbacks:
            params.update(betas=[FALLBACK_BETA], fallbacks="default")
        with self._client.beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
        return ModelTurn(content=[block.to_dict() for block in message.content], stop_reason=message.stop_reason,
                         model=message.model, request_id=getattr(message, "_request_id", None),
                         usage=message.usage.to_dict() if message.usage else {})


class FakeDraftingClient:
    """Returns recorded turns in order and keeps every request it was sent. For tests; never calls the API."""

    def __init__(self, turns, model="fake-model"):
        self.turns = list(turns)
        self.model = model
        self.requests = []

    def send(self, system, messages, tools):
        self.requests.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        if not self.turns:
            raise AssertionError("the fake client has no more recorded turns")
        return self.turns.pop(0)


def tool_turn(proposal, tool_name="propose_pco_draft", block_id="toolu_1", model="fake-model"):
    """A recorded turn in which the model calls the tool with this input."""
    return ModelTurn(content=[{"type": "tool_use", "id": block_id, "name": tool_name, "input": proposal}],
                     stop_reason="tool_use", model=model, usage={"input_tokens": 1, "output_tokens": 1})


def text_turn(text, model="fake-model"):
    return ModelTurn(content=[{"type": "text", "text": text}], stop_reason="end_turn", model=model)
