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
