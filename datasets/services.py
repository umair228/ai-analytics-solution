"""Dataset refresh and alert evaluation."""
import logging

import pandas as pd
import requests
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone

from querybuilder.executor import execute_raw_sql, execute_spec
from querybuilder.models import QueryDefinition

logger = logging.getLogger(__name__)

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
    effective_params = {**query.declared_param_defaults(),
                        **(dataset.param_defaults or {}), **(params or {})}

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

    # The cache is a preview (capped at DSE_QUERY_MAX_ROWS) — also record the
    # query's TRUE row count so UIs can show full-data totals. Best-effort.
    dataset.total_row_count = dataset.row_count
    if result.get("truncated") and query.mode == QueryDefinition.Mode.RAW:
        try:
            from querybuilder.executor import execute_dataset_query
            counted = execute_dataset_query(
                datasource, query.raw_sql, database,
                params=effective_params or None,
                columns=result["columns"], spec={"mode": "count"},
            )
            dataset.total_row_count = counted["total_row_count"]
        except Exception:  # noqa: BLE001
            dataset.total_row_count = None

    dataset.last_refreshed_at = now
    dataset.last_error = ""
    dataset.schedule_next_refresh(from_time=now)
    dataset.save(
        update_fields=[
            "cached_columns", "cached_rows", "row_count", "total_row_count",
            "last_refreshed_at", "last_error", "next_refresh_at", "updated_at",
        ]
    )

    try:
        evaluate_alerts(dataset)
    except Exception:  # noqa: BLE001 — alerts are best-effort
        pass

    return result


def _threshold_value(alert, df):
    """The old, and still most common, alert: an aggregate of one column."""
    series = pd.to_numeric(df[alert.column], errors="coerce").dropna()
    if series.empty:
        return None
    agg = alert.aggregation
    if agg == "sum":
        return float(series.sum())
    if agg == "count":
        return float(len(series))
    if agg == "max":
        return float(series.max())
    if agg == "min":
        return float(series.min())
    if agg == "last":
        return float(series.iloc[-1])
    return float(series.mean())


def evaluate_alerts(dataset):
    """Check all active alerts for the dataset and fire AlertEvent records.

    A threshold alert compares an aggregate of a column. An analytic alert (SPC,
    capability, anomaly, forecast) compares the *outcome of an analysis* — see
    ``datasets.alert_rules``. Both then go through the same condition/threshold
    comparison and the same notification path.
    """
    from .alert_rules import RULES
    from .models import AlertEvent, DatasetAlert

    alerts = DatasetAlert.objects.filter(dataset=dataset, is_active=True)
    if not alerts.exists():
        return

    df = pd.DataFrame(
        dataset.cached_rows or [], columns=dataset.cached_columns or None
    )

    now = timezone.now()
    for alert in alerts:
        if alert.column and alert.column not in df.columns:
            continue

        detail, subject, summary = {}, "", ""
        rule = RULES.get(alert.kind)
        try:
            if rule is None:                       # threshold — the original path
                value = _threshold_value(alert, df)
                subject = f"{alert.aggregation}({alert.column})"
            else:
                outcome = rule(alert, df)
                if outcome is None:                # the rule could not be evaluated
                    DatasetAlert.objects.filter(pk=alert.pk).update(
                        last_checked_at=now, updated_at=now)
                    continue
                value, subject, summary = outcome.value, outcome.subject, outcome.summary
                detail = outcome.detail
        except Exception as exc:  # noqa: BLE001 — one bad rule must not stop the rest
            logger.exception("Alert %s (%s) failed to evaluate: %s",
                             alert.pk, alert.kind, exc)
            DatasetAlert.objects.filter(pk=alert.pk).update(
                last_checked_at=now, updated_at=now)
            continue

        if value is None:
            continue

        op = _COND_OPS.get(alert.condition)
        triggered = op is not None and op(value, alert.threshold)

        # A rule that keeps re-firing on the same unresolved violation is how a
        # monitoring scheme gets ignored. Still record the check; just stay quiet.
        if triggered and alert.in_cooldown(now):
            DatasetAlert.objects.filter(pk=alert.pk).update(
                last_checked_at=now, updated_at=now)
            continue

        if triggered:
            label = _COND_LABELS.get(alert.condition, alert.condition)
            condition = f"{subject} {label} {alert.threshold} (current value: {value:.4g})"
            # Lead with WHAT happened; the arithmetic that tripped it comes second.
            message = (f"Alert '{alert.name}' triggered: {summary} [{condition}]"
                       if summary else f"Alert '{alert.name}' triggered: {condition}")
            AlertEvent.objects.create(
                alert=alert,
                triggered_value=value,
                message=message,
                detail=detail,
            )
            _notify(alert, message)

        update_fields = {"last_checked_at": now, "updated_at": now}
        if triggered:
            update_fields["last_triggered_at"] = now
        DatasetAlert.objects.filter(pk=alert.pk).update(**update_fields)


def _notify(alert, message: str) -> None:
    """Send email and/or webhook notification for a triggered alert."""
    if alert.notify_email:
        try:
            send_mail(
                subject=f"[DIS Alert] {alert.name}",
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[alert.notify_email],
                fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alert email failed for alert %s: %s", alert.id, exc)

    if alert.notify_webhook:
        try:
            requests.post(
                alert.notify_webhook,
                json={"alert": alert.name, "dataset": alert.dataset.name, "message": message},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alert webhook failed for alert %s: %s", alert.id, exc)
