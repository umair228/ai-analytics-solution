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

import threading
from pathlib import Path

import pandas as pd
from django.conf import settings

from .retrieval import CSV_COLUMNS, DocIndex, build_chunks_dataframe
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


def rebuild_index() -> dict:
    """(Re)extract the corpus, persist the CSV, and refresh the singleton."""
    global _INDEX
    with _LOCK:
        texts, metas = extract_corpus(corpus_dir())
        df = build_chunks_dataframe(texts, metas)
        df.to_csv(index_path(), index=False, encoding="utf-8")
        _INDEX = DocIndex(df)
    return status()


def get_index() -> DocIndex:
    """Return the in-memory index, loading from CSV or building from corpus."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _LOCK:
        if _INDEX is not None:
            return _INDEX
        df = _load_df()
        if df is None:
            texts, metas = extract_corpus(corpus_dir())
            df = build_chunks_dataframe(texts, metas)
            try:
                df.to_csv(index_path(), index=False, encoding="utf-8")
            except Exception as exc:
                print(f"⚠️  could not persist index: {exc}")
        _INDEX = DocIndex(df)
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
