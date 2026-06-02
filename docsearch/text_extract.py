"""
Document text extraction for the AI Document Search feature.

Ported and extended from the LW-Chatbot ``upload_processor.py``. Walks a corpus
directory and turns every supported file into sentence-level *chunks* with
provenance metadata, ready for the BM25 / semantic index in ``retrieval.py``.

Supported types (extensions, case-insensitive):
    .docx           Word documents  (python-docx)  -- the real LIMS corpus
    .pptx           PowerPoint       (python-pptx)
    .pdf            PDF              (pypdf)
    .txt .md        Plain text
    .png .jpg .jpeg Images, OCR'd    (easyocr -- optional, off by default)

Every extractor is wrapped so a single unreadable file (e.g. a git-LFS pointer
stub left un-hydrated) is skipped with a warning rather than aborting the build.

Public API
----------
    extract_corpus(corpus_dir) -> (chunk_texts: list[str], chunk_metas: list[dict])

``chunk_metas`` entries are ``{"source": filename, "page": int|str, "sentence": str}``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

SUPPORTED_EXTS = {".docx", ".pptx", ".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg"}
TEXT_EXTS = {".txt", ".md"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_MIN_SENT_CHARS = 5


def _split_sentences(text: str) -> List[str]:
    """Split text into sentence-ish chunks, dropping very short fragments."""
    if not text:
        return []
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s and len(s.strip()) >= _MIN_SENT_CHARS]


# ──────────────────────────────────────────────────────────────────────────
# DOCX  (the real LIMS knowledge corpus)
# ──────────────────────────────────────────────────────────────────────────
def _iter_docx_blocks(document):
    """Yield paragraphs and tables in document order (python-docx has no
    built-in body iterator that interleaves the two)."""
    from docx.document import Document as _Doc
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body if isinstance(document, _Doc) else document._tc
    for child in body.iterchildren():
        tag = child.tag
        if tag.endswith("}p"):
            yield Paragraph(child, document)
        elif tag.endswith("}tbl"):
            yield Table(child, document)


def _extract_text_from_docx(path: Path) -> Tuple[List[str], List[Dict]]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    texts: List[str] = []
    metas: List[Dict] = []
    section = 1  # bumps at each heading, so "page" roughly maps to a section

    for block in _iter_docx_blocks(document):
        if isinstance(block, Paragraph):
            style = (block.style.name or "") if block.style else ""
            if style.startswith("Heading") or style == "Title":
                section += 1
            block_text = (block.text or "").strip()
        elif isinstance(block, Table):
            rows = []
            for row in block.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    rows.append(" | ".join(dict.fromkeys(cells)))  # de-dup merged cells
            block_text = "\n".join(rows)
        else:
            continue

        if not block_text:
            continue
        for sent in _split_sentences(block_text):
            texts.append(sent)
            metas.append({"source": path.name, "page": section, "sentence": sent})
    return texts, metas


# ──────────────────────────────────────────────────────────────────────────
# PPTX
# ──────────────────────────────────────────────────────────────────────────
def _extract_text_from_pptx(path: Path) -> Tuple[List[str], List[Dict]]:
    from pptx import Presentation

    pres = Presentation(str(path))
    texts: List[str] = []
    metas: List[Dict] = []
    for slide_idx, slide in enumerate(pres.slides, start=1):
        parts = []
        for shape in slide.shapes:
            try:
                if getattr(shape, "has_text_frame", False) and shape.text_frame.text:
                    parts.append(shape.text_frame.text.strip())
                elif getattr(shape, "text", None):
                    parts.append(str(shape.text).strip())
            except Exception:
                continue
        slide_text = "\n".join(p for p in parts if p).strip()
        if not slide_text:
            continue
        for sent in _split_sentences(slide_text):
            texts.append(sent)
            metas.append({"source": path.name, "page": slide_idx, "sentence": sent})
    return texts, metas


# ──────────────────────────────────────────────────────────────────────────
# PDF
# ──────────────────────────────────────────────────────────────────────────
def _extract_text_from_pdf(path: Path) -> Tuple[List[str], List[Dict]]:
    from pypdf import PdfReader

    texts: List[str] = []
    metas: List[Dict] = []
    reader = PdfReader(str(path))
    for i, page in enumerate(reader.pages, start=1):
        for sent in _split_sentences(page.extract_text() or ""):
            texts.append(sent)
            metas.append({"source": path.name, "page": i, "sentence": sent})
    return texts, metas


# ──────────────────────────────────────────────────────────────────────────
# Plain text
# ──────────────────────────────────────────────────────────────────────────
def _extract_text_from_txt(path: Path) -> Tuple[List[str], List[Dict]]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    texts, metas = [], []
    for i, block in enumerate(re.split(r"\n\s*\n", raw), start=1):  # paragraphs
        for sent in _split_sentences(block.replace("\n", " ")):
            texts.append(sent)
            metas.append({"source": path.name, "page": i, "sentence": sent})
    return texts, metas


# ──────────────────────────────────────────────────────────────────────────
# Images (OCR -- optional, gated by settings.DOCSEARCH_ENABLE_OCR)
# ──────────────────────────────────────────────────────────────────────────
_ocr_reader = None


def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr  # heavy + downloads detector weights; imported lazily
        _ocr_reader = easyocr.Reader(["en"])
    return _ocr_reader


def _extract_text_from_image(path: Path) -> Tuple[List[str], List[Dict]]:
    try:
        from django.conf import settings
        if not getattr(settings, "DOCSEARCH_ENABLE_OCR", False):
            return [], []
    except Exception:
        return [], []
    try:
        lines = _get_ocr_reader().readtext(str(path), detail=0)
    except Exception as exc:  # easyocr missing or failed
        print(f"⚠️  OCR skipped for {path.name}: {exc}")
        return [], []
    ocr_text = "\n".join(l for l in lines if isinstance(l, str)).strip()
    texts, metas = [], []
    for sent in _split_sentences(ocr_text):
        texts.append(sent)
        metas.append({"source": path.name, "page": "image", "sentence": sent})
    return texts, metas


_EXTRACTORS = {
    ".docx": _extract_text_from_docx,
    ".pptx": _extract_text_from_pptx,
    ".pdf": _extract_text_from_pdf,
}


def extract_file(path: Path) -> Tuple[List[str], List[Dict]]:
    """Extract chunks from a single file. Returns ([], []) for unsupported or
    unreadable files (never raises)."""
    ext = path.suffix.lower()
    try:
        if ext in _EXTRACTORS:
            return _EXTRACTORS[ext](path)
        if ext in TEXT_EXTS:
            return _extract_text_from_txt(path)
        if ext in IMAGE_EXTS:
            return _extract_text_from_image(path)
    except Exception as exc:
        print(f"⚠️  Extraction failed for {path.name}: {exc}")
    return [], []


def extract_corpus(corpus_dir) -> Tuple[List[str], List[Dict]]:
    """Walk ``corpus_dir`` and extract chunks from every supported file."""
    corpus = Path(corpus_dir)
    corpus.mkdir(parents=True, exist_ok=True)
    chunk_texts: List[str] = []
    chunk_metas: List[Dict] = []
    for path in sorted(corpus.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        texts, metas = extract_file(path)
        chunk_texts.extend(texts)
        chunk_metas.extend(metas)
    print(f"✓ extract_corpus: {len(chunk_texts)} chunks from {corpus}")
    return chunk_texts, chunk_metas
