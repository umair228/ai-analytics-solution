"""
Answer synthesis for AI Document Search.

Given the chunks retrieved by ``retrieval.DocIndex.retrieve``, produce a final
answer plus source citations. Two engines:

  * **claude** — Retrieval-Augmented Generation. The retrieved excerpts are fed
    to the shared DSE AI client (``ai.client.run_chat``) as grounding context and
    Claude writes a concise, cited answer. Used whenever ``CLAUDE_API_KEY`` is set.
  * **extractive** — offline fallback (no LLM): the most query-relevant sentences
    from the retrieved chunks are stitched together. Ported from the LW-Chatbot
    ``format_doc_answer_from_rows`` (minus the distilbart summarizer).

Both return: ``{"answer": str, "sources": [{"doc","page","label"}], "engine": str}``.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .retrieval import _tokenize_bm25

MAX_CONTEXT_CHARS = 6000
TOP_SENTENCES = 24


def _clean_page(value):
    """Normalise a chunk 'page' (section index) into a clean, JSON-safe value.

    A page-less passage is stored by pandas as float ``NaN``, and an int page read
    back from a mixed-dtype column arrives as a float like ``3.0``. Coerce NaN ->
    ``"?"`` and integral floats -> ``int`` so the response never carries a
    non-JSON-compliant ``NaN`` (which crashes DRF's renderer) and labels never
    read "section nan"."""
    if isinstance(value, float):  # numpy.float64 is a float subclass, caught here
        if not math.isfinite(value):
            return "?"
        if float(value).is_integer():
            return int(value)
        return float(value)
    return value


def _unique_sources(rows) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        doc = str(r.get("doc_id", "unknown"))
        page = _clean_page(r.get("page", "?"))
        key = (doc, str(page))
        if key in seen:
            continue
        seen.add(key)
        out.append({"doc": doc, "page": page, "label": f"{doc} (section {page})"})
    return out


def _ordered_unique_chunks(rows) -> list[str]:
    rows_sorted = sorted(rows, key=lambda r: (str(r.get("doc_id", "")), int(r.get("chunk_id", 0) or 0)))
    texts, seen = [], set()
    for r in rows_sorted:
        t = str(r.get("chunk_text", "")).strip()
        if t and t not in seen:
            seen.add(t)
            texts.append(t)
    return texts


def _extractive_answer(rows, query: str) -> str:
    if not rows:
        return "I couldn't find anything relevant in the indexed documents."
    # Collect sentences with provenance (doc, chunk, position), de-duped in order.
    seen, pool = set(), []
    for r in rows:
        doc_id = str(r.get("doc_id", ""))
        chunk_id = int(r.get("chunk_id", 0) or 0)
        for pos, sent in enumerate(re.split(r"(?<=[.!?])\s+", str(r.get("chunk_text", "")).strip())):
            s = sent.strip()
            if len(s.split()) >= 4 and s not in seen:
                seen.add(s)
                pool.append((doc_id, chunk_id, pos, s))
    if not pool:
        return (" ".join(_ordered_unique_chunks(rows)))[:MAX_CONTEXT_CHARS] or "No relevant content found."

    q_toks = _tokenize_bm25(query)

    def _score(item):
        tf = Counter(_tokenize_bm25(item[3]))
        return sum(tf[t] for t in q_toks if t in tf)

    # Pick the most relevant sentences, then re-emit them in document order so the
    # prose reads coherently (and deterministically).
    top = sorted(pool, key=lambda it: (_score(it), -it[1], -it[2]), reverse=True)[:TOP_SENTENCES]
    top.sort(key=lambda it: (it[0], it[1], it[2]))
    combined = " ".join(it[3] for it in top)
    return combined[:MAX_CONTEXT_CHARS] or "No relevant content found."


def _build_rag_context(rows) -> str:
    """Number the retrieved excerpts so Claude can cite them."""
    chunks = _ordered_unique_chunks(rows)
    src = _unique_sources(rows)
    lines = ["Documentation excerpts (cite by [n]):", ""]
    for i, text in enumerate(chunks, start=1):
        lines.append(f"[{i}] {text}")
    lines += ["", "Sources:"]
    for i, s in enumerate(src, start=1):
        lines.append(f"({i}) {s['doc']}")
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


_RAG_INSTRUCTION = (
    "Answer the user's question using ONLY the documentation excerpts above, which "
    "come from internal LabWare LIMS knowledge documents. Be concise and specific. "
    "If the excerpts do not contain the answer, say so plainly rather than guessing. "
    "Cite the source document name(s) you used at the end like: Source: <filename>.\n\n"
    "Question: {q}"
)


def synthesize_answer(query: str, rows, history=None) -> dict:
    """Produce the final answer + citations from retrieved rows."""
    sources = _unique_sources(rows)[:4]

    if not rows:
        return {
            "answer": "I couldn't find anything relevant in the indexed documents. "
                      "Try rephrasing, or upload the relevant document.",
            "sources": [],
            "engine": "none",
        }

    # Prefer grounded LLM synthesis when configured.
    try:
        from ai.client import is_configured, run_chat
        if is_configured():
            context = _build_rag_context(rows)
            messages = []
            for turn in (history or [])[-4:]:
                role = turn.get("role")
                content = (turn.get("content") or "").strip()
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": _RAG_INSTRUCTION.format(q=query)})
            answer = run_chat(messages, dataset_context=context)
            return {"answer": answer, "sources": sources, "engine": "claude"}
    except Exception as exc:
        print(f"⚠️  RAG synthesis failed, falling back to extractive: {exc}")

    return {"answer": _extractive_answer(rows, query), "sources": sources, "engine": "extractive"}
