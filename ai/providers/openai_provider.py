"""Local provider — any OpenAI-compatible server (Ollama today, vLLM on the GPU
box later). Air-gapped: it only talks to ``settings.LLM_BASE_URL``."""
from __future__ import annotations

import json

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

    @staticmethod
    def _tuning_kwargs():
        """Temperature + (Ollama) context-window options shared by chat/complete."""
        kwargs = {"temperature": getattr(settings, "LLM_TEMPERATURE", 0.2)}
        num_ctx = getattr(settings, "LLM_NUM_CTX", 0)
        if num_ctx:
            # Ollama reads `options` from the OpenAI endpoint via extra_body; vLLM
            # ignores it harmlessly (context is fixed at serve time).
            kwargs["extra_body"] = {"options": {"num_ctx": num_ctx}}
        return kwargs

    def chat(self, *, system_blocks, messages, max_tokens, response_format=None) -> str:
        kwargs = {
            "model": settings.LLM_MODEL,
            "max_tokens": max_tokens,
            "messages": self._to_openai_messages(system_blocks, messages),
            **self._tuning_kwargs(),
        }
        if response_format == "json":
            # JSON mode — supported by both Ollama and vLLM's OpenAI server.
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client_().chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()

    # ── agentic (tool-using) turn ──────────────────────────────────────────
    @classmethod
    def _to_tool_messages(cls, system_blocks, messages):
        """Translate neutral messages into OpenAI chat messages, mapping the
        agent's tool calls/results onto OpenAI's ``tool_calls`` / ``role:tool``."""
        sys_text = "\n\n".join(
            b.get("text", "") for b in (system_blocks or []) if b.get("text")
        )
        out = []
        if sys_text:
            out.append({"role": "system", "content": sys_text})
        for m in messages or []:
            role = m.get("role")
            if role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id"),
                    "content": m.get("content") or "",
                })
            elif role == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or None}
                calls = m.get("tool_calls") or []
                if calls:
                    msg["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c.get("input") or {}),
                            },
                        }
                        for c in calls
                    ]
                out.append(msg)
            else:  # user
                out.append({"role": "user", "content": m.get("content") or ""})
        return out

    def complete(self, *, system_blocks, messages, tools, max_tokens) -> dict:
        kwargs = {
            "model": settings.LLM_MODEL,
            "max_tokens": max_tokens,
            "messages": self._to_tool_messages(system_blocks, messages),
            **self._tuning_kwargs(),
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
            kwargs["tool_choice"] = "auto"
        response = self._client_().chat.completions.create(**kwargs)

        message = response.choices[0].message
        tool_calls = []
        for call in (getattr(message, "tool_calls", None) or []):
            raw_args = call.function.arguments or "{}"
            try:
                parsed = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            tool_calls.append({
                "id": call.id,
                "name": call.function.name,
                "input": parsed if isinstance(parsed, dict) else {},
            })
        return {"text": (message.content or "").strip(), "tool_calls": tool_calls}
