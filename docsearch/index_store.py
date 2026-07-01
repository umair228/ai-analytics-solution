"""
Index lifecycle for AI Document Search.

Owns the on-disk chunk index (a CSV under ``media/docsearch/``) and an in-memory
``DocIndex`` singleton. The index is built from the corpus directory
(``settings.DOCSEARCH_CORPUS_DIR``) and lazily (re)loaded on first use.

Building the index needs no ML — only the document text extractors + BM25 — so
``get_index()`` is cheap. The semantic embedder (if enabled) is loaded lazily by
``retrieval.get_embedder`` at query time.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import pandas as pd
from django.conf import settings

from .retrieval import CSV_COLUMNS, OWNER_GLOBAL, DocIndex, build_chunks_dataframe
from .text_extract import SUPPORTED_EXTS, extract_corpus

_INDEX: DocIndex | None = None
_LOCK = threading.RLock()


def corpus_dir() -> Path:
    p = Path(settings.DOCSEARCH_CORPUS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def index_path() -> Path:
    p = Path(settings.DOCSEARCH_INDEX_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def vectors_path() -> Path:
    p = Path(getattr(settings, "DOCSEARCH_VECTORS_PATH",
                     str(index_path().with_name("chunk_vectors.npy"))))
    if p.suffix != ".npy":  # np.save would force .npy; keep save/load in sync
        p = p.with_suffix(".npy")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _meta_path() -> Path:
    vp = vectors_path()
    return vp.with_name(vp.stem + ".meta.json")


def _fingerprint(df) -> str:
    """Content fingerprint of the chunk set + embedding model. Detects content
    drift even when the row COUNT is unchanged (e.g. one chunk swapped for
    another), which a bare length check cannot."""
    h = hashlib.sha1()
    h.update(str(getattr(settings, "DOCSEARCH_EMBED_MODEL", "")).encode("utf-8"))
    h.update(b"\x00")
    for t in df["chunk_text"].tolist():
        h.update(str(t).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _clear_vectors():
    """Remove persisted vectors so a failed/degraded rebuild can never leave a
    stale store that later misaligns with the chunk rows."""
    vectors_path().unlink(missing_ok=True)
    _meta_path().unlink(missing_ok=True)
    if getattr(settings, "DOCSEARCH_VECTOR_BACKEND", "numpy") == "pgvector":
        try:
            from .pgvector_store import truncate
            truncate()
        except Exception as exc:
            print(f"⚠️  could not clear pgvector table: {exc}")


def _build_vectors(df):
    """Embed every chunk (aligned to df row order), persist to .npy + a content
    fingerprint, and — for the pgvector backend — push them to Postgres too.
    Returns the matrix, or None if the embedder is unavailable (index degrades
    to lexical-only)."""
    if df is None or df.empty:
        return None
    try:
        import numpy as np
        from .retrieval import encode_passages
        vecs = encode_passages([str(t) for t in df["chunk_text"].tolist()])
        if vecs is None:
            return None
        np.save(vectors_path(), vecs)
        _meta_path().write_text(json.dumps({
            "count": int(len(df)),
            "dim": int(vecs.shape[1]),
            "model": getattr(settings, "DOCSEARCH_EMBED_MODEL", ""),
            "fingerprint": _fingerprint(df),
        }))
        if getattr(settings, "DOCSEARCH_VECTOR_BACKEND", "numpy") == "pgvector":
            try:
                from .pgvector_store import replace_all
                replace_all(df, vecs)
            except Exception as exc:
                print(f"⚠️  pgvector store update failed (using in-memory vectors): {exc}")
        return vecs
    except Exception as exc:
        print(f"⚠️  could not build dense vectors: {exc}")
        return None


def _load_vectors(df):
    """Load persisted vectors only if they match the current chunk set by both
    row count AND content fingerprint (so same-count content drift is rejected)."""
    try:
        import numpy as np
        p = vectors_path()
        if not p.exists():
            return None
        meta = None
        if _meta_path().exists():
            try:
                meta = json.loads(_meta_path().read_text())
            except Exception:
                meta = None
        if not meta or meta.get("count") != len(df) or meta.get("fingerprint") != _fingerprint(df):
            return None  # missing/stale/content-drift -> force rebuild
        v = np.load(p)
        if getattr(v, "shape", [0])[0] != len(df):
            return None
        return v
    except Exception as exc:
        print(f"⚠️  could not load vectors: {exc}")
    return None


def _load_df() -> pd.DataFrame | None:
    path = index_path()
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        print(f"⚠️  could not read index {path}: {exc}")
        return None
    if not set(CSV_COLUMNS).issubset(df.columns) or df.empty:
        return None
    return df


def _dedupe_chunks(texts, metas):
    """Drop exact (normalised) duplicate chunks across all sources, preserving
    order so each ``doc_id``'s chunks stay contiguous for neighbour-expansion."""
    seen: set = set()
    out_t, out_m = [], []
    for t, m in zip(texts, metas):
        key = re.sub(r"\s+", " ", (t or "").lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out_t.append(t)
        out_m.append(m)
    return out_t, out_m


def _build_df() -> pd.DataFrame:
    """Build the chunk DataFrame from the union of corpus files + approved
    knowledge-base records, deduped across sources. Wholesale by design: BM25
    IDF is global, so the whole table must be rebuilt together."""
    texts, metas = extract_corpus(corpus_dir())
    texts, metas = list(texts), list(metas)
    # Legacy physical-corpus chunks are global-readable to any authenticated user.
    for m in metas:
        m["owner"] = OWNER_GLOBAL
    try:
        from .ingest import approved_record_chunks  # local: avoid import cycle
        kb_texts, kb_metas = approved_record_chunks()  # these carry their own owner
        texts += list(kb_texts)
        metas += list(kb_metas)
    except Exception as exc:  # never let KB merge break the file index
        print(f"⚠️  could not merge knowledge records into index: {exc}")
    texts, metas = _dedupe_chunks(texts, metas)
    return build_chunks_dataframe(texts, metas)


def rebuild_index() -> dict:
    """(Re)build the index from corpus + approved KB records, persist, refresh.
    Recomputes dense vectors so they stay aligned with the chunk rows.

    Best-effort and resilient: a failure in any single step is logged and skipped
    rather than raised, so an approve/upload/delete never 500s on a transient
    IO/embed error. Callers on the request path (KnowledgeApproveView,
    DocumentsView) rely on this contract — the DB action has already committed by
    the time we get here, so indexing is a best-effort follow-on, never a gate."""
    global _INDEX
    with _LOCK:
        try:
            df = _build_df()
        except Exception as exc:  # keep the prior index rather than crash the caller
            print(f"⚠️  rebuild_index: could not build chunk frame, keeping prior index: {exc}")
            return _safe_status()
        try:
            df.to_csv(index_path(), index=False, encoding="utf-8")
        except Exception as exc:
            print(f"⚠️  rebuild_index: could not persist index CSV: {exc}")
        vectors = _build_vectors(df)  # already returns None on any failure
        if vectors is None:  # keep the vector store lifecycle atomic with the CSV
            try:
                _clear_vectors()
            except Exception as exc:
                print(f"⚠️  rebuild_index: could not clear stale vectors: {exc}")
        try:
            _INDEX = DocIndex(df, vectors=vectors)
        except Exception as exc:
            print(f"⚠️  rebuild_index: could not construct in-memory index: {exc}")
    return _safe_status()


def get_index() -> DocIndex:
    """Return the in-memory index, loading from CSV (+ persisted vectors) or
    building from scratch."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _LOCK:
        if _INDEX is not None:
            return _INDEX
        df = _load_df()
        if df is None:
            df = _build_df()
            try:
                df.to_csv(index_path(), index=False, encoding="utf-8")
            except Exception as exc:
                print(f"⚠️  could not persist index: {exc}")
            vectors = _build_vectors(df)
        else:
            vectors = _load_vectors(df)
            if vectors is None:  # CSV present but vectors missing/stale/drifted -> recompute
                vectors = _build_vectors(df)
        if vectors is None:
            _clear_vectors()
        _INDEX = DocIndex(df, vectors=vectors)
    return _INDEX


def list_corpus_files() -> list[dict]:
    """List indexable files in the corpus with size + indexed chunk count."""
    df = get_index().df
    counts = (
        df.groupby("doc_id").size().to_dict() if not df.empty else {}
    )
    files = []
    for p in sorted(corpus_dir().iterdir()):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        files.append({
            "name": p.name,
            "ext": p.suffix.lower().lstrip("."),
            "size": p.stat().st_size,
            "chunks": int(counts.get(p.name, 0)),
        })
    return files


def list_indexed_sources(user=None) -> list[dict]:
    """List everything currently searchable, grouped by ``doc_id``. Unlike
    ``list_corpus_files`` (which walks the corpus DIR), this reads the index, so
    it also surfaces dataset-sourced virtual documents that have no file.

    When ``user`` is given, the listing is scoped to the doc_ids that user may see
    (slice-1 owner/global policy); pass ``None`` to list everything (internal use)."""
    df = get_index().df
    if df.empty:
        return []
    if user is not None:
        from .visibility import visible_owner_keys
        allowed = visible_owner_keys(user)
        if allowed is not None:
            df = df[df["owner"].astype(str).isin(allowed)]
            if df.empty:
                return []
    out = []
    for doc_id, grp in df.groupby("doc_id"):
        doc_id = str(doc_id)
        out.append({
            "doc_id": doc_id,
            "source_type": "dataset" if doc_id.startswith("dataset:") else "document",
            "chunks": int(len(grp)),
        })
    return sorted(out, key=lambda r: r["doc_id"])


def status() -> dict:
    idx = get_index()
    df = idx.df
    doc_count = int(df["doc_id"].nunique()) if not df.empty else 0
    return {
        "indexed": bool(idx.ready),
        "documents": doc_count,
        "chunks": int(len(df)),
        "corpus_dir": str(corpus_dir()),
        "index_path": str(index_path()),
    }


def _safe_status() -> dict:
    """``status()`` that never raises — ``status()`` calls ``get_index()``, which
    can trigger a (re)build if ``_INDEX`` is unset. Used by ``rebuild_index`` so a
    failed rebuild still returns a well-formed payload instead of propagating."""
    try:
        return status()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  status() failed after rebuild: {exc}")
        ready = _INDEX is not None and getattr(_INDEX, "ready", False)
        chunks = int(len(_INDEX.df)) if _INDEX is not None else 0
        return {
            "indexed": bool(ready),
            "documents": 0,
            "chunks": chunks,
            "corpus_dir": str(corpus_dir()),
            "index_path": str(index_path()),
        }
