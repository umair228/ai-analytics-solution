"""Local provider — any OpenAI-compatible server (Ollama today, vLLM on the GPU
box later). Air-gapped: it only talks to ``settings.LLM_BASE_URL``."""
from __future__ import annotations

from django.conf import settings

from .base import BaseProvider


class OpenAIProvider(BaseProvider):
    name = "local"

    def __init__(self):
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(getattr(settings, "LLM_BASE_URL", ""))

    @property
    def model_name(self) -> str:
        return getattr(settings, "LLM_MODEL", "")

    def not_configured_message(self) -> str:
        return (
            "No local LLM endpoint configured. Set LLM_BASE_URL (and LLM_MODEL) "
            "to your on-prem OpenAI-compatible server (Ollama or vLLM)."
        )

    def _client_(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ModuleNotFoundError as exc:  # pragma: no cover
                raise RuntimeError(
                    "The 'openai' package is required for LLM_PROVIDER=local. "
                    "Install it: pip install openai"
                ) from exc
            self._client = OpenAI(
                base_url=settings.LLM_BASE_URL,
                api_key=getattr(settings, "LLM_API_KEY", "") or "EMPTY",
                timeout=getattr(settings, "LLM_TIMEOUT", 120),
            )
        return self._client

    @staticmethod
    def _to_openai_messages(system_blocks, messages):
        """Flatten Anthropic-style system blocks into a single system message
        (dropping cache_control — the local server handles prefix caching)."""
        sys_text = "\n\n".join(
            b.get("text", "") for b in (system_blocks or []) if b.get("text")
        )
        out = []
        if sys_text:
            out.append({"role": "system", "content": sys_text})
        out.extend(messages or [])
        return out

    def chat(self, *, system_blocks, messages, max_tokens, response_format=None) -> str:
        kwargs = {
            "model": settings.LLM_MODEL,
            "max_tokens": max_tokens,
            "messages": self._to_openai_messages(system_blocks, messages),
        }
        if response_format == "json":
            # JSON mode — supported by both Ollama and vLLM's OpenAI server.
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client_().chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()
