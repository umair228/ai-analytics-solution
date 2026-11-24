"""The curated semantic layer / data dictionary (DIS_TalkToData_Plan.md §4).

This is the heart of accuracy: it teaches the model the *meaning* of a cryptic
LabWare schema — value encodings (IN_SPEC='F'), required casts (NUMERIC_ENTRY is
text), near-empty columns not to group on (INSTRUMENT), certified joins (LabWare
declares no FKs), business synonyms, and certified metrics (pre-validated SQL for
hot questions). Authored as version-controlled YAML, loaded into these tables for
runtime use by the text-to-SQL retriever/validator/router.
"""
from django.db import models

from core.models import TimeStampedModel


class SemanticLayer(TimeStampedModel):
    """Datasource-level free-form guidance that isn't tied to one column
    (date-column choice, status-code legends, general do/don'ts)."""
    datasource = models.OneToOneField(
        "connections.DataSource", on_delete=models.CASCADE, related_name="semantic_layer")
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"semantic layer for {self.datasource_id}"


class SemanticTable(TimeStampedModel):
    datasource = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE, related_name="semantic_tables")
    name = models.CharField(max_length=128)              # real table name, e.g. SAMPLE
    description = models.TextField(blank=True)
    in_scope = models.BooleanField(default=True)         # excluded ~junk tables stay False
    row_count = models.IntegerField(null=True, blank=True)
    synonyms = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name"]
        unique_together = (("datasource", "name"),)

    def __str__(self):
        return self.name


class SemanticColumn(TimeStampedModel):
    table = models.ForeignKey(SemanticTable, on_delete=models.CASCADE, related_name="columns")
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    dtype = models.CharField(max_length=64, blank=True)
    encoding = models.CharField(max_length=255, blank=True)   # e.g. "T=in-spec, F=off-spec"
    null_fraction = models.FloatField(null=True, blank=True)
    needs_cast = models.BooleanField(default=False)           # text-numeric → TRY_CONVERT
    do_not_group = models.BooleanField(default=False)         # near-empty / unsafe to group
    synonyms = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name"]
        unique_together = (("table", "name"),)

    def __str__(self):
        return f"{self.table.name}.{self.name}"


class CertifiedJoin(TimeStampedModel):
    """An allowed join path (LabWare declares no foreign keys, so these are curated)."""
    datasource = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE, related_name="certified_joins")
    left_table = models.CharField(max_length=128)
    right_table = models.CharField(max_length=128)
    # [["RESULT_COL","SAMPLE_COL"], ...] — left.col = right.col
    on = models.JSONField(default=list)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["left_table", "right_table"]

    def __str__(self):
        return f"{self.left_table}⋈{self.right_table}"


class HealthTile(TimeStampedModel):
    """One number on the Lab Health home page — certified, not fabricated.

    The home page used to show counts of *app objects* (connections, datasets,
    dashboards): true, and of no interest to anyone who runs a laboratory. These
    tiles show the lab instead — samples logged, open backlog, off-spec rate,
    turnaround against SLA.

    They are curated per-datasource in the spec YAML for the same reason the
    metrics are: EGPC and OMICS are different LabWare schemas, so a home page
    hardcoded to EGPC's tables would render blank for a genomics lab. The SQL is
    portable (semantics.metric_sql) and must return exactly ONE row, ONE numeric
    column — a tile is a scalar.
    """

    class Unit(models.TextChoices):
        COUNT = "count", "Count"
        PERCENT = "percent", "Percent"
        HOURS = "hours", "Hours"

    class Direction(models.TextChoices):
        LOWER_BETTER = "lower_better", "Lower is better"
        HIGHER_BETTER = "higher_better", "Higher is better"
        NEUTRAL = "neutral", "Neutral"

    datasource = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE, related_name="health_tiles")
    key = models.CharField(max_length=128)
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    sql = models.TextField()
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.COUNT)
    direction = models.CharField(max_length=14, choices=Direction.choices,
                                 default=Direction.NEUTRAL)
    # The SLA / control limit this number is judged against. A tile with no target
    # is reported, not graded — we do not invent a threshold nobody agreed to.
    target = models.FloatField(null=True, blank=True)
    # Which lab roles see this tile. Empty = everyone.
    roles = models.JSONField(default=list, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "key"]
        unique_together = (("datasource", "key"),)

    def __str__(self):
        return self.key


class CertifiedMetric(TimeStampedModel):
    """Pre-validated SQL for a hot question — the deterministic router fast path.

    ``sql`` is stored *portable* (see semantics.metric_sql): unquoted identifiers
    plus ``{{num:…}}`` / ``{{table:…}}`` macros, rendered per-dialect at execution.
    One definition therefore answers the same question against the LabWare MSSQL
    source and against the Postgres/SQLite replica.
    """
    datasource = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE, related_name="certified_metrics")
    key = models.CharField(max_length=128)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    sql = models.TextField()
    # The metric's OWN row cap — the semantic "top 5 tests", not the safety cap.
    # Kept out of the SQL because the syntax is positional and dialect-specific
    # (TOP n vs LIMIT n vs FETCH FIRST); inject_row_limit places it correctly.
    row_limit = models.PositiveIntegerField(null=True, blank=True)
    synonyms = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["key"]
        unique_together = (("datasource", "key"),)

    def __str__(self):
        return self.key
