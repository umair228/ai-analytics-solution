"""REST API for the statistical analysis engine (Phase 1 — Statistical suite).

Two endpoints power the Statistical Analysis dashboard and the agent tool:
  * GET  /api/analytics/stats/catalog/  — the test registry + param schemas.
  * POST /api/analytics/stats/run/      — run one test on a dataset.
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from core.audit import record_audit
from core.models import AuditLog
from datasets.models import Dataset

from django.db.models import Q

from . import anomaly, forecasting_ext, relationships, rootcause
from .engine import AnalyticsError, build_dataframe
from .stats_catalog import catalog, run_test


def _accessible_dataset(user, dataset_id):
    if not dataset_id:
        return None
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset and dataset.accessible_by(user):
        return dataset
    return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def stats_catalog(request):
    """The available statistical tests and their parameter schemas."""
    return Response({"tests": catalog()})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stats_run(request):
    """Run a statistical test on a dataset.

    Body: ``{"dataset": <id>, "test": "<name>", "params": {...}}``.
    """
    dataset = _accessible_dataset(request.user, request.data.get("dataset"))
    if dataset is None:
        return Response({"error": True, "detail": "Dataset not found or access denied."},
                        status=status.HTTP_404_NOT_FOUND)

    test = (request.data.get("test") or "").strip()
    params = request.data.get("params") or {}
    if not isinstance(params, dict):
        return Response({"error": True, "detail": "'params' must be an object."},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        df = build_dataframe(dataset)
        result = run_test(test, df, params)
    except AnalyticsError as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"Test failed: {exc}"},
                        status=status.HTTP_400_BAD_REQUEST)

    record_audit(request, AuditLog.Action.QUERY, target_type="Dataset",
                 target_id=dataset.id, summary=f"Stat test '{test}' on '{dataset.name}'")
    return Response({"dataset": dataset.id, "test": test, "result": result})


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
    """Multivariate anomaly detection (IsolationForest / LOF)."""
    d = request.data
    return _run_analysis(
        request, anomaly.detect_multivariate, "Anomaly detection",
        columns=d.get("columns"), method=d.get("method", "isolation_forest"),
        contamination=d.get("contamination", 0.05),
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
    """Forecast an operational metric (personnel / time / cost) over time."""
    d = request.data
    return _run_analysis(
        request, forecasting_ext.forecast_metric, "Metric forecast",
        date_column=d.get("date_column"), value_column=d.get("value_column"),
        agg=d.get("agg", "count"), freq=d.get("freq", "M"),
        periods=d.get("periods", 6), group_by=d.get("group_by"),
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
