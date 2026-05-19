"""Data-quality profiling — a per-dataset profile with distributions,
quality flags and completeness scoring. Powers the Datasets "Profile" tab.
"""
import math

import pandas as pd


def _num(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return round(result, 4)


def _histogram(series, bins=10):
    """Build a simple equal-width histogram for a numeric series."""
    clean = series.dropna()
    if clean.empty:
        return []
    lo, hi = float(clean.min()), float(clean.max())
    if lo == hi:
        return [{"bin": f"{_num(lo)}", "count": int(len(clean))}]
    counts, edges = _binned(clean, lo, hi, bins)
    out = []
    for i, count in enumerate(counts):
        left, right = edges[i], edges[i + 1]
        out.append({
            "bin": f"{_num(left)}–{_num(right)}",
            "count": int(count),
        })
    return out


def _binned(clean, lo, hi, bins):
    width = (hi - lo) / bins
    edges = [lo + width * i for i in range(bins + 1)]
    counts = [0] * bins
    for value in clean:
        idx = int((value - lo) / width)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1
    return counts, edges


def _profile_column(series):
    name = str(series.name)
    non_null = int(series.notna().sum())
    null_count = int(series.isna().sum())
    total = len(series)
    distinct = int(series.nunique(dropna=True))

    numeric = pd.to_numeric(series, errors="coerce")
    is_numeric = non_null > 0 and int(numeric.notna().sum()) >= non_null * 0.85
    is_date = False
    if not is_numeric and non_null and any(
        k in name.lower() for k in ("date", "_on", "_at", "month", "time", "year")
    ):
        is_date = int(pd.to_datetime(series, errors="coerce").notna().sum()) \
            >= non_null * 0.7

    entry = {
        "column": name,
        "type": "numeric" if is_numeric else ("date" if is_date else "text"),
        "count": non_null,
        "null_count": null_count,
        "null_pct": _num(null_count / total * 100) if total else 0,
        "distinct": distinct,
        "unique_pct": _num(distinct / non_null * 100) if non_null else 0,
        "flags": [],
    }

    if is_numeric:
        clean = numeric.dropna()
        q1 = clean.quantile(0.25) if len(clean) else 0
        q3 = clean.quantile(0.75) if len(clean) else 0
        iqr = q3 - q1
        outliers = int(
            ((clean < q1 - 1.5 * iqr) | (clean > q3 + 1.5 * iqr)).sum()
        ) if iqr else 0
        entry.update({
            "min": _num(clean.min()), "max": _num(clean.max()),
            "mean": _num(clean.mean()), "median": _num(clean.median()),
            "std": _num(clean.std()),
            "p25": _num(q1), "p75": _num(q3),
            "outlier_count": outliers,
            "histogram": _histogram(clean),
        })
    else:
        counts = series.dropna().astype(str).value_counts()
        top = counts.head(8)
        entry["top_values"] = [
            {"value": str(k), "count": int(v),
             "pct": _num(v / non_null * 100) if non_null else 0}
            for k, v in top.items()
        ]

    # Quality flags
    if total and null_count == total:
        entry["flags"].append("empty")
    elif entry["null_pct"] and entry["null_pct"] >= 20:
        entry["flags"].append("high_nulls")
    if distinct == 1 and non_null:
        entry["flags"].append("constant")
    if non_null and distinct == non_null and not is_numeric:
        entry["flags"].append("all_unique")
    return entry


def profile_dataset(df):
    """Return a full profile of the dataframe."""
    rows = int(len(df))
    cols = int(len(df.columns))
    total_cells = rows * cols
    null_cells = int(df.isna().sum().sum()) if cols else 0
    try:
        duplicate_rows = int(df.duplicated().sum())
    except Exception:  # noqa: BLE001 - unhashable cells
        duplicate_rows = 0

    columns = [_profile_column(df[c]) for c in df.columns]
    return {
        "row_count": rows,
        "column_count": cols,
        "duplicate_rows": duplicate_rows,
        "total_cells": total_cells,
        "null_cells": null_cells,
        "completeness": _num((1 - null_cells / total_cells) * 100)
        if total_cells else 100,
        "numeric_columns": sum(c["type"] == "numeric" for c in columns),
        "text_columns": sum(c["type"] == "text" for c in columns),
        "date_columns": sum(c["type"] == "date" for c in columns),
        "columns": columns,
    }
