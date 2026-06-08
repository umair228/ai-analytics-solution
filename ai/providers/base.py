"""Provider interface + factory."""
from __future__ import annotations

from django.conf import settings


class AINotConfigured(Exception):
    """Raised when the active LLM provider is not configured/reachable."""


class BaseProvider:
    """Common interface every provider implements.

    ``chat`` takes the SAME inputs ``ai.client`` already builds:
      * ``system_blocks`` — a list of ``{"type":"text","text":..., "cache_control"?}``
        blocks (Anthropic's shape). Local providers flatten these to a single
        system message and ignore ``cache_control`` (the local server does prefix
        caching itself).
      * ``messages`` — ``[{"role","content"}, ...]`` (OpenAI/Anthropic compatible).
      * ``response_format`` — ``None`` or ``"json"`` (request a JSON object).
    Returns the assistant text (str).

    ``complete`` is the agentic variant: it offers the model a set of *tools* and
    returns whatever the model decided to do for ONE turn — either final text, or
    a batch of tool calls to run (the :mod:`ai.agent` loop runs them and calls
    back in). It uses a provider-neutral message + tool vocabulary so the agent
    loop never sees Anthropic-vs-OpenAI differences:

      * a *tool schema* is ``{"name", "description", "input_schema"}`` where
        ``input_schema`` is a JSON Schema object (Anthropic's native shape; the
        OpenAI provider repackages it under ``function.parameters``).
      * a neutral *message* is one of::

            {"role": "user",      "content": <str>}
            {"role": "assistant", "content": <str>, "tool_calls": [<call>, ...]}
            {"role": "tool",      "tool_call_id": <str>, "name": <str>, "content": <str>}

      * a neutral *tool call* is ``{"id", "name", "input": <dict>}``.

    ``complete`` returns ``{"text": <str>, "tool_calls": [<call>, ...]}`` — a
    non-empty ``tool_calls`` means the model wants those run before answering.
    """

    name = "base"

    @property
    def configured(self) -> bool:
        return False

    @property
    def model_name(self) -> str:
        return ""

    def not_configured_message(self) -> str:
        return "The AI assistant is not configured."

    def chat(self, *, system_blocks, messages, max_tokens, response_format=None) -> str:
        raise NotImplementedError

    def complete(self, *, system_blocks, messages, tools, max_tokens) -> dict:
        """One agentic turn. ``tools`` is a list of neutral tool schemas; pass an
        empty list to force a plain text answer. See the class docstring for the
        message/tool/return shapes. Returns ``{"text", "tool_calls"}``."""
        raise NotImplementedError


# Cache one instance per provider name. Settings are static at runtime, and each
# instance lazily builds its underlying SDK client on first use.
_CACHE: dict[str, BaseProvider] = {}


def get_provider() -> BaseProvider:
    name = (getattr(settings, "LLM_PROVIDER", "anthropic") or "anthropic").lower()
    if name not in _CACHE:
        if name == "local":
            from .openai_provider import OpenAIProvider
            _CACHE[name] = OpenAIProvider()
        else:
            from .anthropic_provider import AnthropicProvider
            _CACHE[name] = AnthropicProvider()
    return _CACHE[name]
