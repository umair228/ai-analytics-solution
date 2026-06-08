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

    # ── agentic (tool-using) turn ──────────────────────────────────────────
    @staticmethod
    def _to_anthropic_messages(messages):
        """Translate neutral messages into Anthropic's content-block format.

        Consecutive ``tool`` results are merged into ONE user message of
        ``tool_result`` blocks — Anthropic requires tool results to arrive as a
        user turn following the assistant ``tool_use`` turn."""
        out = []
        pending_results = []

        def flush_results():
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for m in messages or []:
            role = m.get("role")
            if role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id"),
                    "content": m.get("content") or "",
                })
                continue
            flush_results()
            if role == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for call in m.get("tool_calls") or []:
                    blocks.append({
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("input") or {},
                    })
                out.append({"role": "assistant", "content": blocks or m.get("content", "")})
            else:  # user
                out.append({"role": "user", "content": m.get("content") or ""})
        flush_results()
        return out

    def complete(self, *, system_blocks, messages, tools, max_tokens) -> dict:
        kwargs = {
            "model": settings.CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": self._to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema") or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
        response = self._client_().messages.create(**kwargs)

        text_parts, tool_calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input or {}),
                })
        return {"text": "\n".join(text_parts).strip(), "tool_calls": tool_calls}
