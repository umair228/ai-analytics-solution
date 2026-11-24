from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.access import Visibility, can_view
from core.models import TimeStampedModel


class Dataset(TimeStampedModel):
    """A named, reusable result set backed by a saved query."""

    Visibility = Visibility          # core.access — one rule, not four copies of it

    class RefreshInterval(models.TextChoices):
        MANUAL = "manual", "Manual only"
        HOURLY = "hourly", "Every hour"
        DAILY = "daily", "Every day"
        WEEKLY = "weekly", "Every week"

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    query = models.ForeignKey(
        "querybuilder.QueryDefinition",
        on_delete=models.CASCADE,
        related_name="datasets",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="datasets"
    )
    site = models.ForeignKey(
        "accounts.Site", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="datasets",
    )
    # The laboratory this belongs to. It is what makes Visibility.LAB mean anything —
    # without it, `User.labs` was decoration.
    lab = models.ForeignKey(
        "accounts.Lab", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="datasets",
    )
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_datasets"
    )

    cached_columns = models.JSONField(default=list, blank=True)
    cached_rows = models.JSONField(default=list, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    # True COUNT(*) of the full query result in the source database — the
    # cached rows above are only a preview capped at DSE_QUERY_MAX_ROWS.
    total_row_count = models.BigIntegerField(null=True, blank=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    calculated_fields = models.JSONField(default=list, blank=True)

    refresh_interval = models.CharField(
        max_length=10, choices=RefreshInterval.choices,
        default=RefreshInterval.MANUAL,
    )
    next_refresh_at = models.DateTimeField(null=True, blank=True)

    # Default values for any query parameters defined on the underlying query.
    # Format: {"param_name": "value", ...}
    param_defaults = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    INTERVAL_DELTA = {
        RefreshInterval.HOURLY: timedelta(hours=1),
        RefreshInterval.DAILY: timedelta(days=1),
        RefreshInterval.WEEKLY: timedelta(weeks=1),
    }

    def schedule_next_refresh(self, from_time=None):
        delta = self.INTERVAL_DELTA.get(self.refresh_interval)
        self.next_refresh_at = (from_time or timezone.now()) + delta if delta else None
        return self.next_refresh_at

    def accessible_by(self, user) -> bool:
        return can_view(self, user)


class DatasetAlert(TimeStampedModel):
    """An alert on a dataset — fires an event on refresh.

    Two families, sharing one comparison. A THRESHOLD alert compares an aggregate of
    a column ("average(sulphur) > 12"). The others compare the *outcome of an
    analysis* — a Westgard rejection count, a Cpk, an anomaly count, a projected
    value — because that is what a laboratory actually monitors, and none of it can
    be expressed as a number sitting in a column. See ``datasets.alert_rules``.

    In both cases ``condition``/``threshold`` do the comparing, so "reject when the
    rejection count > 0" reuses the existing machinery rather than reinventing it.
    """

    class Kind(models.TextChoices):
        THRESHOLD  = "threshold",  "Threshold on a column"
        SPC        = "spc",        "Control-chart rule violated (Westgard / Nelson)"
        CAPABILITY = "capability", "Process capability (Cpk) below target"
        ANOMALY    = "anomaly",    "Anomalies detected"
        FORECAST   = "forecast",   "Forecast breaches a limit"

    class Condition(models.TextChoices):
        GT  = "gt",  "Greater than"
        GTE = "gte", "Greater than or equal"
        LT  = "lt",  "Less than"
        LTE = "lte", "Less than or equal"
        EQ  = "eq",  "Equal to"

    class Aggregation(models.TextChoices):
        AVG   = "avg",   "Average"
        SUM   = "sum",   "Sum"
        COUNT = "count", "Count"
        MAX   = "max",   "Maximum"
        MIN   = "min",   "Minimum"
        LAST  = "last",  "Last value"

    name        = models.CharField(max_length=200)
    dataset     = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="alerts")
    kind        = models.CharField(max_length=12, choices=Kind.choices,
                                   default=Kind.THRESHOLD)
    column      = models.CharField(max_length=200)
    # Threshold alerts only; ignored by the analytic kinds, which compute their own
    # scalar (rejection count, Cpk, anomaly count, projected value).
    aggregation = models.CharField(
        max_length=10, choices=Aggregation.choices, default=Aggregation.AVG
    )
    condition   = models.CharField(max_length=5, choices=Condition.choices)
    threshold   = models.FloatField()
    # Analysis parameters for the analytic kinds, e.g. for SPC:
    #   {"test": "levey_jennings", "mean": 100, "sd": 2, "rules": "westgard"}
    config      = models.JSONField(default=dict, blank=True)
    is_active   = models.BooleanField(default=True)

    # Minutes to stay quiet after firing. 0 = fire on every check, which is the old
    # behaviour and is kept as the default so existing alerts are unchanged — but an
    # SPC alert re-firing hourly on the same unresolved violation is precisely how a
    # monitoring scheme gets switched off. Set this on any analytic alert.
    cooldown_minutes = models.PositiveIntegerField(default=0)

    # Notification targets — both optional
    notify_email   = models.EmailField(blank=True)
    notify_webhook = models.URLField(blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alerts"
    )

    last_checked_at   = models.DateTimeField(null=True, blank=True)
    last_triggered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def in_cooldown(self, now) -> bool:
        if not self.cooldown_minutes or not self.last_triggered_at:
            return False
        from datetime import timedelta
        return (now - self.last_triggered_at) < timedelta(minutes=self.cooldown_minutes)


class AlertEvent(TimeStampedModel):
    """A single firing of a DatasetAlert — created when the condition is met."""

    alert           = models.ForeignKey(DatasetAlert, on_delete=models.CASCADE, related_name="events")
    triggered_value = models.FloatField()
    message         = models.TextField()
    # The evidence behind the firing — for an SPC alert, which rules fired, over
    # which points, and whether the error is random or systematic. "Out of control"
    # is a verdict; this is what tells the analyst where to look.
    detail          = models.JSONField(default=dict, blank=True)
    acknowledged    = models.BooleanField(default=False)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="acknowledged_events"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Event for '{self.alert.name}' @ {self.created_at:%Y-%m-%d %H:%M}"


class DatasetReport(TimeStampedModel):
    """A scheduled email report — a dataset CSV, a saved analysis run, a chart, or a
    whole board as a PDF."""

    class Schedule(models.TextChoices):
        DAILY   = "daily",   "Daily"
        WEEKLY  = "weekly",  "Weekly"
        MONTHLY = "monthly", "Monthly"

    class Kind(models.TextChoices):
        DATASET_CSV  = "dataset_csv",  "Dataset CSV"
        ANALYSIS_RUN = "analysis_run", "Saved analysis run"
        CHART        = "chart",        "Chart"
        BOARD        = "board",        "Board (PDF)"

    dataset          = models.ForeignKey(Dataset, null=True, blank=True,
                                         on_delete=models.CASCADE, related_name="reports")
    owner            = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                         on_delete=models.CASCADE, related_name="dataset_reports")
    name             = models.CharField(max_length=200)
    recipient_emails = models.TextField(help_text="Comma-separated list of recipient email addresses")
    schedule         = models.CharField(max_length=10, choices=Schedule.choices, default=Schedule.DAILY)
    is_active        = models.BooleanField(default=True)
    last_sent_at     = models.DateTimeField(null=True, blank=True)

    # Release-0: a report can target a saved analysis run or a chart, exported in
    # a chosen format, in addition to the original dataset-CSV behaviour.
    kind             = models.CharField(max_length=16, choices=Kind.choices,
                                        default=Kind.DATASET_CSV)
    analysis_run     = models.ForeignKey("analytics.AnalysisRun", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="reports")
    chart_config     = models.JSONField(default=dict, blank=True)
    export_format    = models.CharField(max_length=8, default="csv")
    # A board report: the whole board as a PDF (core.board_render). Only a board that
    # everyone in the organisation may open can be emailed (DatasetReportSerializer).
    dashboard        = models.ForeignKey("dashboards.Dashboard", null=True, blank=True,
                                         on_delete=models.CASCADE, related_name="reports")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.schedule})"


class SavedView(TimeStampedModel):
    """A named, shareable exploration — the thing the Data Explorer never had.

    Explorer state lived in **one browser's localStorage**, keyed by dataset, unnamed. The
    QA manager's Monday-morning view — the filters, the grouping, the chart, the date window
    she has tuned over months — existed only on the laptop she happened to build it on. She
    could not name it, could not hand it to the analyst covering for her, and clearing the
    cache destroyed it. Every colleague who wanted the same view had to rebuild it from
    memory, and any two of them would build it slightly differently.

    So: give it a name, and let her share it.

    **The security property is the whole design.** A saved view is a *configuration*, not
    data — and a sharing feature is exactly where an access-control bypass likes to hide. If
    Nadia shares an organisation-wide view of a Crude-Distillation dataset, Omar in
    Lubricants must not get the rows by opening it. He must not even see the view exists,
    because a view named "Crude Distillation — off-spec batches" leaks the dataset by leaking
    its title.

    Hence :meth:`SavedView.visible_to`: a view is visible only if the user may see **both the
    view and the dataset it points at**. Sharing a view can never widen access to data.
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    dataset = models.ForeignKey(
        Dataset, on_delete=models.CASCADE, related_name="saved_views",
    )
    # The screen this view belongs to. One today; the field exists so that adding another
    # does not need a migration and a second model that drifts from this one.
    page = models.CharField(max_length=32, default="explorer")
    # The state itself: chart types, x/y fields, aggregation, group-by, filters, date window.
    config = models.JSONField(default=dict, blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views",
    )
    lab = models.ForeignKey(
        "accounts.Lab", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="saved_views",
    )
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_saved_views"
    )

    class Meta:
        ordering = ["name"]

    @classmethod
    def visible_to(cls, user):
        """Views this user may open — **and whose data they may also read.**

        The second half is the point. Filtering only on the view's own visibility would let
        a shared view act as a key to a dataset the user is not allowed to see, which would
        make the sharing feature a hole in URS-032 rather than a feature built on it.
        """
        from core.access import visibility_q, visible_to as _visible

        qs = _visible(cls.objects.select_related("dataset", "owner", "lab"), user)
        if getattr(user, "is_admin", False):
            return qs
        # …and the dataset behind it.
        return qs.filter(visibility_q(user, "dataset")).distinct()

    def accessible_by(self, user) -> bool:
        return self.__class__.visible_to(user).filter(pk=self.pk).exists()

    def __str__(self):
        return self.name
