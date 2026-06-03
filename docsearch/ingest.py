"""
Knowledge-base ingestion + staging engine for AI Document Search.

Turns two kinds of source into staged ``KnowledgeRecord`` rows:

  * **documents** — uploaded files, extracted + passage-packed by ``text_extract``
  * **dataset rows** — rendered from ``datasets.Dataset.cached_rows`` into readable
    passages (optionally grouped, e.g. per sample)

Every ingest runs dedupe (exact normalised-content hash) and lightweight
validation, then lands ``pending`` for human review. ``approve_record`` flips a
record to ``approved`` and rebuilds the searchable index so the content becomes
retrievable; ``approved_record_chunks`` is what ``index_store`` reads back.

This is retrieval indexing (RAG), not model training.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import List, Tuple

from django.utils import timezone

from .models import KnowledgeRecord
from .text_extract import _est_tokens, _pack_settings, extract_file


# ──────────────────────────────────────────────────────────────────────────
# Hashing / dedupe / validation
# ──────────────────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def normalized_content_hash(passages: List[dict]) -> str:
    """sha256 over the normalised concatenation of all passage texts."""
    blob = "\n".join(p.get("text", "") for p in passages)
    return hashlib.sha256(_normalize(blob).encode("utf-8")).hexdigest()


def _validate(passages: List[dict]) -> dict:
    """Return ``{"warnings": [...], "errors": [...]}``. Errors block approval."""
    warnings: List[str] = []
    errors: List[str] = []
    if not passages:
        errors.append("No extractable text was found.")
        return {"warnings": warnings, "errors": errors}
    short = sum(1 for p in passages if len((p.get("text") or "").split()) < 4)
    if short:
        warnings.append(
            f"{short} passage(s) are too short and will be skipped by the index."
        )
    total_tokens = sum(_est_tokens(p.get("text", "")) for p in passages)
    if total_tokens < 8:
        errors.append("Content is too small to index.")
    return {"warnings": warnings, "errors": errors}


def _find_active_duplicate(content_hash: str, exclude_id=None) -> KnowledgeRecord | None:
    """An existing pending/approved record with identical content, if any."""
    qs = KnowledgeRecord.objects.filter(
        content_hash=content_hash,
        status__in=[KnowledgeRecord.Status.APPROVED, KnowledgeRecord.Status.PENDING],
    )
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs.first()


def _stage(*, source_type, doc_id, passages, created_by,
           dataset=None, source_file="", ingest_options=None) -> KnowledgeRecord:
    content_hash = normalized_content_hash(passages)
    validation = _validate(passages)
    dup = _find_active_duplicate(content_hash)
    status = KnowledgeRecord.Status.DUPLICATE if dup else KnowledgeRecord.Status.PENDING
    return KnowledgeRecord.objects.create(
        source_type=source_type,
        doc_id=doc_id,
        passages=passages,
        content_hash=content_hash,
        dataset=dataset,
        source_file=source_file,
        ingest_options=ingest_options or {},
        passage_count=len(passages),
        token_estimate=sum(_est_tokens(p.get("text", "")) for p in passages),
        validation=validation,
        status=status,
        duplicate_of=dup,
        created_by=created_by,
    )


# ──────────────────────────────────────────────────────────────────────────
# Source A — documents
# ──────────────────────────────────────────────────────────────────────────
def ingest_document_file(path, *, created_by, source_file="") -> KnowledgeRecord:
    """Extract + passage-pack a file already saved on disk, then stage it."""
    p = Path(path)
    texts, metas = extract_file(p)
    passages = [{"page": m.get("page"), "text": t} for t, m in zip(texts, metas)]
    return _stage(
        source_type=KnowledgeRecord.Source.DOCUMENT,
        doc_id=p.name,
        passages=passages,
        created_by=created_by,
        source_file=source_file or p.name,
    )


# ──────────────────────────────────────────────────────────────────────────
# Source B — dataset rows
# ──────────────────────────────────────────────────────────────────────────
def _row_to_dict(row, columns: List[str]) -> dict:
    """Normalise a cached row (dict OR positional list) to a column->value dict."""
    if isinstance(row, dict):
        return row
    if isinstance(row, (list, tuple)):
        return {c: (row[i] if i < len(row) else None) for i, c in enumerate(columns)}
    return {}


def _pack_blocks(blocks: List[str]) -> List[dict]:
    """Pack whole text blocks into ~target-token passages WITHOUT sentence
    splitting (so values containing '.' aren't shredded). Returns
    ``[{"page": section, "text": ...}, ...]``."""
    target, hard_max, _ = _pack_settings()
    passages: List[dict] = []
    section = 1
    cur: List[str] = []
    cur_tok = 0
    for b in blocks:
        b = (b or "").strip()
        if not b:
            continue
        t = _est_tokens(b)
        if cur and cur_tok + t > hard_max:
            passages.append({"page": section, "text": "\n\n".join(cur)})
            section += 1
            cur, cur_tok = [], 0
        cur.append(b)
        cur_tok += t
        if cur_tok >= target:
            passages.append({"page": section, "text": "\n\n".join(cur)})
            section += 1
            cur, cur_tok = [], 0
    if cur:
        passages.append({"page": section, "text": "\n\n".join(cur)})
    return passages


def render_dataset_passages(dataset, *, columns=None, group_by=None,
                            max_rows=None) -> List[dict]:
    """Render dataset rows into readable, packed passages."""
    cols = list(dataset.cached_columns or [])
    rows = list(dataset.cached_rows or [])
    if max_rows:
        rows = rows[:max_rows]
    use_cols = [c for c in (columns or cols) if c in cols] or cols
    name = dataset.name

    blocks: List[str] = []
    if group_by and group_by in cols:
        groups: dict = {}
        order: List = []
        for row in rows:
            d = _row_to_dict(row, cols)
            key = d.get(group_by)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(d)
        for key in order:
            lines = [f"{group_by} {key} — {name}"]
            for d in groups[key]:
                parts = [
                    f"{c}: {d.get(c)}"
                    for c in use_cols
                    if c != group_by and d.get(c) not in (None, "")
                ]
                if parts:
                    lines.append("- " + " | ".join(parts))
            blocks.append("\n".join(lines))
    else:
        for i, row in enumerate(rows, start=1):
            d = _row_to_dict(row, cols)
            lines = [f"Record {i} — {name}"]
            for c in use_cols:
                v = d.get(c)
                if v not in (None, ""):
                    lines.append(f"{c}: {v}")
            if len(lines) > 1:
                blocks.append("\n".join(lines))

    return _pack_blocks(blocks)


def ingest_dataset(dataset, *, created_by, columns=None, group_by=None,
                   max_rows=None) -> KnowledgeRecord:
    passages = render_dataset_passages(
        dataset, columns=columns, group_by=group_by, max_rows=max_rows,
    )
    return _stage(
        source_type=KnowledgeRecord.Source.DATASET,
        doc_id=f"dataset:{dataset.name}#{dataset.id}",
        passages=passages,
        created_by=created_by,
        dataset=dataset,
        ingest_options={"columns": columns, "group_by": group_by, "max_rows": max_rows},
    )


# ──────────────────────────────────────────────────────────────────────────
# Review workflow
# ──────────────────────────────────────────────────────────────────────────
def approve_record(rec: KnowledgeRecord, *, reviewed_by, reindex=True) -> KnowledgeRecord:
    if rec.is_duplicate:
        raise ValueError("Cannot approve a duplicate record.")
    if rec.validation.get("errors"):
        raise ValueError("Record has validation errors and cannot be approved.")
    rec.status = KnowledgeRecord.Status.APPROVED
    rec.reviewed_by = reviewed_by
    rec.reviewed_at = timezone.now()
    rec.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    if reindex:
        from . import index_store
        index_store.rebuild_index()
    return rec


def reject_record(rec: KnowledgeRecord, *, reviewed_by, reason="") -> KnowledgeRecord:
    was_approved = rec.status == KnowledgeRecord.Status.APPROVED
    rec.status = KnowledgeRecord.Status.REJECTED
    rec.reviewed_by = reviewed_by
    rec.reviewed_at = timezone.now()
    rec.reject_reason = reason or ""
    rec.save(update_fields=[
        "status", "reviewed_by", "reviewed_at", "reject_reason", "updated_at",
    ])
    if was_approved:
        from . import index_store
        index_store.rebuild_index()
    return rec


def approved_record_chunks() -> Tuple[List[str], List[dict]]:
    """``(texts, metas)`` for every approved record, in stable order — the
    knowledge-base contribution that ``index_store`` merges into the index.
    Passages of one record stay contiguous so neighbour-expansion works."""
    texts: List[str] = []
    metas: List[dict] = []
    for rec in (KnowledgeRecord.objects
                .filter(status=KnowledgeRecord.Status.APPROVED)
                .order_by("id")):
        for p in rec.passages:
            t = (p.get("text") or "").strip()
            if not t:
                continue
            texts.append(t)
            metas.append({"source": rec.doc_id, "page": p.get("page")})
    return texts, metas
