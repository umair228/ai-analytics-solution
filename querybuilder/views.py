import time

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from connections.models import DataSource
from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove, IsOwnerOrSharedReadOnly

from .compiler import CompileError, compile_spec
from .executor import EXPLORE_MAX_PAGE, QueryError, execute_raw_sql, execute_spec
from .models import QueryDefinition, QueryRun
from .serializers import QueryDefinitionSerializer
from .spec import SpecError


def _clamp_max_rows(value):
    """Optional per-request row cap, bounded so a preview can't melt the browser."""
    try:
        return max(100, min(int(value), EXPLORE_MAX_PAGE)) if value else None
    except (TypeError, ValueError):
        return None


class QueryRunSerializer(serializers.ModelSerializer):
    query_name = serializers.CharField(source="query.name", read_only=True, default=None)
    datasource_name = serializers.CharField(source="datasource.name", read_only=True, default=None)

    class Meta:
        model = QueryRun
        fields = [
            "id", "query", "query_name", "datasource", "datasource_name",
            "sql", "row_count", "duration_ms", "error", "created_at",
        ]


def _err(detail, code=status.HTTP_400_BAD_REQUEST):
    return Response({"error": True, "detail": str(detail)}, status=code)


class QueryDefinitionViewSet(viewsets.ModelViewSet):
    """CRUD for saved queries plus ad-hoc compile / execute / run actions."""

    serializer_class = QueryDefinitionSerializer
    permission_classes = [IsAnalystOrAbove, IsOwnerOrSharedReadOnly]

    def get_queryset(self):
        user = self.request.user
        qs = QueryDefinition.objects.select_related("datasource", "owner")
        if user.is_admin:
            return qs
        return qs.filter(Q(owner=user) | Q(shared_with=user)).distinct()

    def get_permissions(self):
        # viewing or running a saved query is allowed for viewers too;
        # the queryset already restricts which queries are visible.
        if self.action in ("list", "retrieve", "run"):
            return [IsAuthenticated()]
        return [perm() for perm in self.permission_classes]

    def _get_datasource(self, datasource_id):
        """Return a data source the current user may use, or None."""
        try:
            ds = DataSource.objects.get(pk=datasource_id)
        except (DataSource.DoesNotExist, ValueError, TypeError):
            return None
        return ds if ds.accessible_by(self.request.user) else None

    def perform_create(self, serializer):
        try:
            query = serializer.save()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("QueryDefinition save failed")
            raise serializers.ValidationError({"detail": str(exc)}) from exc
        record_audit(self.request, AuditLog.Action.CREATE, target_type="QueryDefinition",
                     target_id=query.id, summary=f"Saved query '{query.name}'")

    def perform_update(self, serializer):
        query = serializer.save()
        record_audit(self.request, AuditLog.Action.UPDATE, target_type="QueryDefinition",
                     target_id=query.id, summary=f"Updated query '{query.name}'")

    # ---- ad-hoc: compile a spec to SQL (no save, no execution) -------------
    @action(detail=False, methods=["post"])
    def compile(self, request):
        ds = self._get_datasource(request.data.get("datasource"))
        if ds is None:
            return _err("Data source not found or access denied.", status.HTTP_404_NOT_FOUND)
        try:
            compiled = compile_spec(
                ds, request.data.get("spec") or {}, request.data.get("database") or None
            )
        except (SpecError, CompileError) as exc:
            return _err(exc)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
        return Response({"sql": compiled.sql, "dialect": compiled.dialect})

    # ---- ad-hoc: execute a spec or raw SQL (no save) -----------------------
    @action(detail=False, methods=["post"])
    def execute(self, request):
        """Run a query and return one page of rows.

        Pages the FULL result server-side so the whole table is reachable
        (nothing is silently dropped): ``offset`` selects the window,
        ``max_rows`` its size, and ``total_row_count`` (computed on the first
        page) reports how many rows match in all. ``columns`` (echoed back by
        the client on page 2+) lets the paginated wrapper handle queries whose
        output has duplicate column names.
        """
        from querybuilder.executor import execute_dataset_query

        ds = self._get_datasource(request.data.get("datasource"))
        if ds is None:
            return _err("Data source not found or access denied.", status.HTTP_404_NOT_FOUND)
        database = request.data.get("database") or None
        mode = request.data.get("mode", "builder")
        raw_sql = request.data.get("raw_sql", "")
        # Declared parameter defaults merge UNDER explicit values so a
        # parameterized query (":lab_id = '' OR …") runs without input.
        declared = {p.get("name"): p.get("default")
                    for p in (request.data.get("parameters") or [])
                    if isinstance(p, dict) and p.get("name")
                    and p.get("default") is not None}
        params = {**declared, **(request.data.get("params") or {})}
        # Ad-hoc runs are forgiving: any :param written in the SQL but left
        # unbound defaults to '' so the query runs (the recommended
        # ":p = '' OR col = :p" pattern then simply applies no filter) instead
        # of crashing with "a value is required for bind parameter".
        if mode == "raw":
            from querybuilder.executor import sql_bind_params
            for name in sql_bind_params(raw_sql):
                params.setdefault(name, "")
        params = params or None
        page_size = _clamp_max_rows(request.data.get("max_rows")) \
            or settings.DSE_QUERY_MAX_ROWS
        offset = max(0, int(request.data.get("offset") or 0))
        columns = request.data.get("columns") or None
        want_total = bool(request.data.get("count_total", offset == 0))
        t0 = time.monotonic()
        try:
            if mode == "raw" and offset > 0 and columns:
                # A later page — slice via the dup-safe OFFSET/FETCH wrapper.
                # Skip the COUNT (the client already has the total from page 1).
                result = execute_dataset_query(
                    ds, raw_sql, database, params=params, columns=columns,
                    spec={"mode": "rows", "offset": offset, "limit": page_size,
                          "with_count": want_total})
            elif mode == "raw":
                result = execute_raw_sql(ds, raw_sql, database,
                                         params=params, max_rows=page_size)
            else:
                result = execute_spec(ds, request.data.get("spec") or {}, database,
                                      max_rows=page_size)
        except (SpecError, CompileError, QueryError) as exc:
            QueryRun.objects.create(
                user=request.user, datasource=ds, sql=raw_sql,
                duration_ms=int((time.monotonic() - t0) * 1000), error=str(exc),
            )
            return _err(exc)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

        # True total (once, on the first page) so the UI can show "of N".
        if want_total and result.get("total_row_count") is None:
            total = result["row_count"]
            if result.get("truncated") and mode == "raw":
                try:
                    total = execute_dataset_query(
                        ds, raw_sql, database, params=params,
                        columns=result["columns"], spec={"mode": "count"},
                    )["total_row_count"]
                except Exception:  # noqa: BLE001 — count is best-effort
                    total = None
            result["total_row_count"] = total
        result.setdefault("offset", offset)
        result.setdefault("limit", page_size)

        QueryRun.objects.create(
            user=request.user, datasource=ds, sql=result.get("sql", ""),
            row_count=result["row_count"],
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        record_audit(request, AuditLog.Action.QUERY, target_type="DataSource",
                     target_id=ds.id, summary=f"Ad-hoc query on '{ds.name}'",
                     detail={"rows": result["row_count"]})
        return Response(result)

    # ---- export the FULL result as CSV (every row, no preview cap) ---------
    @action(detail=False, methods=["post"], url_path="export")
    def export(self, request):
        """Stream a query's complete result as CSV — all rows, however many.

        Rows are pulled from a server-side cursor and flushed in chunks, so a
        million-row export stays memory-safe on the API and downloads
        progressively in the browser instead of building a giant JSON payload.
        """
        import csv
        import io

        from django.http import StreamingHttpResponse
        from sqlalchemy import text as sa_text

        from connections.engine import get_engine

        from .executor import assert_read_only

        ds = self._get_datasource(request.data.get("datasource"))
        if ds is None:
            return _err("Data source not found or access denied.", status.HTTP_404_NOT_FOUND)
        database = request.data.get("database") or None
        mode = request.data.get("mode", "builder")
        declared = {p.get("name"): p.get("default")
                    for p in (request.data.get("parameters") or [])
                    if isinstance(p, dict) and p.get("name")
                    and p.get("default") is not None}
        params = {**declared, **(request.data.get("params") or {})}
        try:
            if mode == "raw":
                sql = request.data.get("raw_sql", "")
                assert_read_only(sql)
                stmt = sa_text(sql)
                from .executor import sql_bind_params
                for name in sql_bind_params(sql):
                    params.setdefault(name, "")
            else:
                # unlimited: an export must not be truncated by the preview cap
                # (or the spec's own LIMIT) — that would silently lose rows.
                compiled = compile_spec(ds, request.data.get("spec") or {},
                                        database, unlimited=True)
                stmt = compiled.statement
                params = {}
        except (SpecError, CompileError, QueryError) as exc:
            return _err(exc)

        # Execute BEFORE streaming: a bad query / missing bind / unreachable DB
        # must be a real 400, not a 200 whose CSV body is an error marker.
        engine = get_engine(ds, database)
        try:
            conn = engine.connect()
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
        try:
            result = conn.execution_options(stream_results=True).execute(stmt, params)
            header = [str(c) for c in result.keys()]
        except Exception as exc:  # noqa: BLE001
            conn.close()
            return _err(exc)

        def csv_cell(v):
            if v is None:
                return ""
            # Neutralize CSV formula injection (=cmd, +cmd, @cmd) without
            # corrupting negative numbers, which arrive as real numerics.
            if isinstance(v, str) and v[:1] in ("=", "+", "@", "\t"):
                return "'" + v
            return v

        def stream(connection, res):
            buf = io.StringIO()
            writer = csv.writer(buf)

            def flush():
                chunk = buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
                return chunk

            try:
                writer.writerow(header)
                yield flush()
                n = 0
                for row in res:
                    writer.writerow([csv_cell(v) for v in row])
                    n += 1
                    if n % 2000 == 0:
                        yield flush()
                tail = flush()
                if tail:
                    yield tail
            except Exception as exc:  # noqa: BLE001 — headers already sent
                yield f"\n# EXPORT FAILED: {exc}\n"
            finally:
                connection.close()

        record_audit(request, AuditLog.Action.QUERY, target_type="DataSource",
                     target_id=ds.id, summary=f"CSV export from '{ds.name}'")
        resp = StreamingHttpResponse(stream(conn, result),
                                     content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = 'attachment; filename="query-export.csv"'
        return resp

    # ---- run a saved query --------------------------------------------------
    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        query = self.get_object()
        ds = query.datasource
        if not ds.accessible_by(request.user):
            return _err(
                "You do not have access to this query's data source.",
                status.HTTP_403_FORBIDDEN,
            )
        params = {**query.declared_param_defaults(),
                  **(request.data.get("params") or {})} or None
        max_rows = _clamp_max_rows(request.data.get("max_rows"))
        t0 = time.monotonic()
        try:
            if query.mode == QueryDefinition.Mode.RAW:
                result = execute_raw_sql(ds, query.raw_sql, query.database or None,
                                         params=params, max_rows=max_rows)
            else:
                result = execute_spec(ds, query.spec, query.database or None,
                                      max_rows=max_rows)
        except (SpecError, CompileError, QueryError) as exc:
            QueryRun.objects.create(
                query=query, user=request.user, datasource=ds,
                sql=query.raw_sql or query.generated_sql,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc),
            )
            return _err(exc)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
        dur = int((time.monotonic() - t0) * 1000)
        QueryRun.objects.create(
            query=query, user=request.user, datasource=ds,
            sql=result.get("sql", ""),
            row_count=result["row_count"],
            duration_ms=dur,
        )
        query.generated_sql = result.get("sql", "")
        query.last_run_at = timezone.now()
        query.last_row_count = result["row_count"]
        query.save(update_fields=["generated_sql", "last_run_at", "last_row_count"])
        record_audit(request, AuditLog.Action.QUERY, target_type="QueryDefinition",
                     target_id=query.id, summary=f"Ran query '{query.name}'",
                     detail={"rows": result["row_count"]})
        return Response(result)

    # ---- run history --------------------------------------------------------
    @action(detail=False, methods=["get"], url_path="history")
    def history(self, request):
        """Recent query runs for the current user (last 50)."""
        runs = QueryRun.objects.filter(user=request.user).select_related(
            "query", "datasource"
        )[:50]
        return Response(QueryRunSerializer(runs, many=True).data)
