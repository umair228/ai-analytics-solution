"""
BM25 retrieval + optional semantic rerank for AI Document Search.

A faithful port of the LW-Chatbot "CSV-BM25 TitleBoost FastPath" pipeline
(originally in ``LWChatbot.py``), refactored into a reusable ``DocIndex`` class:

  * chunk metadata (keywords, entities, BM25 terms, title terms, intent tags)
    is precomputed into a DataFrame  -> ``build_chunks_dataframe``
  * retrieval = BM25 keyword scoring + title-overlap / keyword / entity / intent
    boosts + multi-word phrase bonuses, then neighbour expansion within the best
    document, then an OPTIONAL MiniLM semantic rerank  -> ``DocIndex.retrieve``

The semantic rerank uses ``sentence-transformers`` (all-MiniLM-L12-v2). It is
loaded lazily and only when ``settings.DOCSEARCH_ENABLE_SEMANTIC`` is true and the
library is importable; otherwise retrieval degrades gracefully to BM25 + boosts.
"""
from __future__ import annotations

import math
import re
import string
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

# ───── Retrieval tuning (mirrors LW-Chatbot constants) ─────
PER_DOC_CAP = 1            # 1 => restrict the final answer to the single best document
RERANK_KEEP = 10          # keep top-N after semantic rerank
TITLE_OVERLAP_W = 0.25

CSV_COLUMNS = [
    "doc_id", "page", "chunk_id", "chunk_text", "doc_title", "title_terms",
    "keywords", "entities", "bm25_terms", "intent_tags", "chunk_len_tokens",
]

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at", "by", "with", "from", "as",
    "is", "are", "was", "were", "be", "been", "being", "that", "this", "these", "those", "it", "its",
    "we", "you", "they", "i", "he", "she", "them", "his", "her", "our", "your", "their",
}
_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation})

_SECTION_HINTS = {
    "procedure", "steps", "how to", "configuration", "config", "setup", "definition", "overview",
    "policy", "requirement", "requirements", "example", "error", "troubleshoot", "sql", "query",
    "path", "file", "endpoint", "authentication", "authorization", "inventory", "retest", "chemistry",
}
_DATE_RE = re.compile(r"\b(20\d{2}|19\d{2})\b")
_ID_RE = re.compile(r"\b[A-Za-z0-9_-]{6,}\b")

_HEADER_PATTERNS = [
    r"^©\s*LabWare.*?(?=\s[A-Z]|\s[a-z]|\s\d|$)",
    r"^LabWare\s+ELN\s+Overview.*",
    r"^\s*©\s*\S.*$",
]


# ───── tokenisation / feature helpers ─────
def _normalize_basic(text: str) -> str:
    s = (text or "").lower().translate(_PUNCT_TABLE)
    return re.sub(r"\s+", " ", s).strip()


def _tokenize_bm25(text: str) -> List[str]:
    return [t for t in _normalize_basic(text).split() if t not in _STOP]


def _extract_keywords_simple(text: str, k: int = 8) -> List[str]:
    toks = _tokenize_bm25(text)
    ranked = sorted(Counter(toks).items(), key=lambda x: (x[1], len(x[0])), reverse=True)
    return [w for w, _ in ranked[:k]]


def _intent_tags_from_text(text: str) -> List[str]:
    s = _normalize_basic(text)
    return [h for h in _SECTION_HINTS if h in s]


def _extract_entities_simple(text: str) -> List[str]:
    ents = [f"DATE:{m.group(0)}" for m in _DATE_RE.finditer(text)]
    ents += [f"ID:{m.group(0)}" for m in _ID_RE.finditer(text)]
    return list(dict.fromkeys(ents))


def _strip_headers_footers(text: str) -> str:
    t = text or ""
    for pat in _HEADER_PATTERNS:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    t = re.sub(r"[•·►▪●■]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _overlap_score(set_a: set, set_b: set) -> float:
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / max(1, min(len(set_a), len(set_b)))


def build_chunks_dataframe(chunk_texts, chunk_metas) -> pd.DataFrame:
    """Build the BM25 fast-path table from extracted chunks + metadata."""
    rows = []
    for i, (txt, meta) in enumerate(zip(chunk_texts, chunk_metas)):
        doc_id = meta.get("source", "unknown")
        chunk_text = _strip_headers_footers((txt or "").strip())
        if not chunk_text or len(chunk_text.split()) < 4:
            continue
        title_norm = re.sub(r"[_\-]+", " ", Path(doc_id).stem).strip()
        bm25_terms = " ".join(_tokenize_bm25(chunk_text))
        rows.append({
            "doc_id": doc_id,
            "page": meta.get("page"),
            "chunk_id": i,
            "chunk_text": chunk_text,
            "doc_title": title_norm,
            "title_terms": "|".join(_tokenize_bm25(title_norm)),
            "keywords": "|".join(_extract_keywords_simple(chunk_text, k=8)),
            "entities": "|".join(_extract_entities_simple(chunk_text)),
            "bm25_terms": bm25_terms,
            "intent_tags": "|".join(_intent_tags_from_text(chunk_text)),
            "chunk_len_tokens": len(bm25_terms.split()),
        })
    return pd.DataFrame(rows, columns=CSV_COLUMNS)


# ───── optional semantic embedder (lazy, thread-safe singleton) ─────
_embedder = None
_embedder_lock = threading.Lock()
_embedder_failed_at = 0.0
_EMBEDDER_RETRY_COOLDOWN = 300  # seconds — retry a transient load failure later


def get_embedder():
    """Return a SentenceTransformer or None (semantic rerank disabled/unavailable).

    Thread-safe (double-checked locking) so concurrent first requests don't each
    load a model. A transient load failure is retried after a cooldown rather than
    being latched off for the process lifetime."""
    global _embedder, _embedder_failed_at
    if _embedder is not None:
        return _embedder
    from django.conf import settings
    if not getattr(settings, "DOCSEARCH_ENABLE_SEMANTIC", True):
        return None
    import time
    if _embedder_failed_at and (time.monotonic() - _embedder_failed_at) < _EMBEDDER_RETRY_COOLDOWN:
        return None
    with _embedder_lock:
        if _embedder is not None:
            return _embedder
        try:
            model = getattr(settings, "DOCSEARCH_EMBED_MODEL", "all-MiniLM-L12-v2")
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer(model)
            _embedder_failed_at = 0.0
            print(f"✓ docsearch semantic embedder loaded: {model}")
        except Exception as exc:
            print(f"⚠️  semantic rerank unavailable, using BM25 only: {exc}")
            _embedder = None
            _embedder_failed_at = time.monotonic()
    return _embedder


class DocIndex:
    """In-memory BM25 index over a chunks DataFrame, with optional semantic rerank."""

    def __init__(self, df: pd.DataFrame):
        self.df = df.reset_index(drop=True) if df is not None else pd.DataFrame(columns=CSV_COLUMNS)
        self._doc_tf: List[Counter] = []
        self._doc_len: List[int] = []
        self._idf: dict = {}
        self._avg_len: float = 1.0
        self.ready = False
        self._build_bm25()

    # ----- index build -----
    def _build_bm25(self):
        terms = self.df["bm25_terms"].fillna("") if not self.df.empty else []
        n = len(terms)
        df_counts: dict = defaultdict(int)
        self._doc_tf, self._doc_len = [], []
        for s in terms:
            toks = str(s).split()
            tf = Counter(toks)
            self._doc_tf.append(tf)
            self._doc_len.append(len(toks))
            for t in tf:
                df_counts[t] += 1
        self._avg_len = (sum(self._doc_len) / n) if n else 1.0
        self._idf = {
            t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df_counts.items()
        }
        self.ready = n > 0

    def _bm25_score(self, q_toks, tf: Counter, doc_len: int, k: float = 1.2, b: float = 0.6) -> float:
        score = 0.0
        for q in q_toks:
            idf = self._idf.get(q)
            if not idf:
                continue
            f = tf.get(q, 0)
            if not f:
                continue
            denom = f + k * (1 - b + b * (doc_len / max(1.0, self._avg_len)))
            score += idf * (f * (k + 1)) / denom
        return score

    # ----- retrieval -----
    def retrieve(self, query: str, top_k: int = 10, expand_neighbors: int = 2) -> Tuple[list, dict]:
        df = self.df
        if not self.ready or df.empty:
            return [], {"best_score": 0.0, "semantic_top_sim": 0.0, "empty": True}

        q_toks = _tokenize_bm25(query)
        q_keywords = set(_extract_keywords_simple(query, k=6))
        q_entities = set(_extract_entities_simple(query))
        q_tags = set(_intent_tags_from_text(query))
        phrases = [
            p.strip() for p in re.findall(
                r"\b([a-zA-Z][a-zA-Z0-9]+\s+[a-zA-Z0-9][a-zA-Z0-9]+(?:\s+[a-zA-Z0-9][a-zA-Z0-9]+)?)\b",
                query,
            )
        ]

        final_scores = []
        for i in range(len(df)):
            row = df.iloc[i]
            base = (
                0.65 * self._bm25_score(q_toks, self._doc_tf[i], self._doc_len[i])
                + 0.20 * _overlap_score(q_keywords, set(str(row.get("keywords", "")).split("|")) - {""})
                + 0.15 * _overlap_score(q_entities, set(str(row.get("entities", "")).split("|")) - {""})
                + 0.10 * min(1.0, _overlap_score(q_tags, set(str(row.get("intent_tags", "")).split("|")) - {""}))
            )
            L = int(row.get("chunk_len_tokens", 0) or 0)
            len_bonus = 0.05 if 20 <= L <= 200 else 0.0
            text_lower = str(row.get("chunk_text", "")).lower()
            phrase_bonus = 0.15 if any(ph.lower() in text_lower for ph in phrases) else 0.0
            title_terms_row = set(str(row.get("title_terms", "")).split("|")) - {""}
            title_overlap = _overlap_score(set(q_toks), title_terms_row)
            title_lower = str(row.get("doc_title", "")).lower()
            title_phrase_bonus = 0.20 if any(ph.lower() in title_lower for ph in phrases) else 0.0
            final_scores.append(
                base + len_bonus + phrase_bonus + (TITLE_OVERLAP_W * title_overlap) + title_phrase_bonus
            )

        top_idx = np.argsort(final_scores)[-top_k:][::-1].tolist()

        # restrict to the single best document
        if PER_DOC_CAP == 1 and top_idx:
            top_doc_id = df.iloc[top_idx[0]]["doc_id"]
            top_idx = [i for i in top_idx if df.iloc[i]["doc_id"] == top_doc_id]

        # neighbour expansion within the same document
        chosen, seen = [], set()
        for idx in top_idx:
            doc_id = df.iloc[idx]["doc_id"]
            for j in range(max(0, idx - expand_neighbors), min(len(df), idx + expand_neighbors + 1)):
                if j in seen:
                    continue
                if df.iloc[j]["doc_id"] == doc_id:
                    seen.add(j)
                    chosen.append(j)
        chosen = chosen[:12]
        rows = [df.iloc[i] for i in chosen]

        debug = {"best_score": float(final_scores[top_idx[0]]) if top_idx else 0.0, "semantic_top_sim": 0.0}

        # optional semantic rerank
        embedder = get_embedder()
        if embedder is not None and rows:
            try:
                cand = [str(r.get("chunk_text", "")).strip() for r in rows]
                qv = embedder.encode([query], normalize_embeddings=True).astype("float32")
                dv = embedder.encode(cand, normalize_embeddings=True).astype("float32")
                sims = (dv @ qv.T).flatten()
                order = np.argsort(sims)[::-1].tolist()
                rows = [rows[i] for i in order][:RERANK_KEEP]
                debug["semantic_top_sim"] = float(sims[order[0]]) if len(order) else 0.0
            except Exception as exc:
                print(f"⚠️  semantic rerank skipped: {exc}")

        return rows, debug
