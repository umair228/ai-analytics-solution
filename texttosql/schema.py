"""Build a compact live-schema context for the text-to-SQL generator.

Phase 1 kept this simple: introspect the live tables/columns and present the most
relevant ones plus a block of data conventions ("DATA NOTES"). Phase 4 adds a
*structured* return (:class:`SchemaContext`) so the validator can enforce a name
allow-list — the model may only reference the tables/columns it was actually shown
— and a ``do_not_group`` set (near-empty columns like INSTRUMENT that produce a
single junk bucket if grouped on).

For a small database every table is shown; for a large LabWare-style schema
(~hundreds of tables) we show a core allow-list plus any tables the question
names — a cheap stand-in for the Phase-3 schema-RAG retriever. Core tables, data
notes and do_not_group can be overridden per datasource via
``DataSource.options`` (``core_tables`` / ``llm_notes`` / ``do_not_group``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings

DEFAULT_CORE_TABLES = [
    "SAMPLE", "RESULT", "TEST", "ANALYSIS", "PRODUCT", "PRODUCT_SPEC",
    "INSTRUMENTS", "LIMS_USERS",
]

# Columns that are essentially empty in EGPC — grouping on them yields one giant
# NULL bucket presented as a "result". The validator rejects GROUP BY on these.
DEFAULT_DO_NOT_GROUP = ["INSTRUMENT", "ASSIGNED_OPERATOR"]

DEFAULT_NOTES = (
    "- IN_SPEC is a single character: 'T' = in-spec/pass, 'F' = off-spec/failed. "
    "Use IN_SPEC='F' for off-spec; never compare it to a number.\n"
    "- NUMERIC_ENTRY (RESULT) and MIN_VALUE/MAX_VALUE (PRODUCT_SPEC) are stored as "
    "TEXT — wrap in TRY_CONVERT(float, col) before any AVG/MIN/MAX/comparison, and "
    "filter WHERE col IS NOT NULL.\n"
    "- A result joins its sample on RESULT.SAMPLE_NUMBER = SAMPLE.SAMPLE_NUMBER.\n"
    "- Product/grade is SAMPLE.PRODUCT. The analysis/test method is RESULT.ANALYSIS.\n"
    "- ENTERED_BY (RESULT) is the operator's user id, not a name; join LIMS_USERS for "
    "a display name if needed. There is no usable instrument column on RESULT/TEST "
    "(TEST.INSTRUMENT is essentially empty) — do NOT group results by instrument; "
    "group by operator (ENTERED_BY), product or analysis instead.\n"
    "- For time series use SAMPLE.LOGIN_DATE (full 3-year history). SAMPLE turnaround "
    "= DATEDIFF(hour, LOGIN_DATE, DATE_COMPLETED).\n"
    "- SAMPLE.STATUS is a code (e.g. A=Authorized, C=Complete, X=Cancelled, R=Rejected).\n"
    "- Always return aggregates with a clear label column and order results so the "
    "answer (e.g. the worst/highest) is the first row."
)


@dataclass
class SchemaContext:
    """The exact schema slice shown to the model — also the validator's allow-list."""
    datasource_name: str
    source_type: str
    tables: dict = field(default_factory=dict)   # {TABLE: [{name,type,primary_key}, ...]}
    do_not_group: set = field(default_factory=set)
    notes: str = ""
    total_tables: int = 0

    def table_names(self) -> set[str]:
        return {t.upper() for t in self.tables}

    def render(self) -> str:
        lines = []
        for name, cols in self.tables.items():
            col_str = ", ".join(
                f"{c['name']} {c['type']}" + (" PK" if c.get("primary_key") else "")
                for c in cols)
            lines.append(f"- {name} ({len(cols)} cols): {col_str}")
        return (
            f"DATABASE: \"{self.datasource_name}\" [{self.source_type}]. "
            f"{self.total_tables} tables total; showing the {len(self.tables)} most relevant.\n"
            "TABLES (use these EXACT table and column names — never invent any):\n"
            + "\n".join(lines)
            + "\n\nDATA NOTES (apply exactly):\n" + self.notes
        )


def _select_tables(names, core, question, max_tables):
    """Pick tables: core allow-list first, then any the question names, then fill."""
    by_lower = {n.lower(): n for n in names}
    chosen, seen = [], set()

    def add(name):
        if not name:
            return
        real = by_lower.get(name.lower())
        if real and real.lower() not in seen:
            chosen.append(real)
            seen.add(real.lower())

    for c in core:
        add(c)

    q = (question or "").lower()
    if q:
        for n in names:
            if n.lower() in seen:
                continue
            toks = n.lower().replace("_", " ").split()
            if re.search(rf"\b{re.escape(n.lower())}\b", q) or any(
                len(p) > 3 and re.search(rf"\b{re.escape(p)}\b", q) for p in toks
            ):
                add(n)

    for n in names:
        if len(chosen) >= max_tables:
            break
        add(n)
    return chosen[:max_tables]


def _assemble_notes(layer, chosen) -> str:
    """Build the DATA NOTES block from the curated layer for the chosen tables."""
    chosen_u = {t.upper() for t in chosen}
    blocks = []
    if (layer.notes or "").strip():
        blocks.append(layer.notes.strip())

    enc = []
    for name in chosen:
        lt = layer.tables.get(name.upper())
        if not lt:
            continue
        for c in lt.columns.values():
            if c.encoding:
                enc.append(f"{name}.{c.name}: {c.encoding}")
            elif c.needs_cast:
                enc.append(f"{name}.{c.name} is stored as TEXT — wrap in "
                           "TRY_CONVERT(float, …) before any maths, and filter NOT NULL.")
    if enc:
        blocks.append("Column encodings / casts:\n- " + "\n- ".join(enc))

    joins = []
    for j in layer.joins:
        if j.left.upper() in chosen_u and j.right.upper() in chosen_u and j.on:
            pairs = [p for p in j.on if isinstance(p, (list, tuple)) and len(p) == 2]
            if not pairs:
                continue
            on = " AND ".join(f"{j.left}.{a} = {j.right}.{b}" for a, b in pairs)
            joins.append(f"{j.left} ⋈ {j.right} ON {on}")
    if joins:
        blocks.append("Certified joins (use ONLY these join paths):\n- " + "\n- ".join(joins))

    dng = sorted({c.name for name in chosen
                  for c in (layer.tables.get(name.upper()).columns.values()
                            if layer.tables.get(name.upper()) else [])
                  if c.do_not_group})
    if dng:
        blocks.append("Do NOT GROUP BY " + ", ".join(dng) + " (near-empty) — group by "
                      "product, analysis or ENTERED_BY (operator) instead.")
    return "\n\n".join(blocks)


def _build_from_layer(layer, datasource, question, max_tables, max_cols) -> SchemaContext:
    """Schema context from the curated semantic layer + schema-RAG retrieval."""
    from semantics.retrieval import select_tables

    chosen = select_tables(layer, question, max_tables=max_tables)
    tables = {}
    for name in chosen:
        lt = layer.tables.get(name.upper())
        if not lt:
            continue
        tables[lt.name] = [
            {"name": c.name, "type": c.dtype or "", "primary_key": False}
            for c in list(lt.columns.values())[:max_cols]]

    return SchemaContext(
        datasource_name=layer.datasource_name or datasource.name,
        source_type=datasource.source_type,
        tables=tables,
        do_not_group=layer.do_not_group_columns(),
        notes=_assemble_notes(layer, chosen),
        total_tables=len(layer.in_scope_tables()),
    )


def build_schema(datasource, database=None, question=None,
                 max_tables=None, max_cols=None, layer=None) -> SchemaContext:
    """Build a structured :class:`SchemaContext`.

    Prefers the curated semantic layer (descriptions, encodings, do_not_group,
    certified joins, schema-RAG retrieval); falls back to raw introspection +
    built-in EGPC notes when no layer is loaded for the datasource.
    """
    from connections import introspection

    max_tables = int(max_tables or getattr(settings, "DSE_TEXTTOSQL_MAX_TABLES", 14))
    max_cols = int(max_cols or getattr(settings, "DSE_TEXTTOSQL_MAX_COLS", 50))

    if layer is None:
        from semantics.layer import get_semantic_layer
        layer = get_semantic_layer(datasource)
    if layer is not None and layer.is_present():
        return _build_from_layer(layer, datasource, question, max_tables, max_cols)

    opts = datasource.options or {}

    all_tables = introspection.list_tables(datasource, database=database)
    names = [t["name"] for t in all_tables]
    core = opts.get("core_tables") or DEFAULT_CORE_TABLES
    selected = _select_tables(names, core, question, max_tables)

    tables: dict[str, list] = {}
    for name in selected:
        try:
            cols = introspection.list_columns(datasource, name, database=database)
        except Exception:  # noqa: BLE001 - one bad table must not kill the context
            continue
        tables[name] = cols[:max_cols]

    dng = {c.upper() for c in (opts.get("do_not_group") or DEFAULT_DO_NOT_GROUP)}
    return SchemaContext(
        datasource_name=datasource.name,
        source_type=datasource.source_type,
        tables=tables,
        do_not_group=dng,
        notes=opts.get("llm_notes") or DEFAULT_NOTES,
        total_tables=len(names),
    )


def build_schema_context(datasource, database=None, question=None,
                         max_tables=None, max_cols=None) -> str:
    """Back-compat string form (the generator prompt)."""
    return build_schema(datasource, database=database, question=question,
                        max_tables=max_tables, max_cols=max_cols).render()
