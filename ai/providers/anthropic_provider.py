"""Claude (Anthropic) provider — the cloud default. Preserves the system-block
prompt-caching behaviour the codebase already relied on."""
from __future__ import annotations

from django.conf import settings

from .base import BaseProvider


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self):
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(getattr(settings, "CLAUDE_API_KEY", ""))

    @property
    def model_name(self) -> str:
        return getattr(settings, "CLAUDE_MODEL", "")

    def not_configured_message(self) -> str:
        return (
            "The Claude API key is not configured. Set CLAUDE_API_KEY in the "
            "backend .env, or switch to a local model with LLM_PROVIDER=local."
        )

    def _client_(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=settings.CLAUDE_API_KEY)
        return self._client

    def chat(self, *, system_blocks, messages, max_tokens, response_format=None) -> str:
        # Claude reliably follows a "return JSON" instruction, so response_format
        # needs no special handling here.
        response = self._client_().messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
        )
        return "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
