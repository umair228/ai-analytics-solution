"""
Optional FAQ / canned-reply fast-path for AI Document Search.

Loads ``standard_responses.xlsx`` (columns ``UserInput``, ``BotReply``) once and
matches a user query against it. Exact normalized matches always work; fuzzy
semantic matching is used only when the embedder is available.

This is a thin convenience layer — greetings and a handful of LabWare facts.
It is entirely optional: if the xlsx is missing, ``faq_lookup`` returns None.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from django.conf import settings

FAQ_PASS_THRESHOLD = 0.80   # >= this similarity -> answer directly
FAQ_AMBIG_THRESHOLD = 0.50  # gray zone -> "did you mean ...?"

_CONTRACTIONS = {
    "what's": "what is", "where's": "where is", "who's": "who is", "it's": "it is",
    "i'm": "i am", "can't": "cannot", "don't": "do not", "doesn't": "does not",
    "isn't": "is not", "aren't": "are not", "won't": "will not",
}

_loaded = False
_responses: dict[str, str] = {}
_questions: list[str] = []
_embs = None


def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    for k, v in _CONTRACTIONS.items():
        s = s.replace(k, v)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load():
    global _loaded, _responses, _questions, _embs
    if _loaded:
        return
    _loaded = True
    path = Path(getattr(settings, "DOCSEARCH_FAQ_PATH", ""))
    if not path or not path.exists():
        return
    try:
        import pandas as pd
        df = pd.read_excel(path)
        for _, row in df.iterrows():
            _responses[_normalize(str(row["UserInput"]))] = str(row["BotReply"]).strip()
        _questions = list(_responses.keys())
        print(f"✓ docsearch FAQ loaded: {len(_questions)} entries")
    except Exception as exc:
        print(f"⚠️  could not load FAQ xlsx: {exc}")


def _get_embs():
    """Lazily embed FAQ questions for fuzzy matching (shares the docsearch embedder)."""
    global _embs
    if _embs is not None or not _questions:
        return _embs
    from .retrieval import get_embedder
    embedder = get_embedder()
    if embedder is None:
        return None
    try:
        _embs = embedder.encode(_questions, normalize_embeddings=True).astype("float32")
    except Exception:
        _embs = None
    return _embs


def faq_lookup(query: str) -> dict | None:
    """
    Return a match dict or None.
        {"answer": str, "score": float, "kind": "exact"|"fuzzy"|"ambiguous", "best_q": str}
    """
    _load()
    if not _questions:
        return None
    norm = _normalize(query)
    if norm in _responses:
        return {"answer": _responses[norm], "score": 1.0, "kind": "exact", "best_q": norm}

    embs = _get_embs()
    if embs is None:
        return None
    from .retrieval import get_embedder
    embedder = get_embedder()
    qv = embedder.encode([norm], normalize_embeddings=True).astype("float32")
    sims = np.dot(embs, qv.T).flatten()
    i = int(np.argmax(sims))
    score = float(sims[i])
    best_q = _questions[i]
    if score >= FAQ_PASS_THRESHOLD:
        return {"answer": _responses[best_q], "score": score, "kind": "fuzzy", "best_q": best_q}
    if score >= FAQ_AMBIG_THRESHOLD:
        return {"answer": _responses[best_q], "score": score, "kind": "ambiguous", "best_q": best_q}
    return None
