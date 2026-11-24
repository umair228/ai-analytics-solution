"""Local provider — any OpenAI-compatible server (Ollama today, vLLM on the GPU
box later). Air-gapped: it only talks to ``settings.LLM_BASE_URL``."""
from __future__ import annotations

import json

from django.conf import settings

from . import egress
from .base import BaseProvider


class OpenAIProvider(BaseProvider):
    name = "local"

    def __init__(self):
        self._client = None

    @property
    def configured(self) -> bool:
        url = getattr(settings, "LLM_BASE_URL", "")
        return bool(url) and egress.endpoint_permitted(url)

    @property
    def model_name(self) -> str:
        return getattr(settings, "LLM_MODEL", "")

    def not_configured_message(self) -> str:
        url = getattr(settings, "LLM_BASE_URL", "")
        if url and not egress.endpoint_permitted(url):
            return (
                f"LLM_BASE_URL ({url}) is not an on-premise address, and "
                "DSE_ALLOW_EXTERNAL_LLM is not enabled. This build will not send "
                "prompts — which include sample LIMS rows — off-network. Point it "
                "at your on-prem model server (Ollama or vLLM)."
            )
        return (
            "No local LLM endpoint configured. Set LLM_BASE_URL (and LLM_MODEL) "
            "to your on-prem OpenAI-compatible server (Ollama or vLLM)."
        )

    def _client_(self):
        if self._client is None:
            # "Local" has to mean local: this client is a generic OpenAI-compatible
            # one and would happily talk to api.openai.com if LLM_BASE_URL said so.
            egress.assert_endpoint_permitted(settings.LLM_BASE_URL)
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
        """Sampling + (Ollama) context-window options shared by chat/complete.

        ``seed`` is top-level: Ollama's OpenAI-compatible endpoint and vLLM both
        accept it there. With temperature=0 it makes a re-run of the same question
        reproduce the same answer — which is what a QC audit asks for."""
        kwargs = {
            "temperature": float(getattr(settings, "LLM_TEMPERATURE", 0.0)),
            "seed": int(getattr(settings, "LLM_SEED", 42)),
        }
        # Ollama reads `options` from the OpenAI endpoint via extra_body; vLLM
        # ignores it harmlessly (context/sampling are fixed at serve time).
        options = {}
        num_ctx = getattr(settings, "LLM_NUM_CTX", 0)
        if num_ctx:
            options["num_ctx"] = num_ctx
        rp = getattr(settings, "LLM_REPEAT_PENALTY", 0)
        if rp:
            options["repeat_penalty"] = rp
        if options:
            kwargs["extra_body"] = {"options": options}
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
