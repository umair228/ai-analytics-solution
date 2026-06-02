"""
Document corpus management for AI Document Search.

    GET    /api/doc-search/documents/          -> list indexed documents + status
    POST   /api/doc-search/documents/          -> upload (multipart "files"); reindex
    DELETE /api/doc-search/documents/<name>/   -> delete a document; reindex
"""
import os
from pathlib import Path

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import index_store
from ..text_extract import SUPPORTED_EXTS


def _payload():
    # Spread status first so the documents LIST wins over status()'s document
    # COUNT (both use the "documents" key).
    return {**index_store.status(), "documents": index_store.list_corpus_files()}


def _unique_dest(corpus: Path, name: str) -> Path:
    """Avoid silently overwriting an existing corpus document: uniquify on collision."""
    dest = corpus / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(1, 1000):
        candidate = corpus / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return corpus / f"{stem} ({os.getpid()}){suffix}"


class DocumentsView(APIView):
    def get(self, request):
        return Response(_payload(), status=status.HTTP_200_OK)

    def post(self, request):
        files = request.FILES.getlist("files") or request.FILES.getlist("documents")
        if not files:
            return Response({"error": "No files uploaded (field 'files')."},
                            status=status.HTTP_400_BAD_REQUEST)

        max_files = getattr(settings, "DOCSEARCH_MAX_UPLOAD_FILES", 20)
        max_bytes = getattr(settings, "DOCSEARCH_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
        if len(files) > max_files:
            return Response({"error": f"Too many files in one upload (max {max_files})."},
                            status=status.HTTP_400_BAD_REQUEST)

        corpus = index_store.corpus_dir()
        saved, skipped = [], []
        for f in files:
            name = os.path.basename(f.name or "")
            if not name or Path(name).suffix.lower() not in SUPPORTED_EXTS:
                skipped.append(name or "(unnamed)")
                continue
            if getattr(f, "size", 0) and f.size > max_bytes:
                skipped.append(f"{name} (too large)")
                continue
            dest = _unique_dest(corpus, name)
            written, overflow = 0, False
            try:
                with open(dest, "wb") as out:
                    for chunk in f.chunks():
                        written += len(chunk)
                        if written > max_bytes:  # guard against missing/spoofed size
                            overflow = True
                            break
                        out.write(chunk)
                if overflow:
                    dest.unlink(missing_ok=True)
                    skipped.append(f"{name} (too large)")
                else:
                    saved.append(dest.name)
            except Exception as exc:
                print(f"[doc-search] save failed for {name}: {exc}")
                dest.unlink(missing_ok=True)
                skipped.append(name)

        if saved:
            index_store.rebuild_index()

        out = _payload()
        out.update({"saved": saved, "skipped": skipped,
                    "message": f"Uploaded {len(saved)} file(s); index rebuilt." if saved
                               else "No files were saved."})
        code = status.HTTP_201_CREATED if saved else status.HTTP_400_BAD_REQUEST
        return Response(out, status=code)


class DocumentDeleteView(APIView):
    def delete(self, request, name):
        safe = os.path.basename(name or "")
        target = index_store.corpus_dir() / safe
        if not safe or not target.exists() or not target.is_file():
            return Response({"error": f"Document not found: {safe}"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            target.unlink()
        except Exception as exc:
            print(f"[doc-search] delete failed for {safe}: {exc}")
            return Response({"error": "Could not delete the document."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        index_store.rebuild_index()
        out = _payload()
        out["message"] = f"Deleted {safe}; index rebuilt."
        return Response(out, status=status.HTTP_200_OK)
