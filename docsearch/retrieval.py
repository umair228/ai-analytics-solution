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

# Per-chunk visibility owner. doc_id is NOT unique across users (two users can
# approve different content under the same doc_id), so authorization keys on this
# owner value, never on doc_id alone:
#   OWNER_GLOBAL  -> legacy physical-corpus chunk (readable by all authenticated)
#   "<user id>"   -> KB chunk owned by that user (created_by)
#   OWNER_PRIVATE -> KB chunk with no/unknown owner -> admin-only (fail closed)
OWNER_GLOBAL = "__global__"
OWNER_PRIVATE = "__private__"

CSV_COLUMNS = [
    "doc_id", "page", "chunk_id", "chunk_text", "doc_title", "title_terms",
    "keywords", "entities", "bm25_terms", "intent_tags", "chunk_len_tokens",
    "owner",
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
            "owner": str(meta.get("owner", OWNER_PRIVATE)),
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


# ───── dense-embedding helpers (shared by index build + query) ─────
def _embed_prefix(kind: str) -> str:
    from django.conf import settings
    return getattr(settings, f"DOCSEARCH_EMBED_{kind.upper()}_PREFIX", "") or ""


def encode_passages(texts):
    """Embed passages for indexing. Returns an (n, d) float32 matrix (rows
    L2-normalised) or None if the embedder is unavailable."""
    emb = get_embedder()
    if emb is None or texts is None or len(texts) == 0:
        return None
    prefix = _embed_prefix("passage")
    payload = [prefix + t for t in texts] if prefix else list(texts)
    return emb.encode(payload, normalize_embeddings=True, batch_size=64).astype("float32")


def encode_query(query: str):
    """Embed a single query. Returns a (d,) float32 vector or None."""
    emb = get_embedder()
    if emb is None:
        return None
    prefix = _embed_prefix("query")
    text = (prefix + query) if prefix else query
    return emb.encode([text], normalize_embeddings=True).astype("float32")[0]


# ───── optional cross-encoder reranker (lazy, thread-safe singleton) ─────
_reranker = None
_reranker_lock = threading.Lock()
_reranker_failed_at = 0.0


def get_reranker():
    """Return a CrossEncoder reranker or None (disabled/unavailable). Mirrors
    get_embedder's lazy + cooldown + graceful-degrade behaviour, so a missing
    model just falls back to the fused order rather than erroring a query."""
    global _reranker, _reranker_failed_at
    if _reranker is not None:
        return _reranker
    from django.conf import settings
    if not getattr(settings, "DOCSEARCH_ENABLE_RERANK", True):
        return None
    import time
    if _reranker_failed_at and (time.monotonic() - _reranker_failed_at) < _EMBEDDER_RETRY_COOLDOWN:
        return None
    with _reranker_lock:
        if _reranker is not None:
            return _reranker
        try:
            model = getattr(settings, "DOCSEARCH_RERANK_MODEL",
                            "cross-encoder/ms-marco-MiniLM-L-6-v2")
            from sentence_transformers import CrossEncoder
            _reranker = CrossEncoder(model)
            _reranker_failed_at = 0.0
            print(f"✓ docsearch reranker loaded: {model}")
        except Exception as exc:
            print(f"⚠️  cross-encoder rerank unavailable, keeping fused order: {exc}")
            _reranker = None
            _reranker_failed_at = time.monotonic()
    return _reranker


def _rrf_fuse(rankings, k: int = 60):
    """Reciprocal Rank Fusion of several ranked index lists -> one fused list."""
    scores: dict = defaultdict(float)
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] += 1.0 / (k + rank + 1)
    return [idx for idx, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


class DocIndex:
    """Hybrid retrieval over a chunks DataFrame: BM25 (+ lexical boosts) fused
    with dense semantic retrieval (RRF), then an optional cross-encoder rerank."""

    def __init__(self, df: pd.DataFrame, vectors=None):
        self.df = df.reset_index(drop=True) if df is not None else pd.DataFrame(columns=CSV_COLUMNS)
        # Dense vectors aligned 1:1 with df rows (or None -> lexical-only).
        if vectors is not None and len(vectors) == len(self.df) and len(self.df) > 0:
            self.vectors = np.asarray(vectors, dtype="float32")
        else:
            self.vectors = None
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

    # ----- lexical scoring (BM25 + boosts) over all chunks -----
    def _lexical_scores(self, query: str) -> list:
        from django.conf import settings
        df = self.df
        # Length prior: reward substantial passages. The band is scaled to the
        # configured passage size (chunk_len_tokens is the post-stopword word
        # count; ~384-token passages land ~200-260) — the old 20..200 band was
        # tuned for the previous one-sentence-per-chunk indexing.
        len_hi = int(getattr(settings, "DOCSEARCH_CHUNK_MAX_TOKENS", 512))
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
        scores = []
        for i in range(len(df)):
            row = df.iloc[i]
            base = (
                0.65 * self._bm25_score(q_toks, self._doc_tf[i], self._doc_len[i])
                + 0.20 * _overlap_score(q_keywords, set(str(row.get("keywords", "")).split("|")) - {""})
                + 0.15 * _overlap_score(q_entities, set(str(row.get("entities", "")).split("|")) - {""})
                + 0.10 * min(1.0, _overlap_score(q_tags, set(str(row.get("intent_tags", "")).split("|")) - {""}))
            )
            L = int(row.get("chunk_len_tokens", 0) or 0)
            len_bonus = 0.05 if 40 <= L <= len_hi else 0.0
            text_lower = str(row.get("chunk_text", "")).lower()
            phrase_bonus = 0.15 if any(ph.lower() in text_lower for ph in phrases) else 0.0
            title_terms_row = set(str(row.get("title_terms", "")).split("|")) - {""}
            title_overlap = _overlap_score(set(q_toks), title_terms_row)
            title_lower = str(row.get("doc_title", "")).lower()
            title_phrase_bonus = 0.20 if any(ph.lower() in title_lower for ph in phrases) else 0.0
            scores.append(
                base + len_bonus + phrase_bonus + (TITLE_OVERLAP_W * title_overlap) + title_phrase_bonus
            )
        return scores

    # ----- dense (semantic) retrieval over all chunks -----
    def _dense_search(self, query: str, pool: int):
        """Return (ordered_indices, {idx: sim}) from dense retrieval over the
        WHOLE corpus, or ([], {}) if dense retrieval is unavailable."""
        from django.conf import settings
        qv = encode_query(query)
        if qv is None:
            return [], {}
        backend = getattr(settings, "DOCSEARCH_VECTOR_BACKEND", "numpy")
        if backend == "pgvector":
            try:
                from .pgvector_store import knn
                pairs = knn(qv, pool)  # [(ordinal, sim), ...]
                order = [i for i, _ in pairs if 0 <= i < len(self.df)]
                return order, {i: float(s) for i, s in pairs if 0 <= i < len(self.df)}
            except Exception as exc:
                print(f"⚠️  pgvector dense search failed, using in-memory vectors: {exc}")
        if self.vectors is None:
            return [], {}
        try:
            # Guard against an embed-dim mismatch (e.g. DOCSEARCH_EMBED_MODEL
            # changed without a reindex). Degrade to lexical-only, never crash
            # the whole query.
            if self.vectors.shape[1] != qv.shape[0]:
                print(f"⚠️  dense dim {self.vectors.shape[1]} != query dim {qv.shape[0]}; skipping dense leg")
                return [], {}
            sims = self.vectors @ qv  # cosine — vectors are L2-normalised
        except Exception as exc:
            print(f"⚠️  dense search skipped: {exc}")
            return [], {}
        order = np.argsort(sims)[::-1][:pool].tolist()
        return order, {i: float(sims[i]) for i in order}

    # ----- cross-encoder / bi-encoder rerank of a candidate set -----
    def _rerank(self, query: str, rows: list, keep: int, debug: dict):
        if not rows:
            return rows, debug
        reranker = get_reranker()
        if reranker is not None:
            try:
                pairs = [(query, str(r.get("chunk_text", ""))) for r in rows]
                scores = np.asarray(reranker.predict(pairs)).flatten()
                order = np.argsort(scores)[::-1].tolist()
                debug["rerank"] = "cross-encoder"
                debug["rerank_top"] = float(scores[order[0]]) if len(order) else 0.0
                return [rows[i] for i in order][:keep], debug
            except Exception as exc:
                print(f"⚠️  cross-encoder rerank skipped: {exc}")
        # fallback: bi-encoder cosine rerank (the prior behaviour)
        try:
            qv = encode_query(query)
            dv = encode_passages([str(r.get("chunk_text", "")).strip() for r in rows])
            if qv is not None and dv is not None:
                sims = dv @ qv
                order = np.argsort(sims)[::-1].tolist()
                debug["rerank"] = "bi-encoder"
                debug["semantic_top_sim"] = float(sims[order[0]]) if len(order) else debug.get("semantic_top_sim", 0.0)
                return [rows[i] for i in order][:keep], debug
        except Exception as exc:
            print(f"⚠️  bi-encoder rerank skipped: {exc}")
        return rows[:keep], debug

    # ----- retrieval -----
    def retrieve(self, query: str, top_k: int = 10, expand_neighbors: int = 2,
                 allowed_owners=None) -> Tuple[list, dict]:
        from django.conf import settings
        df = self.df
        if not self.ready or df.empty:
            return [], {"best_score": 0.0, "semantic_top_sim": 0.0, "empty": True}

        pool = getattr(settings, "DOCSEARCH_CANDIDATE_POOL", 50)
        per_doc_cap = getattr(settings, "DOCSEARCH_PER_DOC_CAP", PER_DOC_CAP)
        max_chunks = getattr(settings, "DOCSEARCH_MAX_CHUNKS", 15)
        keep = getattr(settings, "DOCSEARCH_RERANK_KEEP", RERANK_KEEP)
        rrf_k = getattr(settings, "DOCSEARCH_RRF_K", 60)
        hybrid = getattr(settings, "DOCSEARCH_HYBRID", True)

        # Per-user visibility scoping. allowed_owners is None => no restriction;
        # otherwise a chunk is a candidate only if its OWNER (not its doc_id —
        # doc_ids are not unique across users) is in the set. Filtering is applied
        # BEFORE the candidate-pool cut and document selection, so chunks the user
        # MAY see are never crowded out of the pool by ones they may not.
        owners = df["owner"].to_numpy() if allowed_owners is not None else None

        def _visible(i) -> bool:
            return allowed_owners is None or str(owners[i]) in allowed_owners

        # lexical leg
        lex_scores = self._lexical_scores(query)
        lex_ranked = np.argsort(lex_scores)[::-1]
        if allowed_owners is None:
            lex_order = lex_ranked[:pool].tolist()
        else:
            lex_order = [int(i) for i in lex_ranked if _visible(i)][:pool]

        # dense leg + fusion
        dense_order, dense_map = ([], {})
        if hybrid:
            dense_order, dense_map = self._dense_search(query, pool)
            if allowed_owners is not None and dense_order:
                dense_order = [i for i in dense_order if _visible(i)]
                dense_map = {i: s for i, s in dense_map.items() if _visible(i)}
        fused = _rrf_fuse([lex_order, dense_order], k=rrf_k) if dense_order else lex_order
        if not fused:
            return [], {"best_score": 0.0, "semantic_top_sim": 0.0}

        # cap to the top-N distinct documents (in fused priority order)
        top_docs, seen_docs = [], set()
        for i in fused:
            d = df.iloc[i]["doc_id"]
            if d not in seen_docs:
                seen_docs.add(d)
                top_docs.append(d)
            if len(top_docs) >= max(1, per_doc_cap):
                break
        top_docs = set(top_docs)
        seeds = [i for i in fused if df.iloc[i]["doc_id"] in top_docs]

        # neighbour expansion within each seed's document (keeps surrounding context)
        chosen, seen = [], set()
        for idx in seeds:
            doc_id = df.iloc[idx]["doc_id"]
            for j in range(max(0, idx - expand_neighbors), min(len(df), idx + expand_neighbors + 1)):
                # Same doc_id is not enough: doc_ids are shared across users, so a
                # neighbour must also be visible to the requester (owner check),
                # otherwise expansion would re-introduce another user's chunk.
                if j in seen or df.iloc[j]["doc_id"] != doc_id or not _visible(j):
                    continue
                seen.add(j)
                chosen.append(j)
            if len(chosen) >= max_chunks:
                break
        chosen = chosen[:max_chunks]
        rows = [df.iloc[i] for i in chosen]

        debug = {
            "best_score": float(lex_scores[lex_order[0]]) if lex_order else 0.0,
            "semantic_top_sim": float(max(dense_map.values())) if dense_map else 0.0,
            "hybrid": bool(dense_order),
            "candidates": len(rows),
        }
        return self._rerank(query, rows, keep, debug)
