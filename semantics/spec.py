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

    units = {"count", "percent", "hours"}
    directions = {"lower_better", "higher_better", "neutral"}

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
    return errors


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
    errors = validate_metrics(spec) + validate_health(spec)
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
        datasource=datasource, defaults={"notes": spec.get("notes") or ""})

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
        CertifiedMetric.objects.create(
            datasource=datasource, key=m["key"], name=m.get("name", m["key"]),
            description=m.get("description", ""), sql=m["sql"],
            row_limit=m.get("limit"),
            synonyms=m.get("synonyms") or [])
        n_metrics += 1

    for i, t in enumerate(spec.get("health") or []):
        HealthTile.objects.create(
            datasource=datasource, key=t["key"], label=t["label"],
            description=t.get("description", ""), sql=t["sql"],
            unit=t.get("unit", "count"), direction=t.get("direction", "neutral"),
            target=t.get("target"), roles=t.get("roles") or [],
            position=t.get("position", i))
        n_health += 1

    from .layer import invalidate
    invalidate(datasource)

    return {"tables": n_tables, "columns": n_cols, "joins": n_joins,
            "metrics": n_metrics, "health": n_health}
