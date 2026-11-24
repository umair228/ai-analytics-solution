"""Is the model answering questions the model that was qualified?

Preflight has warned for a while that ``LLM_MODEL=qwen2.5:7b-instruct`` is a mutable tag,
and told the operator to *"pin the model by digest"*. That advice cannot be followed:
Ollama has no digest-pinned model reference — you cannot write ``qwen2.5:7b@sha256:…`` and
have it mean anything. So the check was scolding people for not doing something impossible,
which is the same failure as the REVOKE that a superuser ignores.

What is possible, and what actually matters, is **detecting that it changed**.

Ollama's ``/api/tags`` reports a content digest for every model it holds. Record it when the
system is qualified; check it afterwards. If the digest has moved, someone has re-pulled the
tag and **the model answering questions today is not the model the Operational Qualification
was run against** — every accuracy claim, every golden-set pass, the whole `/validation`
page, is evidence about a different thing. That is not a warning; that is the qualification
being void.

The obvious objection is that this is only as good as the recorded value. Yes — and that is
why it is recorded in ``.env.prod`` (part of the deployment's configuration record) rather
than derived from the running system, which would be a control reporting on itself.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from django.conf import settings


def _tags_url() -> str:
    """Ollama's model list. LLM_BASE_URL points at the OpenAI-compatible shim (…/v1);
    the native API sits alongside it."""
    base = str(getattr(settings, "LLM_BASE_URL", "") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/api/tags"


def live_digest(model: str | None = None, timeout: float = 5.0) -> tuple[str | None, str]:
    """The digest the serving host currently holds for ``model``.

    Returns ``(digest, note)``. ``digest`` is None when it cannot be determined — and that
    is reported as unknown, never as agreement. A check that could not run has proven
    nothing.
    """
    model = model or str(getattr(settings, "LLM_MODEL", "") or "")
    if not model:
        return None, "LLM_MODEL is not set."

    try:
        with urllib.request.urlopen(_tags_url(), timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return None, f"could not reach the model host ({exc})"

    for entry in payload.get("models", []) or []:
        # Ollama reports "qwen2.5:7b-instruct" as both `name` and `model`, and a bare
        # "qwen2.5" as "qwen2.5:latest" — so compare on both, and on the implicit :latest.
        names = {entry.get("name"), entry.get("model")}
        if model in names or f"{model}:latest" in names:
            digest = entry.get("digest") or ""
            return (digest or None), ("" if digest else "the host reports no digest")

    have = ", ".join(sorted({e.get("name", "?") for e in payload.get("models", [])})) or "none"
    return None, f"the host is not serving '{model}' (it has: {have})"


def recorded_digest() -> str:
    """What the deployment's configuration says the qualified model was."""
    return str(getattr(settings, "DSE_LLM_MODEL_DIGEST", "") or "").strip()
