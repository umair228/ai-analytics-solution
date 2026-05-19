from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove, IsOwnerOrSharedReadOnly

from . import introspection
from .constants import SOURCE_TYPE_CATALOG
from .materialize import materialize_file
from .models import DataSource
from .serializers import DataSourceSerializer


def _error(exc, code=status.HTTP_400_BAD_REQUEST):
    return Response({"error": True, "detail": str(exc)}, status=code)


@api_view(["GET"])
def source_types(request):
    """Catalog of supported source types — drives the connection form."""
    return Response({"source_types": SOURCE_TYPE_CATALOG})


class DataSourceViewSet(viewsets.ModelViewSet):
    """CRUD + connection-testing + schema introspection for data sources."""

    serializer_class = DataSourceSerializer
    permission_classes = [IsAnalystOrAbove, IsOwnerOrSharedReadOnly]

    def get_queryset(self):
        user = self.request.user
        qs = DataSource.objects.select_related("owner", "site").prefetch_related("shared_with")
        if user.is_admin:
            return qs
        return qs.filter(Q(owner=user) | Q(shared_with=user)).distinct()

    def perform_create(self, serializer):
        ds = serializer.save()
        record_audit(self.request, AuditLog.Action.CREATE, target_type="DataSource",
                     target_id=ds.id, summary=f"Created data source '{ds.name}'")

    def perform_update(self, serializer):
        ds = serializer.save()
        record_audit(self.request, AuditLog.Action.UPDATE, target_type="DataSource",
                     target_id=ds.id, summary=f"Updated data source '{ds.name}'")

    def perform_destroy(self, instance):
        record_audit(self.request, AuditLog.Action.DELETE, target_type="DataSource",
                     target_id=instance.id, summary=f"Deleted data source '{instance.name}'")
        instance.delete()

    # ---- connection testing -------------------------------------------------
    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        ds = self.get_object()
        ok, error = introspection.test_connection(ds)
        ds.test_status = DataSource.TestStatus.OK if ok else DataSource.TestStatus.FAILED
        ds.last_tested_at = timezone.now()
        ds.last_test_error = error or ""
        ds.save(update_fields=["test_status", "last_tested_at", "last_test_error"])
        record_audit(request, AuditLog.Action.TEST, target_type="DataSource", target_id=ds.id,
                     summary=f"Tested '{ds.name}': {'ok' if ok else 'failed'}")
        return Response({"success": ok, "error": error, "test_status": ds.test_status})

    # ---- schema introspection ----------------------------------------------
    @action(detail=True, methods=["get"])
    def databases(self, request, pk=None):
        ds = self.get_object()
        try:
            return Response({"databases": introspection.list_databases(ds)})
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @action(detail=True, methods=["get"])
    def schemas(self, request, pk=None):
        ds = self.get_object()
        try:
            schemas = introspection.list_schemas(ds, request.query_params.get("database"))
            return Response({"schemas": schemas})
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @action(detail=True, methods=["get"])
    def tables(self, request, pk=None):
        ds = self.get_object()
        try:
            tables = introspection.list_tables(
                ds,
                schema=request.query_params.get("schema"),
                database=request.query_params.get("database"),
            )
            record_audit(request, AuditLog.Action.INTROSPECT, target_type="DataSource",
                         target_id=ds.id, summary=f"Listed tables of '{ds.name}'")
            return Response({"tables": tables})
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @action(detail=True, methods=["get"])
    def columns(self, request, pk=None):
        ds = self.get_object()
        table = request.query_params.get("table")
        if not table:
            return _error("Query parameter 'table' is required.")
        try:
            columns = introspection.list_columns(
                ds, table,
                schema=request.query_params.get("schema"),
                database=request.query_params.get("database"),
            )
            return Response({"columns": columns})
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @action(detail=True, methods=["get"], url_path="foreign-keys")
    def foreign_keys(self, request, pk=None):
        ds = self.get_object()
        table = request.query_params.get("table")
        if not table:
            return _error("Query parameter 'table' is required.")
        try:
            fks = introspection.get_foreign_keys(
                ds, table,
                schema=request.query_params.get("schema"),
                database=request.query_params.get("database"),
            )
            return Response({"foreign_keys": fks})
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        ds = self.get_object()
        table = request.query_params.get("table")
        if not table:
            return _error("Query parameter 'table' is required.")
        try:
            limit = min(int(request.query_params.get("limit", 100)), 500)
        except (TypeError, ValueError):
            limit = 100
        try:
            data = introspection.preview_table(
                ds, table,
                schema=request.query_params.get("schema"),
                database=request.query_params.get("database"),
                limit=limit,
            )
            return Response(data)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    # ---- file upload (Excel / CSV sources) ---------------------------------
    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def upload(self, request, pk=None):
        ds = self.get_object()
        if not ds.is_file_source:
            return _error("This data source is not a file source.")
        upload = request.FILES.get("file")
        if not upload:
            return _error("No file uploaded — send it as the 'file' field.")

        upload_dir = settings.MEDIA_ROOT / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        raw_path = upload_dir / f"ds_{ds.id}_{upload.name}"
        with open(raw_path, "wb") as out:
            for chunk in upload.chunks():
                out.write(chunk)

        try:
            materialized, tables = materialize_file(ds, raw_path)
        except Exception as exc:  # noqa: BLE001
            return _error(f"Could not read the file: {exc}")

        ds.options = {
            **(ds.options or {}),
            "materialized_path": materialized,
            "original_filename": upload.name,
            "tables": tables,
        }
        ds.test_status = DataSource.TestStatus.OK
        ds.last_tested_at = timezone.now()
        ds.last_test_error = ""
        ds.save()
        record_audit(request, AuditLog.Action.UPDATE, target_type="DataSource",
                     target_id=ds.id, summary=f"Uploaded file to '{ds.name}'")
        return Response({"success": True, "tables": tables, "filename": upload.name})
