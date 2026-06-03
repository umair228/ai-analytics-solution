"""Pluggable LLM providers for the DSE AI assistant.

A provider hides *where* generation runs behind one ``chat`` method:

  * :class:`AnthropicProvider` — the Claude API (cloud)
  * :class:`OpenAIProvider`    — an on-prem OpenAI-compatible server (Ollama / vLLM)

``get_provider()`` returns the one selected by ``settings.LLM_PROVIDER``.
``ai.client`` calls into this layer so swapping cloud <-> local is a settings
change, never a code change.
"""
from .base import AINotConfigured, BaseProvider, get_provider

__all__ = ["AINotConfigured", "BaseProvider", "get_provider"]
