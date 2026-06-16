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
    from .models import (CertifiedJoin, CertifiedMetric, SemanticColumn,
                         SemanticLayer, SemanticTable)

    if verify:
        errors = verify_spec(datasource, spec)
        if errors:
            raise SpecError("Semantic spec does not match the live schema:\n  - "
                            + "\n  - ".join(errors))

    SemanticTable.objects.filter(datasource=datasource).delete()      # cascades columns
    CertifiedJoin.objects.filter(datasource=datasource).delete()
    CertifiedMetric.objects.filter(datasource=datasource).delete()
    SemanticLayer.objects.update_or_create(
        datasource=datasource, defaults={"notes": spec.get("notes") or ""})

    n_tables = n_cols = n_joins = n_metrics = 0
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
            synonyms=m.get("synonyms") or [])
        n_metrics += 1

    return {"tables": n_tables, "columns": n_cols, "joins": n_joins, "metrics": n_metrics}
