"""Runtime view of the semantic layer for one datasource.

Loads the curated models into plain dataclasses (no further DB hits) so the
retriever / router / schema builder can stay pure and testable. Returns an empty
Layer (``is_present() == False``) when a datasource has no curated layer, so the
text-to-SQL path transparently falls back to raw introspection (Phase 1).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LColumn:
    name: str
    description: str = ""
    dtype: str = ""
    encoding: str = ""
    null_fraction: float | None = None
    needs_cast: bool = False
    do_not_group: bool = False
    synonyms: list = field(default_factory=list)


@dataclass
class LTable:
    name: str
    description: str = ""
    in_scope: bool = True
    row_count: int | None = None
    synonyms: list = field(default_factory=list)
    columns: dict = field(default_factory=dict)  # {COLNAME: LColumn}


@dataclass
class LJoin:
    left: str
    right: str
    on: list = field(default_factory=list)   # [[left_col, right_col], ...]
    description: str = ""


@dataclass
class LMetric:
    key: str
    name: str
    sql: str                       # portable — render with semantics.metric_sql
    description: str = ""
    row_limit: int | None = None   # the metric's own semantic cap (the old TOP n)
    synonyms: list = field(default_factory=list)
    kpi_id: str = ""
    unit: str = ""
    direction: str = "neutral"
    owner: str = ""
    brd_ref: str = ""
    thresholds: dict = field(default_factory=dict)
    status: str = "live"
    observe: bool = False
    classes: list = field(default_factory=list)   # restricted classes (need clearance)
    scoped: bool = False                          # reads a project-scoped view

    @property
    def offered(self) -> bool:
        """Only certified and live metrics answer as certified (the KPI lifecycle)."""
        return self.status in ("certified", "live")


@dataclass
class LHealthTile:
    key: str
    label: str
    sql: str                       # portable — render with semantics.metric_sql
    description: str = ""
    unit: str = "count"
    direction: str = "neutral"
    target: float | None = None
    roles: list = field(default_factory=list)   # empty = every role
    position: int = 0
    warn: float | None = None
    critical: float | None = None
    classes: list = field(default_factory=list)
    scoped: bool = False
    group: str = ""
    board: str = ""
    kpi_id: str = ""

    def visible_to(self, role: str) -> bool:
        return not self.roles or (role or "") in self.roles


@dataclass
class Layer:
    datasource_id: int | None = None
    datasource_name: str = ""
    notes: str = ""
    timezone: str = ""                               # IANA, renders {{local:…}}
    timezone_mssql: str = ""
    tables: dict = field(default_factory=dict)       # {NAME: LTable}
    joins: list = field(default_factory=list)        # [LJoin]
    metrics: list = field(default_factory=list)      # [LMetric]
    health: list = field(default_factory=list)       # [LHealthTile]

    def is_present(self) -> bool:
        return bool(self.tables)

    def in_scope_tables(self) -> list:
        return [t for t in self.tables.values() if t.in_scope]

    def table_names(self) -> set:
        return {n.upper() for n in self.tables}

    def do_not_group_columns(self) -> set:
        out = set()
        for t in self.tables.values():
            for c in t.columns.values():
                if c.do_not_group:
                    out.add(c.name.upper())
        return out

    def joins_for(self, table: str) -> list:
        u = table.upper()
        return [j for j in self.joins if u in (j.left.upper(), j.right.upper())]


# The layer is read on EVERY question (texttosql.runner) and is pure, curated data
# that only changes when `load_semantic_layer` runs. Rebuilding it from the database
# per question cost several ORM round-trips on the hot path for no benefit.
# Keyed by (datasource_id, updated_at) so a reload is picked up without a restart:
# apply_spec bumps SemanticLayer.updated_at, which changes the key.
_CACHE: dict[tuple, Layer] = {}


def _cache_key(datasource):
    from .models import SemanticLayer

    stamp = (SemanticLayer.objects.filter(datasource=datasource)
             .values_list("updated_at", flat=True).first())
    return (datasource.id, stamp)


def layer_version(datasource) -> str:
    """When this source's layer was last loaded: part of any cache key built from it."""
    stamp = _cache_key(datasource)[1]
    return stamp.isoformat() if stamp else ""


def render_for(layer: "Layer", sql: str, datasource, *, scope=None) -> str:
    """Render portable SQL with this layer's time zone (and the caller's scope)."""
    from .metric_sql import render

    return render(sql, datasource, timezone=layer.timezone or None,
                  timezone_mssql=layer.timezone_mssql, scope=scope)


def invalidate(datasource=None) -> None:
    """Drop the cached layer(s). Called by apply_spec; safe to call anywhere."""
    if datasource is None:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k[0] == datasource.id]:
        _CACHE.pop(key, None)


def get_semantic_layer(datasource) -> Layer:
    """Build the runtime Layer for ``datasource`` from the curated models (cached)."""
    key = _cache_key(datasource)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    layer = _build_semantic_layer(datasource)
    invalidate(datasource)          # evict the stale-stamp entry for this datasource
    _CACHE[key] = layer
    return layer


def _build_semantic_layer(datasource) -> Layer:
    from .models import (CertifiedJoin, CertifiedMetric, HealthTile, SemanticLayer,
                         SemanticTable)

    layer = Layer(datasource_id=datasource.id, datasource_name=datasource.name)
    sl = SemanticLayer.objects.filter(datasource=datasource).first()
    if sl:
        layer.notes = sl.notes
        layer.timezone = sl.timezone
        layer.timezone_mssql = sl.timezone_mssql
    tables = (SemanticTable.objects.filter(datasource=datasource)
              .prefetch_related("columns"))
    for t in tables:
        lt = LTable(name=t.name, description=t.description, in_scope=t.in_scope,
                    row_count=t.row_count, synonyms=list(t.synonyms or []))
        for c in t.columns.all():
            lt.columns[c.name.upper()] = LColumn(
                name=c.name, description=c.description, dtype=c.dtype,
                encoding=c.encoding, null_fraction=c.null_fraction,
                needs_cast=c.needs_cast, do_not_group=c.do_not_group,
                synonyms=list(c.synonyms or []))
        layer.tables[t.name.upper()] = lt

    for j in CertifiedJoin.objects.filter(datasource=datasource):
        layer.joins.append(LJoin(left=j.left_table, right=j.right_table,
                                 on=list(j.on or []), description=j.description))
    for m in CertifiedMetric.objects.filter(datasource=datasource):
        layer.metrics.append(LMetric(key=m.key, name=m.name, sql=m.sql,
                                     description=m.description,
                                     row_limit=m.row_limit,
                                     synonyms=list(m.synonyms or []),
                                     kpi_id=m.kpi_id, unit=m.unit, direction=m.direction,
                                     owner=m.owner, brd_ref=m.brd_ref,
                                     thresholds=dict(m.thresholds or {}), status=m.status,
                                     observe=m.observe, classes=list(m.classes or []),
                                     scoped=m.scoped))
    for h in HealthTile.objects.filter(datasource=datasource):
        layer.health.append(LHealthTile(
            key=h.key, label=h.label, sql=h.sql, description=h.description,
            unit=h.unit, direction=h.direction, target=h.target,
            roles=list(h.roles or []), position=h.position, warn=h.warn,
            critical=h.critical, classes=list(h.classes or []), scoped=h.scoped,
            group=h.group, board=h.board, kpi_id=h.kpi_id))
    return layer
