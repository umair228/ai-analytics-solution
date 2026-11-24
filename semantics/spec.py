"""Load/verify a curated semantic-layer YAML into the runtime models.

YAML shape (see semantics/specs/egpc_dev.yaml)::

    datasource: EGPC_DEV
    notes: |
      Use SAMPLE.LOGIN_DATE for time series. STATUS: A=Authorized, ...
    tables:
      SAMPLE:
        description: One row per sample login.
        synonyms: [sample, login, registration]
        columns:
          IN_SPEC: {description: pass/fail flag, encoding: "T=in-spec, F=off-spec"}
          NUMERIC_ENTRY: {needs_cast: true}
          INSTRUMENT: {do_not_group: true}
    joins:
      - {left: RESULT, right: SAMPLE, on: [[SAMPLE_NUMBER, SAMPLE_NUMBER]]}
    metrics:
      - {key: oos_rate_by_product, name: ..., synonyms: [...], sql: "SELECT ..."}
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from django.db import transaction


UNITS = ("count", "percent", "hours", "days", "currency", "ratio")
DIRECTIONS = ("lower_better", "higher_better", "neutral")
BANDS = ("target", "warn", "critical")
STATUSES = ("draft", "parity_checked", "certified", "live", "deprecated")
SCOPES = ("all_scope_only", "scoped:project", "dim:public")
CLASSES = ("cost", "bid_confidential", "personal")

# FROM/JOIN followed by a plain identifier rather than {{table:…}} or a subquery.
_RAW_TABLE_RE = re.compile(r"\b(?:from|join)\s+(?!\{\{|\()([A-Za-z_][\w.]*)", re.IGNORECASE)


class SpecError(Exception):
    """The spec is malformed or does not match the live schema."""


def join_on(j: dict) -> list:
    """The join's ON column pairs, tolerating YAML 1.1 coercing a bare ``on:``
    key to the boolean ``True``."""
    if "on" in j:
        return j["on"] or []
    if True in j:                      # unquoted `on:` parsed as boolean True
        return j[True] or []
    return []


def load_yaml(path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SpecError(f"{path}: top-level YAML must be a mapping.")
    return data


def validate_health(spec) -> list[str]:
    """Static checks on the Lab Health tiles.

    A tile is a scalar: one row, one number. The SQL contract cannot be enforced
    statically, but the shape of the definition can be — and a tile that silently
    renders nothing leaves a blank on the first screen a lab manager sees.
    """
    from .metric_sql import unknown_macros

    units = set(UNITS)
    directions = set(DIRECTIONS)

    errors = []
    seen = set()
    for t in spec.get("health") or []:
        key = t.get("key") or "<missing key>"
        if key in seen:
            errors.append(f"health tile '{key}' is defined more than once")
        seen.add(key)
        if not (t.get("sql") or "").strip():
            errors.append(f"health tile '{key}' has no sql")
        if not (t.get("label") or "").strip():
            errors.append(f"health tile '{key}' has no label")
        for macro in unknown_macros(t.get("sql") or ""):
            errors.append(f"health tile '{key}' uses unknown macro '{{{{{macro}:…}}}}'")
        if (unit := t.get("unit", "count")) not in units:
            errors.append(f"health tile '{key}' has unknown unit {unit!r} "
                          f"(one of: {', '.join(sorted(units))})")
        if (d := t.get("direction", "neutral")) not in directions:
            errors.append(f"health tile '{key}' has unknown direction {d!r}")
        # A target with no direction is ungradeable — we would not know which side
        # of the line is good, so the tile would be coloured arbitrarily.
        if t.get("target") is not None and t.get("direction", "neutral") == "neutral":
            errors.append(f"health tile '{key}' sets a target but direction is "
                          f"'neutral' — say whether higher or lower is better")
        errors += _band_errors(f"health tile '{key}'", t.get("direction", "neutral"),
                               {b: t.get(b) for b in BANDS})
    return errors


def _band_errors(what, direction, bands) -> list[str]:
    """RAG bands are only meaningful with a direction, must be numbers, and must be
    ordered so that warn sits between target and critical."""
    errors = []
    given = {k: v for k, v in bands.items() if v is not None}
    if not given:
        return errors
    if direction == "neutral":
        return [f"{what} sets {', '.join(sorted(given))} but direction is 'neutral'"]
    for k, v in given.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"{what} {k} must be a number (got {v!r})")
    if errors or not {"warn", "critical"} <= set(given):
        return errors
    warn, critical = given["warn"], given["critical"]
    ordered = warn < critical if direction == "lower_better" else warn > critical
    if not ordered:
        errors.append(f"{what}: with direction {direction!r}, warn ({warn}) must come "
                      f"before critical ({critical})")
    return errors


def validate_metrics(spec) -> list[str]:
    """Static checks on the certified metrics — no database needed.

    Deliberately unconditional (``apply_spec`` runs it even with verify=False): a
    metric with a typo'd macro renders as a literal ``{{nmu:x}}``, the database
    rejects it, and the runner falls through to the LLM. The failure then *looks*
    like "no metric matched" rather than "a certified metric is broken", which is
    precisely the silent degradation this phase exists to remove.
    """
    from .metric_sql import unknown_macros

    errors = []
    seen = set()
    for m in spec.get("metrics") or []:
        key = m.get("key") or "<missing key>"
        if key in seen:
            errors.append(f"metric '{key}' is defined more than once")
        seen.add(key)
        sql = m.get("sql") or ""
        if not sql.strip():
            errors.append(f"metric '{key}' has no sql")
        for macro in unknown_macros(sql):
            errors.append(f"metric '{key}' uses unknown macro '{{{{{macro}:…}}}}' "
                          f"(supported: num, table)")
        # TOP/LIMIT in the body defeats dialect portability — use `limit:` instead.
        if re.search(r"\bselect\s+(distinct\s+)?top\s+\d+", sql, re.IGNORECASE):
            errors.append(f"metric '{key}' hardcodes 'TOP n' — use the `limit:` field "
                          f"so it renders per-dialect")
        if re.search(r"\blimit\s+\d+\s*$", sql.strip(), re.IGNORECASE):
            errors.append(f"metric '{key}' hardcodes 'LIMIT n' — use the `limit:` field")
        limit = m.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            errors.append(f"metric '{key}' has a non-positive limit: {limit!r}")
        if (unit := m.get("unit")) is not None and unit not in UNITS:
            errors.append(f"metric '{key}' has unknown unit {unit!r} "
                          f"(one of: {', '.join(UNITS)})")
        direction = m.get("direction", "neutral")
        if direction not in DIRECTIONS:
            errors.append(f"metric '{key}' has unknown direction {direction!r}")
        if (status := m.get("status", "live")) not in STATUSES:
            errors.append(f"metric '{key}' has unknown status {status!r} "
                          f"(one of: {', '.join(STATUSES)})")
        thresholds = m.get("thresholds") or {}
        if not isinstance(thresholds, dict) or set(thresholds) - set(BANDS):
            errors.append(f"metric '{key}' thresholds may only set {', '.join(BANDS)}")
        else:
            errors += _band_errors(f"metric '{key}'", direction, thresholds)
        if m.get("observe") and m.get("limit") is not None:
            errors.append(f"metric '{key}' is observed nightly, so it must be a single "
                          f"number; drop its limit")
    return errors


def validate_layer(spec) -> list[str]:
    """Layer-wide rules: the time zone, and (when the spec asks for it) the scope loader.

    ``classification: {required: true}`` makes every metric and tile prove where its
    numbers come from: each table it reads must go through ``{{table:…}}`` and carry
    a ``scope:`` in ``tables:``; a metric on a ``scoped:project`` view must contain a
    ``{{scope:…}}`` filter, so a project-scoped caller can never get all-scope totals.
    """
    from .metric_sql import tables_read, uses_macro

    errors = []
    tz = spec.get("timezone")
    if tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(str(tz))
        except Exception:  # noqa: BLE001 — any lookup failure means "not a zone"
            errors.append(f"timezone {tz!r} is not an IANA time zone (e.g. Asia/Riyadh)")
    items = ([("metric", m) for m in spec.get("metrics") or []]
             + [("health tile", t) for t in spec.get("health") or []])
    if not tz and any(uses_macro(i.get("sql") or "", "local") for _, i in items):
        errors.append("a metric or tile uses {{local:…}} but the spec declares no `timezone:`")

    tables = {str(n).upper(): (d or {}) for n, d in (spec.get("tables") or {}).items()}
    for name, tdef in tables.items():
        if (scope := tdef.get("scope")) is not None and scope not in SCOPES:
            errors.append(f"table '{name}' has unknown scope {scope!r} "
                          f"(one of: {', '.join(SCOPES)})")
        for c in tdef.get("classes") or []:
            if c not in CLASSES:
                errors.append(f"table '{name}' has unknown class {c!r} "
                              f"(one of: {', '.join(CLASSES)})")

    if not (spec.get("classification") or {}).get("required"):
        return errors
    for kind, item in items:
        key, sql = item.get("key") or "<missing key>", item.get("sql") or ""
        read = tables_read(sql)
        if not read:
            errors.append(f"{kind} '{key}' reads no {{{{table:…}}}}: every table must go "
                          f"through the macro so its classification is known")
        for raw in _RAW_TABLE_RE.findall(sql):
            errors.append(f"{kind} '{key}' names table '{raw}' directly; "
                          f"use {{{{table:{raw.split('.')[-1]}}}}}")
        for tname in read:
            tdef = tables.get(tname.upper())
            if not tdef or not tdef.get("scope"):
                errors.append(f"{kind} '{key}' reads '{tname}', which has no `scope:` "
                              f"in tables: (unclassified views cannot feed a KPI)")
            elif tdef["scope"] == "scoped:project" and not uses_macro(sql, "scope"):
                errors.append(f"{kind} '{key}' reads the project-scoped view '{tname}' "
                              f"without a {{{{scope:…}}}} filter")
    return errors


def classification_of(spec, sql) -> tuple[list, bool]:
    """(classes, scoped) inherited from the tables ``sql`` reads."""
    from .metric_sql import tables_read

    tables = {str(n).upper(): (d or {}) for n, d in (spec.get("tables") or {}).items()}
    classes, scoped = set(), False
    for tname in tables_read(sql):
        tdef = tables.get(tname.upper()) or {}
        classes.update(tdef.get("classes") or [])
        scoped = scoped or tdef.get("scope") == "scoped:project"
    return sorted(classes), scoped


def verify_spec(datasource, spec) -> list[str]:
    """Return a list of drift errors (empty = the spec matches the live schema)."""
    from connections import introspection

    errors = []
    try:
        live_tables = {t["name"].upper() for t in introspection.list_tables(datasource)}
    except Exception as exc:  # noqa: BLE001
        return [f"could not introspect datasource: {exc}"]

    for tname, tdef in (spec.get("tables") or {}).items():
        if tname.upper() not in live_tables:
            errors.append(f"table '{tname}' not found in the live schema")
            continue
        try:
            live_cols = {c["name"].upper() for c in introspection.list_columns(datasource, tname)}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"could not read columns of {tname}: {exc}")
            continue
        for cname in (tdef or {}).get("columns") or {}:
            if cname.upper() not in live_cols:
                errors.append(f"column '{tname}.{cname}' not found in the live schema")

    for j in spec.get("joins") or []:
        for side in ("left", "right"):
            if (j.get(side) or "").upper() not in live_tables:
                errors.append(f"join references unknown table '{j.get(side)}'")
    errors += verify_classification(datasource, spec)
    return errors


def verify_classification(datasource, spec) -> list[str]:
    """Compare each table's ``scope:``/``classes:`` with the source's own catalogue.

    The reporting layer already classifies its views (PMSv2: rpt.view_catalog); a
    spec that disagrees with it would label a cost view as public. Configured by
    ``classification.catalog: {table, name, scope, classes}``.
    """
    catalog = (spec.get("classification") or {}).get("catalog")
    if not catalog:
        return []
    from querybuilder.executor import execute_raw_sql

    from .metric_sql import render

    try:
        cols = [catalog[k] for k in ("name", "scope", "classes")]
        sql = render(f"SELECT {cols[0]}, {cols[1]}, {cols[2]} FROM "
                     f"{{{{table:{catalog['table']}}}}}", datasource)
        rows = execute_raw_sql(datasource, sql, max_rows=5000).get("rows") or []
    except Exception as exc:  # noqa: BLE001
        return [f"could not read the classification catalogue: {exc}"]
    live = {}
    for name, scope, classes in rows:
        live[str(name).upper()] = (str(scope or ""), sorted(
            c.strip() for c in str(classes or "").split(",") if c.strip()))
    errors = []
    for tname, tdef in (spec.get("tables") or {}).items():
        tdef = tdef or {}
        if not tdef.get("scope"):
            continue
        if tname.upper() not in live:
            errors.append(f"table '{tname}' is not in the classification catalogue")
            continue
        scope, classes = live[tname.upper()]
        if tdef["scope"] != scope or sorted(tdef.get("classes") or []) != classes:
            errors.append(f"table '{tname}' is classified {tdef['scope']} "
                          f"{sorted(tdef.get('classes') or [])} in the spec but {scope} "
                          f"{classes} in the catalogue")
    return errors


@transaction.atomic
def apply_spec(datasource, spec, verify=True) -> dict:
    """Persist the spec to the semantic-layer models for ``datasource`` (replacing
    any existing one). Raises SpecError on drift when ``verify`` is True."""
    from .models import (CertifiedJoin, CertifiedMetric, HealthTile, SemanticColumn,
                         SemanticLayer, SemanticTable)

    # Static validity is checked ALWAYS — a broken metric fails silently at runtime
    # (falls through to the LLM) and a broken tile leaves a blank on the home page,
    # so both must fail loudly at load.
    errors = validate_metrics(spec) + validate_health(spec) + validate_layer(spec)
    if errors:
        raise SpecError("Semantic spec is invalid:\n  - " + "\n  - ".join(errors))

    if verify:
        errors = verify_spec(datasource, spec)
        if errors:
            raise SpecError("Semantic spec does not match the live schema:\n  - "
                            + "\n  - ".join(errors))

    SemanticTable.objects.filter(datasource=datasource).delete()      # cascades columns
    CertifiedJoin.objects.filter(datasource=datasource).delete()
    CertifiedMetric.objects.filter(datasource=datasource).delete()
    HealthTile.objects.filter(datasource=datasource).delete()
    SemanticLayer.objects.update_or_create(
        datasource=datasource, defaults={
            "notes": spec.get("notes") or "",
            "timezone": str(spec.get("timezone") or ""),
            "timezone_mssql": str(spec.get("timezone_mssql") or "")})

    n_tables = n_cols = n_joins = n_metrics = n_health = 0
    for tname, tdef in (spec.get("tables") or {}).items():
        tdef = tdef or {}
        st = SemanticTable.objects.create(
            datasource=datasource, name=tname,
            description=tdef.get("description", ""),
            in_scope=bool(tdef.get("in_scope", True)),
            row_count=tdef.get("row_count"),
            synonyms=tdef.get("synonyms") or [])
        n_tables += 1
        for cname, cdef in (tdef.get("columns") or {}).items():
            cdef = cdef or {}
            SemanticColumn.objects.create(
                table=st, name=cname,
                description=cdef.get("description", ""),
                dtype=cdef.get("dtype", ""),
                encoding=cdef.get("encoding", ""),
                null_fraction=cdef.get("null_fraction"),
                needs_cast=bool(cdef.get("needs_cast", False)),
                do_not_group=bool(cdef.get("do_not_group", False)),
                synonyms=cdef.get("synonyms") or [])
            n_cols += 1

    for j in spec.get("joins") or []:
        CertifiedJoin.objects.create(
            datasource=datasource, left_table=j["left"], right_table=j["right"],
            on=join_on(j), description=j.get("description", ""))
        n_joins += 1

    for m in spec.get("metrics") or []:
        classes, scoped = classification_of(spec, m["sql"])
        CertifiedMetric.objects.create(
            datasource=datasource, key=m["key"], name=m.get("name", m["key"]),
            description=m.get("description", ""), sql=m["sql"],
            row_limit=m.get("limit"),
            synonyms=m.get("synonyms") or [],
            kpi_id=str(m.get("kpi_id") or ""), unit=m.get("unit") or "",
            direction=m.get("direction", "neutral"), owner=m.get("owner", ""),
            brd_ref=m.get("brd_ref", ""), thresholds=m.get("thresholds") or {},
            status=m.get("status", "live"), observe=bool(m.get("observe", False)),
            classes=classes, scoped=scoped)
        n_metrics += 1

    for i, t in enumerate(spec.get("health") or []):
        HealthTile.objects.create(
            datasource=datasource, key=t["key"], label=t["label"],
            description=t.get("description", ""), sql=t["sql"],
            unit=t.get("unit", "count"), direction=t.get("direction", "neutral"),
            target=t.get("target"), warn=t.get("warn"), critical=t.get("critical"),
            roles=t.get("roles") or [], position=t.get("position", i),
            classes=classification_of(spec, t["sql"])[0],
            scoped=classification_of(spec, t["sql"])[1])
        n_health += 1

    from .layer import invalidate
    invalidate(datasource)

    return {"tables": n_tables, "columns": n_cols, "joins": n_joins,
            "metrics": n_metrics, "health": n_health}
