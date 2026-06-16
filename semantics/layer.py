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
    sql: str
    description: str = ""
    synonyms: list = field(default_factory=list)


@dataclass
class Layer:
    datasource_id: int | None = None
    datasource_name: str = ""
    notes: str = ""
    tables: dict = field(default_factory=dict)       # {NAME: LTable}
    joins: list = field(default_factory=list)        # [LJoin]
    metrics: list = field(default_factory=list)      # [LMetric]

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


def get_semantic_layer(datasource) -> Layer:
    """Build the runtime Layer for ``datasource`` from the curated models."""
    from .models import CertifiedJoin, CertifiedMetric, SemanticLayer, SemanticTable

    layer = Layer(datasource_id=datasource.id, datasource_name=datasource.name)
    sl = SemanticLayer.objects.filter(datasource=datasource).first()
    if sl:
        layer.notes = sl.notes
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
                                     synonyms=list(m.synonyms or [])))
    return layer
