"""Extract → load → reconcile: mirror LIMS tables into the analytics replica.

Copy is done with pandas (read_sql → to_sql) so it is dialect-agnostic. Two modes:

  * **full**       — replace the whole target table (the nightly reconcile that
                     also catches hard-deletes / back-dated edits a watermark misses).
  * **incremental** — pull only rows whose CHANGED_ON exceeds the stored high-
                     watermark, then upsert by primary key (delete-then-append).

Reference tables (no watermark) are always full. Per-table failures are captured
on the state row and do not abort the run.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import bindparam, inspect, text
from django.utils import timezone

from connections.engine import get_engine

from .models import ReplicationState
from .sql_dialect import quote_ident, table_ref
from .tables import table_specs


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _native(v):
    """Coerce numpy/pandas scalars to plain Python so DB drivers accept them."""
    if v is None or v is pd.NaT:
        return None
    try:
        if not isinstance(v, (list, tuple, dict)) and pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            return v
    return v


def _watermark_str(mx):
    """Canonical, chronologically-sortable string for the high-watermark.

    Watermark columns MUST be timestamps (the EGPC tables use CHANGED_ON); a
    datetime is rendered ISO-8601 (sortable, and safely castable back by the DB),
    and an already-ISO text timestamp passes through unchanged. Non-sortable types
    (e.g. raw integers / M-D-Y text) are intentionally unsupported — keep watermark
    columns to ISO/timestamp values.
    """
    try:
        if mx is None or mx is pd.NaT or pd.isna(mx):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(mx, "isoformat"):  # pandas Timestamp / datetime
        return mx.isoformat(sep=" ")
    return str(mx)


def _source_table_ref(source, table):
    d = source.source_type
    schema = source.schema_name or ("dbo" if d == "mssql" else None)
    return table_ref(table, schema, d)


def _ensure_schema(engine, schema, dialect):
    if schema and dialect == "postgres":
        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema, dialect)}"))


def _table_exists(conn, table, schema):
    try:
        return inspect(conn).has_table(table, schema=schema)
    except Exception:  # noqa: BLE001
        return False


def _key_tuples(df, keys):
    return [tuple(_native(v) for v in row)
            for row in df[keys].itertuples(index=False, name=None)]


def _delete_keys(conn, ref, keys, key_tuples, dialect):
    if not keys or not key_tuples:
        return
    if len(keys) == 1:
        col = quote_ident(keys[0], dialect)
        stmt = text(f"DELETE FROM {ref} WHERE {col} IN :ks").bindparams(
            bindparam("ks", expanding=True))
        vals = [t[0] for t in key_tuples]
    else:
        cols = "(" + ", ".join(quote_ident(k, dialect) for k in keys) + ")"
        stmt = text(f"DELETE FROM {ref} WHERE {cols} IN :ks").bindparams(
            bindparam("ks", expanding=True))
        vals = [tuple(t) for t in key_tuples]
    for batch in _chunks(vals, 500):
        conn.execute(stmt, {"ks": batch})


def _fail(state, msg):
    state.last_error = msg[:2000]
    state.save(update_fields=["last_error", "updated_at"])
    return {"table": state.table_name, "error": msg}


def ensure_states(source, target):
    """Create/sync a ReplicationState per configured table for this pair."""
    states = []
    for spec in table_specs():
        st, _ = ReplicationState.objects.get_or_create(
            source=source, target=target, table_name=spec["table"],
            defaults={
                "key_columns": spec.get("keys") or [],
                "watermark_column": spec.get("watermark") or "",
                "cadence": spec.get("cadence") or "hourly",
            },
        )
        keys = spec.get("keys") or []
        wm = spec.get("watermark") or ""
        cad = spec.get("cadence") or "hourly"
        if (st.key_columns, st.watermark_column, st.cadence) != (keys, wm, cad):
            st.key_columns, st.watermark_column, st.cadence = keys, wm, cad
            st.save(update_fields=["key_columns", "watermark_column", "cadence", "updated_at"])
        states.append(st)
    return states


def replicate_table(state, *, mode="incremental", target_schema=None,
                    chunksize=5000, dry_run=False) -> dict:
    """Mirror one table per ``state``; update the state row. Never raises."""
    source, target = state.source, state.target
    src_dialect, tgt_dialect = source.source_type, target.source_type
    table, keys = state.table_name, (state.key_columns or [])
    wm_col = state.watermark_column or ""

    do_incremental = bool(mode == "incremental" and wm_col and state.high_watermark)

    src_ref = _source_table_ref(source, table)
    tgt_ref = table_ref(table, target_schema, tgt_dialect)
    select_sql = f"SELECT * FROM {src_ref}"
    params = None
    if do_incremental:
        select_sql += f" WHERE {quote_ident(wm_col, src_dialect)} > :wm"
        params = {"wm": state.high_watermark}

    if dry_run:
        return {"table": table, "mode": "incremental" if do_incremental else "full",
                "sql": select_sql, "dry_run": True}

    src_engine, tgt_engine = get_engine(source), get_engine(target)

    try:
        df = pd.read_sql(text(select_sql), src_engine, params=params)
    except Exception as exc:  # noqa: BLE001
        return _fail(state, f"extract failed: {exc}")

    n = len(df)
    new_wm = None
    if wm_col and wm_col in df.columns and n:
        try:
            new_wm = _watermark_str(df[wm_col].dropna().max())
        except Exception:  # noqa: BLE001
            new_wm = None

    try:
        _ensure_schema(tgt_engine, target_schema, tgt_dialect)
        if do_incremental:
            if n:
                with tgt_engine.begin() as conn:
                    if _table_exists(conn, table, target_schema):
                        _delete_keys(conn, tgt_ref, keys, _key_tuples(df, keys), tgt_dialect)
                    df.to_sql(table, conn, schema=target_schema, if_exists="append",
                              index=False, chunksize=chunksize)
        else:
            with tgt_engine.begin() as conn:
                df.to_sql(table, conn, schema=target_schema, if_exists="replace",
                          index=False, chunksize=chunksize)
    except Exception as exc:  # noqa: BLE001
        return _fail(state, f"load failed: {exc}")

    now = timezone.now()
    if new_wm and (not state.high_watermark or new_wm > state.high_watermark):
        state.high_watermark = new_wm[:64]
    if do_incremental:
        state.last_incremental_at = now
    else:
        state.last_full_at = now
        state.last_incremental_at = now
    try:
        with tgt_engine.connect() as conn:
            state.row_count = int(conn.execute(text(f"SELECT COUNT(*) FROM {tgt_ref}")).scalar() or 0)
    except Exception:  # noqa: BLE001
        state.row_count = n if not do_incremental else state.row_count
    state.last_error = ""
    state.save()
    return {"table": table, "mode": "incremental" if do_incremental else "full",
            "rows_pulled": n, "row_count": state.row_count,
            "high_watermark": state.high_watermark}


def replicate_all(source, target, *, mode="incremental", target_schema=None,
                  only_tables=None, dry_run=False) -> list[dict]:
    states = ensure_states(source, target)
    if only_tables:
        want = {t.upper() for t in only_tables}
        states = [s for s in states if s.table_name.upper() in want]
    return [replicate_table(s, mode=mode, target_schema=target_schema, dry_run=dry_run)
            for s in states]


def stale_states(factor=2.0, **filters):
    """ReplicationState rows that are overdue / errored — feeds the alert."""
    qs = ReplicationState.objects.all()
    if filters:
        qs = qs.filter(**filters)
    return [s for s in qs if s.is_stale(factor=factor)]
