"""REST API for the statistical analysis engine (Phase 1 — Statistical suite).

Two endpoints power the Statistical Analysis dashboard and the agent tool:
  * GET  /api/analytics/stats/catalog/  — the test registry + param schemas.
  * POST /api/analytics/stats/run/      — run one test on a dataset.
"""
import time

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status, viewsets

from core.audit import record_audit
from core.models import AuditLog
from datasets.models import Dataset

from django.db.models import Q

from . import anomaly, forecasting_ext, relationships, rootcause
from .engine import AnalyticsError, build_dataframe
from .models import AnalysisRun
from .serializers import AnalysisRunDetailSerializer, AnalysisRunListSerializer
from .stats_catalog import catalog, run_test


def _accessible_dataset(user, dataset_id):
    if not dataset_id:
        return None
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset and dataset.accessible_by(user):
        return dataset
    return None


# --------------------------------------------------------------------------
# Saved-run plumbing — persist each workflow run as an AnalysisRun
# --------------------------------------------------------------------------
def _auto_name(workflow, method, dataset):
    label = {
        AnalysisRun.Workflow.ANOMALY: "Anomaly",
        AnalysisRun.Workflow.STATISTICS: "Statistics",
        AnalysisRun.Workflow.FORECAST: "Forecast",
    }.get(workflow, "Analysis")
    bits = [label] + ([str(method)] if method else []) + [dataset.name]
    return " · ".join(bits)[:200]


def _extract_metrics(workflow, result):
    """A small, list-friendly projection of the full result dict."""
    if not isinstance(result, dict):
        return {}
    if workflow == AnalysisRun.Workflow.ANOMALY:
        keys = ("anomaly_count", "n", "test", "method", "scope", "contamination")
        return {k: result[k] for k in keys if k in result}
    if workflow == AnalysisRun.Workflow.FORECAST:
        keys = ("selected", "metric", "season", "periods", "direction")
        out = {k: result[k] for k in keys if k in result}
        scores, sel = result.get("scores") or {}, result.get("selected")
        if sel and isinstance(scores, dict) and isinstance(scores.get(sel), dict):
            for s in ("mae", "rmse", "mape", "smape"):
                if s in scores[sel]:
                    out[s] = scores[sel][s]
        return out
    keys = ("test", "p_value", "statistic", "significant", "is_normal",
            "r_squared", "cramers_v", "eta_squared", "cohens_d", "coefficient",
            "f_statistic", "chi2_statistic", "cpk", "capable")
    return {k: result[k] for k in keys if k in result}


def _maybe_explain(workflow, result, dataset):
    """Generate an LLM explanation, swallowing any failure (soft feature)."""
    try:
        from .explain import explain_result
        return explain_result(workflow, result, dataset=dataset)
    except Exception as exc:  # noqa: BLE001 - explanation must never break a run
        return {"configured": False, "answer": "", "error": str(exc)}


def _run_and_save(request, *, workflow, method, fn, config, summary,
                  save=True, explain=False):
    """Load+authorise the dataset, build the df, run ``fn(df)``, persist an
    ``AnalysisRun`` (unless ``save`` is False), audit, and return.

    Preserves the historic ``{dataset, result}`` response shape and *adds*
    ``run_id``/``metrics``/``explanation`` when available. Turns
    ``AnalyticsError`` into a clean 400 and records the failure on the run.
    """
    dataset = _accessible_dataset(request.user, request.data.get("dataset"))
    if dataset is None:
        return Response({"error": True, "detail": "Dataset not found or access denied."},
                        status=status.HTTP_404_NOT_FOUND)

    run = None
    if save:
        run = AnalysisRun(
            owner=request.user, dataset=dataset, workflow=workflow,
            method=method or "", config=config or {},
            status=AnalysisRun.Status.RUNNING,
            name=(request.data.get("name") or "").strip(),
        )

    t0 = time.perf_counter()
    try:
        df = build_dataframe(dataset)
        result = fn(df)
    except (AnalyticsError, Exception) as exc:  # noqa: BLE001
        detail = str(exc) if isinstance(exc, AnalyticsError) else f"Analysis failed: {exc}"
        if run is not None:
            run.status = AnalysisRun.Status.FAILED
            run.error = detail
            run.duration_ms = int((time.perf_counter() - t0) * 1000)
            run.name = run.name or _auto_name(workflow, method, dataset)
            run.save()
        return Response({"error": True, "detail": detail},
                        status=status.HTTP_400_BAD_REQUEST)

    duration_ms = int((time.perf_counter() - t0) * 1000)
    metrics = _extract_metrics(workflow, result)
    payload = {"dataset": dataset.id, "result": result}
    if run is not None:
        run.result = result
        run.metrics = metrics
        run.status = AnalysisRun.Status.SUCCESS
        run.duration_ms = duration_ms
        run.name = run.name or _auto_name(workflow, method, dataset)
        run.save()
        payload["run_id"] = run.id
        payload["metrics"] = metrics

    record_audit(request, AuditLog.Action.QUERY, target_type="AnalysisRun",
                 target_id=getattr(run, "id", "") or "",
                 summary=f"{summary} on '{dataset.name}'")

    if explain:
        payload["explanation"] = _maybe_explain(workflow, result, dataset)
    return Response(payload)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def stats_catalog(request):
    """The available statistical tests and their parameter schemas."""
    return Response({"tests": catalog()})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stats_run(request):
    """Run a statistical test on a dataset.

    Body: ``{"dataset": <id>, "test": "<name>", "params": {...},
             "save": true, "explain": false}``.
    """
    test = (request.data.get("test") or "").strip()
    params = request.data.get("params") or {}
    if not isinstance(params, dict):
        return Response({"error": True, "detail": "'params' must be an object."},
                        status=status.HTTP_400_BAD_REQUEST)
    return _run_and_save(
        request, workflow=AnalysisRun.Workflow.STATISTICS, method=test,
        fn=lambda df: run_test(test, df, params),
        config={"test": test, "params": params},
        summary=f"Stat test '{test}'",
        save=request.data.get("save", True),
        explain=bool(request.data.get("explain", False)),
    )


# --------------------------------------------------------------------------
# Phase 3 — anomaly detection & root-cause analysis
# --------------------------------------------------------------------------
def _run_analysis(request, fn, summary, **kwargs):
    """Shared plumbing: load+authorise the dataset, build the df, run ``fn``,
    audit, and return — turning AnalyticsError into a clean 400."""
    dataset = _accessible_dataset(request.user, request.data.get("dataset"))
    if dataset is None:
        return Response({"error": True, "detail": "Dataset not found or access denied."},
                        status=status.HTTP_404_NOT_FOUND)
    try:
        df = build_dataframe(dataset)
        result = fn(df, **kwargs)
    except AnalyticsError as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"Analysis failed: {exc}"},
                        status=status.HTTP_400_BAD_REQUEST)
    record_audit(request, AuditLog.Action.QUERY, target_type="Dataset",
                 target_id=dataset.id, summary=f"{summary} on '{dataset.name}'")
    return Response({"dataset": dataset.id, "result": result})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def anomaly_detect(request):
    """Anomaly detection — univariate (z/IQR/MAD), seasonal (STL) or
    multivariate (IsolationForest / LOF / EllipticEnvelope / autoencoder / auto)."""
    d = request.data
    scope = d.get("scope", "multivariate")
    method = d.get("method", "auto")
    return _run_and_save(
        request, workflow=AnalysisRun.Workflow.ANOMALY, method=method,
        fn=lambda df: anomaly.detect(
            df, scope=scope, method=method, columns=d.get("columns"),
            value_column=d.get("value_column"), index_column=d.get("index_column"),
            contamination=d.get("contamination", 0.05), threshold=d.get("threshold"),
            period=d.get("period"), projection=d.get("projection", True)),
        config={k: d.get(k) for k in ("scope", "method", "columns", "value_column",
                "index_column", "contamination", "threshold", "period")},
        summary="Anomaly detection",
        save=d.get("save", True), explain=bool(d.get("explain", False)),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def root_cause(request):
    """Driver/root-cause analysis for a defined outcome."""
    d = request.data
    return _run_analysis(
        request, rootcause.driver_analysis, "Root-cause analysis",
        target_column=d.get("target_column"), target_value=d.get("target_value"),
        op=d.get("op"), threshold=d.get("threshold"),
        feature_columns=d.get("feature_columns"),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cluster(request):
    """KMeans / DBSCAN clustering with per-cluster profiles."""
    d = request.data
    return _run_analysis(
        request, rootcause.cluster_analysis, "Clustering",
        columns=d.get("columns"), method=d.get("method", "kmeans"), k=d.get("k", 4),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def associations(request):
    """Association-rule mining over categorical columns."""
    d = request.data
    return _run_analysis(
        request, rootcause.association_rules_mining, "Association mining",
        columns=d.get("columns"), target_column=d.get("target_column"),
        target_value=d.get("target_value"),
        min_support=d.get("min_support", 0.05),
        min_confidence=d.get("min_confidence", 0.5),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def forecast_metric(request):
    """Forecast an operational metric (personnel / time / cost) over time.

    With ``method`` omitted (or ``holt``) this keeps the historic Holt's-linear
    projection. Any other ``method`` (or ``auto``) routes the resampled series
    through the unified forecasting engine (see B4)."""
    d = request.data
    method = d.get("method") or "holt"
    return _run_and_save(
        request, workflow=AnalysisRun.Workflow.FORECAST, method=method,
        fn=lambda df: forecasting_ext.forecast_metric(
            df, date_column=d.get("date_column"), value_column=d.get("value_column"),
            agg=d.get("agg", "count"), freq=d.get("freq", "M"),
            periods=d.get("periods", 6), group_by=d.get("group_by"),
            method=(None if method == "holt" else method),
            exog_columns=d.get("exog_columns")),
        config={k: d.get(k) for k in ("date_column", "value_column", "agg", "freq",
                                      "periods", "group_by", "method", "exog_columns")},
        summary="Metric forecast",
        save=d.get("save", True), explain=bool(d.get("explain", False)),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def forecast_advanced(request):
    """Advanced forecasting workbench — the unified engine (10 methods + 'auto')
    on a numeric column or a resampled date series, with backtest accuracy."""
    import pandas as pd

    from . import forecasting_models as FM
    from . import predict
    from .forecasting_ext import _FREQ, _build_series, _label

    d = request.data
    method = d.get("method") or "auto"
    periods = int(min(max(d.get("periods", 6) or 6, 1), 60))
    metric = d.get("metric", "smape")
    ci = d.get("ci", 0.95)

    def _run(df):
        value_column = d.get("value_column") or d.get("column")
        date_column = d.get("date_column")
        if date_column:
            if date_column not in df.columns:
                raise AnalyticsError(f"Date column '{date_column}' not found.")
            agg = (d.get("agg") or "count").lower()
            freq = (d.get("freq") or "M").upper()
            if freq not in _FREQ:
                raise AnalyticsError("freq must be one of D, W, M, Q, Y.")
            rule, fmt = _FREQ[freq]
            work = df.copy()
            work["_dt"] = pd.to_datetime(work[date_column], errors="coerce")
            work = work.dropna(subset=["_dt"])
            if work.empty:
                raise AnalyticsError(f"Column '{date_column}' has no parseable dates.")
            s = _build_series(work, rule, value_column, agg)
            if len(s) < 5:
                raise AnalyticsError("Need at least 5 periods to forecast.")
            y = s.to_numpy(float)
            labels_hist = [_label(ts, fmt) for ts in s.index]
            future = pd.date_range(s.index[-1], periods=periods + 1, freq=rule)[1:]
            labels_fc = [_label(ts, fmt) for ts in future]
            res = FM.run_forecast(y, periods=periods, methods=method, freq=freq,
                                  metric=metric, ci=ci)
        else:
            if not value_column or value_column not in df.columns:
                raise AnalyticsError("A numeric 'value_column' (or 'column') is required.")
            exog = None
            exog_columns = [c for c in (d.get("exog_columns") or []) if c in df.columns]
            if exog_columns:
                num = df[[value_column, *exog_columns]].apply(pd.to_numeric, errors="coerce").dropna()
                y = num[value_column].to_numpy(float)
                exog = num[exog_columns].to_numpy(float)
            else:
                y = predict._series(df, value_column)
            labels_hist = [str(i + 1) for i in range(len(y))]
            labels_fc = [str(len(y) + i + 1) for i in range(periods)]
            res = FM.run_forecast(y, periods=periods, methods=method, exog=exog,
                                  metric=metric, ci=ci)
        res["value_column"] = value_column
        res["history_labels"] = labels_hist
        res["forecast_labels"] = labels_fc
        return res

    return _run_and_save(
        request, workflow=AnalysisRun.Workflow.FORECAST, method=method, fn=_run,
        config={k: d.get(k) for k in ("date_column", "value_column", "column", "agg",
                "freq", "periods", "method", "metric", "exog_columns", "ci")},
        summary="Forecast",
        save=d.get("save", True), explain=bool(d.get("explain", False)),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def discover_relationships(request):
    """Discover hidden relationships among a dataset's columns."""
    return _run_analysis(request, relationships.discover_relationships,
                         "Relationship discovery")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dataset_links(request):
    """Suggest join keys shared across the user's accessible datasets."""
    from datasets.models import Dataset

    user = request.user
    qs = (Dataset.objects.all() if getattr(user, "is_admin", False)
          else Dataset.objects.filter(Q(owner=user) | Q(shared_with=user)).distinct())
    meta = [{"id": d.id, "name": d.name,
             "columns": [str(c) for c in (d.cached_columns or [])]}
            for d in qs[:100]]
    return Response({"result": relationships.suggest_links(meta)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def explain_view(request):
    """Plain-English explanation of an analytics result via the local LLM.

    Body: ``{"workflow": "...", "result": {...}}`` or ``{"run_id": <id>}``,
    plus an optional free-form ``question``. Always returns HTTP 200 with
    ``{configured, answer, suggestions}`` (degrades gracefully if the LLM is off).
    """
    from .explain import explain_result

    d = request.data
    workflow = (d.get("workflow") or "generic").strip().lower()
    question = (d.get("question") or "").strip()
    result = d.get("result")
    dataset = None

    run_id = d.get("run_id")
    if run_id:
        run = AnalysisRun.objects.filter(pk=run_id).first()
        if run is None or not run.accessible_by(request.user):
            return Response({"error": True, "detail": "Run not found or access denied."},
                            status=status.HTTP_404_NOT_FOUND)
        result, workflow, dataset = run.result, run.workflow, run.dataset

    if not isinstance(result, dict) or not result:
        return Response({"error": True, "detail": "Provide a 'result' object or a 'run_id'."},
                        status=status.HTTP_400_BAD_REQUEST)

    return Response(explain_result(workflow, result, dataset=dataset, question=question))


# --------------------------------------------------------------------------
# Saved analysis runs — history (list / retrieve / delete)
# --------------------------------------------------------------------------
class AnalysisRunViewSet(viewsets.ModelViewSet):
    """Read + delete saved analytics runs. Runs are *created* by the workflow
    endpoints (stats/anomaly/forecast), not here."""

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "delete", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        qs = AnalysisRun.objects.select_related("dataset", "owner")
        if getattr(user, "is_admin", False):
            pass
        else:
            qs = qs.filter(Q(owner=user) | Q(shared_with=user)).distinct()
        params = self.request.query_params
        if params.get("workflow"):
            qs = qs.filter(workflow=params["workflow"])
        if params.get("dataset"):
            qs = qs.filter(dataset_id=params["dataset"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return AnalysisRunListSerializer
        return AnalysisRunDetailSerializer

    def perform_destroy(self, instance):
        record_audit(self.request, AuditLog.Action.DELETE, target_type="AnalysisRun",
                     target_id=instance.id, summary=f"Deleted run '{instance.name}'")
        instance.delete()
