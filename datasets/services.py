"""Dataset refresh and alert evaluation."""
import pandas as pd
from django.utils import timezone

from querybuilder.executor import execute_raw_sql, execute_spec
from querybuilder.models import QueryDefinition

_COND_OPS = {
    "gt":  lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt":  lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "eq":  lambda v, t: abs(v - t) < 1e-9,
}
_COND_LABELS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "="}


def refresh_dataset(dataset, params: dict | None = None):
    """Re-run the dataset's query, cache the result, then evaluate alerts."""
    query = dataset.query
    datasource = query.datasource
    database = query.database or None
    effective_params = {**(dataset.param_defaults or {}), **(params or {})}

    if query.mode == QueryDefinition.Mode.RAW:
        result = execute_raw_sql(
            datasource, query.raw_sql, database,
            params=effective_params or None,
        )
    else:
        result = execute_spec(datasource, query.spec, database)

    now = timezone.now()
    dataset.cached_columns = result["columns"]
    dataset.cached_rows = result["rows"]
    dataset.row_count = result["row_count"]
    dataset.last_refreshed_at = now
    dataset.last_error = ""
    dataset.schedule_next_refresh(from_time=now)
    dataset.save(
        update_fields=[
            "cached_columns", "cached_rows", "row_count",
            "last_refreshed_at", "last_error", "next_refresh_at", "updated_at",
        ]
    )

    try:
        evaluate_alerts(dataset)
    except Exception:  # noqa: BLE001 — alerts are best-effort
        pass

    return result


def evaluate_alerts(dataset):
    """Check all active alerts for the dataset and fire AlertEvent records."""
    from .models import AlertEvent, DatasetAlert

    alerts = DatasetAlert.objects.filter(dataset=dataset, is_active=True)
    if not alerts.exists():
        return

    df = pd.DataFrame(
        dataset.cached_rows or [], columns=dataset.cached_columns or None
    )

    now = timezone.now()
    for alert in alerts:
        if alert.column not in df.columns:
            continue

        series = pd.to_numeric(df[alert.column], errors="coerce").dropna()
        if series.empty:
            continue

        agg = alert.aggregation
        if agg == "avg":
            value = float(series.mean())
        elif agg == "sum":
            value = float(series.sum())
        elif agg == "count":
            value = float(len(series))
        elif agg == "max":
            value = float(series.max())
        elif agg == "min":
            value = float(series.min())
        elif agg == "last":
            value = float(series.iloc[-1])
        else:
            value = float(series.mean())

        op = _COND_OPS.get(alert.condition)
        triggered = op is not None and op(value, alert.threshold)

        if triggered:
            label = _COND_LABELS.get(alert.condition, alert.condition)
            message = (
                f"Alert '{alert.name}' triggered: "
                f"{agg}({alert.column}) {label} {alert.threshold} "
                f"(current value: {value:.4g})"
            )
            AlertEvent.objects.create(
                alert=alert,
                triggered_value=value,
                message=message,
            )

        update_fields = {"last_checked_at": now, "updated_at": now}
        if triggered:
            update_fields["last_triggered_at"] = now
        DatasetAlert.objects.filter(pk=alert.pk).update(**update_fields)
