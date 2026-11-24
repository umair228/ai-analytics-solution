from django.conf import settings
from django.db import models
from django.db.models import F

from core.access import Visibility, can_view
from core.models import TimeStampedModel


class Dashboard(TimeStampedModel):
    """An interactive dashboard — a canvas of widgets bound to datasets."""

    Visibility = Visibility          # core.access — one rule, not four copies of it

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dashboards"
    )
    site = models.ForeignKey(
        "accounts.Site", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="dashboards",
    )
    lab = models.ForeignKey(
        "accounts.Lab", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="dashboards",
    )
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_dashboards"
    )
    filters = models.JSONField(default=list, blank=True)
    # Where the board sits in lists, smallest first. A domain pack sets it so a manager
    # finds the executive board first; boards without one follow, newest first.
    position = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = [F("position").asc(nulls_last=True), "-updated_at"]

    def __str__(self):
        return self.name

    def accessible_by(self, user) -> bool:
        return can_view(self, user)


class Widget(TimeStampedModel):
    """A single chart / KPI / table tile on a dashboard."""

    class Kind(models.TextChoices):
        BAR = "bar", "Bar chart"
        LINE = "line", "Line chart"
        PIE = "pie", "Pie chart"
        AREA = "area", "Area chart"
        SCATTER = "scatter", "Scatter plot"
        HISTOGRAM = "histogram", "Histogram"
        HEATMAP = "heatmap", "Heatmap"
        BOXPLOT = "boxplot", "Box plot"
        KPI = "kpi", "KPI card"
        TABLE = "table", "Table"
        TEXT = "text", "Text / Note"
        STUDIO = "studio", "Chart Studio (multi-dataset)"
        # Management views: a ranked list with bars (top-N), a stage funnel (pipeline)
        # and progress towards a target (value against a goal or budget).
        BAR_LIST = "bar_list", "Ranked bar list (top N)"
        FUNNEL = "funnel", "Funnel (pipeline stages)"
        PROGRESS = "progress", "Progress vs target"
        # One record's facts as label / value rows (a project's number, client, status,
        # dates): the header of a one-object board such as the Project View.
        DETAILS = "details", "Details (one record's facts)"

    dashboard = models.ForeignKey(
        Dashboard, on_delete=models.CASCADE, related_name="widgets"
    )
    title = models.CharField(max_length=200, blank=True)
    widget_type = models.CharField(
        max_length=12, choices=Kind.choices, default=Kind.BAR
    )
    dataset = models.ForeignKey(
        "datasets.Dataset", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="widgets",
    )
    # config maps dataset columns to chart roles, e.g.
    # {"category_field": "lab", "value_field": "rejected_count", "rollup": "sum"}
    config = models.JSONField(default=dict, blank=True)

    # Grid layout (react-grid-layout units)
    x = models.IntegerField(default=0)
    y = models.IntegerField(default=0)
    w = models.IntegerField(default=4)
    h = models.IntegerField(default=6)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.title or f"Widget {self.pk}"


class DashboardShareToken(TimeStampedModel):
    """A token that grants read-only public access to a dashboard."""
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="share_tokens")
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+"
    )
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Share token for {self.dashboard.name}"
