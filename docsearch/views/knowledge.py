"""
Knowledge-base staging API for AI Document Search.

    GET    /api/doc-search/knowledge/                 -> list staged records (?status=&source_type=)
    POST   /api/doc-search/knowledge/documents/       -> ingest uploaded files (multipart "files")
    POST   /api/doc-search/knowledge/datasets/        -> ingest dataset rows ({dataset, columns?, group_by?, max_rows?})
    GET    /api/doc-search/knowledge/<id>/            -> record detail (incl. passages preview)
    DELETE /api/doc-search/knowledge/<id>/            -> delete a staged record (reindex if it was approved)
    POST   /api/doc-search/knowledge/<id>/approve/    -> approve -> reindex
    POST   /api/doc-search/knowledge/<id>/reject/     -> reject ({reason})
    GET    /api/doc-search/knowledge/sources/         -> what is currently indexed (incl. dataset virtual docs)

Ingest endpoints stage content for review; nothing becomes searchable until a
record is approved. Uploaded files land in a STAGING dir (not the corpus dir) so
they are not auto-indexed before approval. Write actions require the analyst/
builder role (IsAnalystOrAbove); reads are open to any authenticated user.
"""
import os
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError
from rest_framework import generics
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAnalystOrAbove
from datasets.models import Dataset

from .. import index_store, ingest
from ..models import KnowledgeRecord
from ..serializers import KnowledgeRecordDetailSerializer, KnowledgeRecordSerializer
from ..text_extract import SUPPORTED_EXTS
from .documents import _unique_dest


def _staging_dir() -> Path:
    p = Path(getattr(
        settings, "DOCSEARCH_STAGING_DIR",
        str(Path(settings.DOCSEARCH_CORPUS_DIR).parent / "staging"),
    ))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get(request, pk) -> KnowledgeRecord | None:
    """Fetch a record the requester may act on: admins see all; everyone else is
    scoped to their own records (created_by). Other/unowned records resolve to
    None -> 404, so no data leaks and existence is not disclosed."""
    qs = KnowledgeRecord.objects.filter(pk=pk)
    if not getattr(request.user, "is_admin", False):
        qs = qs.filter(created_by=request.user)
    return qs.first()


def _not_found():
    return Response({"error": "Knowledge record not found."}, status=http.HTTP_404_NOT_FOUND)


class KnowledgeListView(generics.ListAPIView):
    """Paginated list of staged records, filterable by status / source_type."""
    serializer_class = KnowledgeRecordSerializer
    permission_classes = [IsAnalystOrAbove]

    def get_queryset(self):
        qs = KnowledgeRecord.objects.all()
        # Scope to the requester's own records (admins see all). Records with
        # created_by=None fail closed for non-admins.
        if not getattr(self.request.user, "is_admin", False):
            qs = qs.filter(created_by=self.request.user)
        status_ = self.request.query_params.get("status")
        source_type = self.request.query_params.get("source_type")
        if status_:
            qs = qs.filter(status=status_)
        if source_type:
            qs = qs.filter(source_type=source_type)
        return qs


class KnowledgeIngestDocumentsView(APIView):
    permission_classes = [IsAnalystOrAbove]

    def post(self, request):
        files = request.FILES.getlist("files") or request.FILES.getlist("documents")
        if not files:
            return Response({"error": "No files uploaded (field 'files')."},
                            status=http.HTTP_400_BAD_REQUEST)

        max_files = getattr(settings, "DOCSEARCH_MAX_UPLOAD_FILES", 20)
        max_bytes = getattr(settings, "DOCSEARCH_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
        if len(files) > max_files:
            return Response({"error": f"Too many files in one upload (max {max_files})."},
                            status=http.HTTP_400_BAD_REQUEST)

        staging = _staging_dir()
        created, skipped = [], []
        for f in files:
            name = os.path.basename(f.name or "")
            if not name or Path(name).suffix.lower() not in SUPPORTED_EXTS:
                skipped.append(name or "(unnamed)")
                continue
            if getattr(f, "size", 0) and f.size > max_bytes:
                skipped.append(f"{name} (too large)")
                continue
            dest = _unique_dest(staging, name)
            written, overflow = 0, False
            try:
                with open(dest, "wb") as out:
                    for chunk in f.chunks():
                        written += len(chunk)
                        if written > max_bytes:
                            overflow = True
                            break
                        out.write(chunk)
                if overflow:
                    dest.unlink(missing_ok=True)
                    skipped.append(f"{name} (too large)")
                    continue
            except Exception as exc:
                print(f"[doc-search] staging save failed for {name}: {exc}")
                dest.unlink(missing_ok=True)
                skipped.append(name)
                continue
            rec = ingest.ingest_document_file(dest, created_by=request.user, source_file=name)
            created.append(rec)

        data = KnowledgeRecordSerializer(created, many=True).data
        dup_ids = [r.id for r in created if r.is_duplicate]
        return Response({
            "created": data,
            "duplicates": dup_ids,
            "skipped": skipped,
            "message": (f"Staged {len(created)} record(s) for review."
                        if created else "No files were staged."),
        }, status=http.HTTP_201_CREATED if created else http.HTTP_400_BAD_REQUEST)


class KnowledgeIngestDatasetView(APIView):
    permission_classes = [IsAnalystOrAbove]

    def post(self, request):
        dataset_id = request.data.get("dataset")
        if not dataset_id:
            return Response({"error": "Field 'dataset' (id) is required."},
                            status=http.HTTP_400_BAD_REQUEST)
        dataset = Dataset.objects.filter(pk=dataset_id).first()
        # Object-level access: a builder may ingest only a dataset they own or
        # that is shared with them (admins: any). Treat "no access" as not-found
        # so dataset existence isn't disclosed (IDOR hardening).
        if dataset is None or not dataset.accessible_by(request.user):
            return Response({"error": "Dataset not found."}, status=http.HTTP_404_NOT_FOUND)
        if not dataset.cached_rows:
            return Response({"error": "Dataset has no cached rows — refresh the dataset first."},
                            status=http.HTTP_400_BAD_REQUEST)

        columns = request.data.get("columns") or None
        group_by = request.data.get("group_by") or None
        kb_max = getattr(settings, "DOCSEARCH_KB_MAX_ROWS", 10000)
        try:
            max_rows = min(int(request.data.get("max_rows") or kb_max), kb_max)
        except (TypeError, ValueError):
            max_rows = kb_max

        rec = ingest.ingest_dataset(
            dataset, created_by=request.user,
            columns=columns, group_by=group_by, max_rows=max_rows,
        )
        return Response({
            "created": [KnowledgeRecordSerializer(rec).data],
            "duplicates": [rec.id] if rec.is_duplicate else [],
            "message": "Staged 1 record for review.",
        }, status=http.HTTP_201_CREATED)


class KnowledgeDetailView(APIView):
    permission_classes = [IsAnalystOrAbove]

    def get(self, request, pk):
        rec = _get(request, pk)
        if rec is None:
            return _not_found()
        return Response(KnowledgeRecordDetailSerializer(rec).data)

    def delete(self, request, pk):
        rec = _get(request, pk)
        if rec is None:
            return _not_found()
        was_approved = rec.status == KnowledgeRecord.Status.APPROVED
        rec.delete()
        if was_approved:
            index_store.rebuild_index()
        return Response(status=http.HTTP_204_NO_CONTENT)


class KnowledgeApproveView(APIView):
    permission_classes = [IsAnalystOrAbove]

    def post(self, request, pk):
        rec = _get(request, pk)
        if rec is None:
            return _not_found()
        try:
            ingest.approve_record(rec, reviewed_by=request.user)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        except IntegrityError:
            rec.refresh_from_db()
            return Response({"error": "Identical content is already approved and indexed."},
                            status=http.HTTP_409_CONFLICT)
        except Exception as exc:  # noqa: BLE001 — approval must not 500 on an index hiccup
            # rebuild_index is best-effort and shouldn't raise, but guard anyway:
            # if the record did flip to APPROVED, report success with a warning so
            # the reviewer isn't blocked by a transient indexing error.
            print(f"[doc-search] approve reindex error for record {pk}: {exc}")
            rec.refresh_from_db()
            if rec.status == KnowledgeRecord.Status.APPROVED:
                return Response({"id": rec.id, "status": rec.status,
                                 "warning": "Approved, but the search index could not be "
                                            "refreshed yet — it will update on the next rebuild."},
                                status=http.HTTP_200_OK)
            return Response({"error": "Could not approve this record. Please try again."},
                            status=http.HTTP_400_BAD_REQUEST)
        try:
            idx_status = index_store.status()
        except Exception as exc:  # noqa: BLE001 — never let a status read 500 a good approve
            print(f"[doc-search] approve status read failed for record {pk}: {exc}")
            idx_status = None
        return Response({"id": rec.id, "status": rec.status, "index": idx_status})


class KnowledgeRejectView(APIView):
    permission_classes = [IsAnalystOrAbove]

    def post(self, request, pk):
        rec = _get(request, pk)
        if rec is None:
            return _not_found()
        ingest.reject_record(rec, reviewed_by=request.user,
                             reason=request.data.get("reason", ""))
        return Response({"id": rec.id, "status": rec.status})


class KnowledgeSourcesView(APIView):
    permission_classes = [IsAnalystOrAbove]

    def get(self, request):
        return Response({
            "sources": index_store.list_indexed_sources(request.user),
            **index_store.status(),
        })
