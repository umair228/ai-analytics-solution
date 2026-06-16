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


class CertifiedMetric(TimeStampedModel):
    """Pre-validated SQL for a hot question — the deterministic router fast path."""
    datasource = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE, related_name="certified_metrics")
    key = models.CharField(max_length=128)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    sql = models.TextField()
    synonyms = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["key"]
        unique_together = (("datasource", "key"),)

    def __str__(self):
        return self.key
