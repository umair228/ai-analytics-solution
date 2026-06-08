"""Contextual intelligence & relationship discovery (Phase 4).

Implements the MOM "identify hidden relationships" / "cross-functional analysis"
asks:

  * :func:`discover_relationships` — scan ONE dataset for the strongest hidden
    relationships among its columns: numeric↔numeric correlations, categorical↔
    categorical associations (Cramér's V), and category→numeric effects (η² from a
    one-way ANOVA scan). Returns a ranked, plain-English list.
  * :func:`suggest_links` — given several datasets' metadata, find columns they
    SHARE (by normalised name), suggesting how to join lab / operational /
    maintenance / calibration data for cross-functional analysis.

Deterministic; JSON-safe via ``engine._num``.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
from scipy import stats

from .engine import AnalyticsError, _num

_MAX_NUMERIC = 15
_MAX_CAT = 12
_MAX_CARDINALITY = 30


def _numeric_cols(df):
    out = []
    for c in df.columns:
        if pd.to_numeric(df[c], errors="coerce").notna().sum() >= 0.6 * len(df):
            out.append(c)
    return out[:_MAX_NUMERIC]


def _categorical_cols(df):
    out = []
    for c in df.columns:
        if pd.to_numeric(df[c], errors="coerce").notna().sum() >= 0.6 * len(df):
            continue
        nun = df[c].nunique(dropna=True)
        if 1 < nun <= _MAX_CARDINALITY:
            out.append(c)
    return out[:_MAX_CAT]


def _eta_squared(values_by_group):
    grand = np.concatenate(values_by_group)
    ss_total = float(np.sum((grand - grand.mean()) ** 2))
    if not ss_total:
        return 0.0
    ss_between = sum(len(g) * (g.mean() - grand.mean()) ** 2 for g in values_by_group)
    return ss_between / ss_total


def discover_relationships(df, min_corr=0.3, min_cramers=0.2, min_eta=0.06,
                           max_results=25):
    """Find and rank the strongest relationships hidden in a dataset."""
    n = len(df)
    if n < 5:
        raise AnalyticsError("Need at least 5 rows to discover relationships.")
    num_cols = _numeric_cols(df)
    cat_cols = _categorical_cols(df)
    rels = []

    # ── numeric ↔ numeric (Pearson) ──
    if len(num_cols) >= 2:
        num_df = df[num_cols].apply(pd.to_numeric, errors="coerce")
        for i in range(len(num_cols)):
            for j in range(i + 1, len(num_cols)):
                a, b = num_cols[i], num_cols[j]
                pair = num_df[[a, b]].dropna()
                if len(pair) < 5 or pair[a].nunique() < 2 or pair[b].nunique() < 2:
                    continue
                r, p = stats.pearsonr(pair[a].to_numpy(), pair[b].to_numpy())
                if abs(r) >= min_corr:
                    rels.append({
                        "type": "numeric_correlation", "columns": [a, b],
                        "metric": "pearson_r", "value": _num(float(r)),
                        "p_value": _num(float(p)), "strength": abs(float(r)),
                        "description": f"{a} and {b} move together (r={r:.2f}).",
                    })

    # ── categorical ↔ categorical (Cramér's V) ──
    for i in range(len(cat_cols)):
        for j in range(i + 1, len(cat_cols)):
            a, b = cat_cols[i], cat_cols[j]
            table = pd.crosstab(df[a].astype(str), df[b].astype(str))
            if table.shape[0] < 2 or table.shape[1] < 2:
                continue
            chi2, p, _, _ = stats.chi2_contingency(table)
            denom = n * (min(table.shape) - 1)
            v = float(np.sqrt(chi2 / denom)) if denom else 0.0
            if v >= min_cramers:
                rels.append({
                    "type": "categorical_association", "columns": [a, b],
                    "metric": "cramers_v", "value": _num(v),
                    "p_value": _num(float(p)), "strength": v,
                    "description": f"{a} and {b} are associated (Cramér's V={v:.2f}).",
                })

    # ── category → numeric (η² from one-way ANOVA) ──
    for cat in cat_cols:
        for num in num_cols:
            vals = pd.to_numeric(df[num], errors="coerce")
            groups = [g.dropna().to_numpy(float)
                      for _, g in vals.groupby(df[cat].astype(str))
                      if g.dropna().size >= 3]
            if len(groups) < 2:
                continue
            eta = _eta_squared(groups)
            if eta >= min_eta:
                f, p = stats.f_oneway(*groups)
                rels.append({
                    "type": "category_drives_numeric",
                    "columns": [cat, num], "metric": "eta_squared",
                    "value": _num(eta), "p_value": _num(float(p)), "strength": eta,
                    "description": (f"{num} differs by {cat} "
                                    f"(η²={eta:.2f} — {cat} explains "
                                    f"{eta*100:.0f}% of {num}'s variance)."),
                })

    rels.sort(key=lambda r: -r["strength"])
    top = rels[:max_results]
    for r in top:
        r.pop("strength", None)
    return {
        "test": "relationship_discovery",
        "n": n, "numeric_columns": num_cols, "categorical_columns": cat_cols,
        "relationship_count": len(rels),
        "relationships": top,
        "interpretation": (
            f"Found {len(rels)} notable relationship(s) among "
            f"{len(num_cols)} numeric + {len(cat_cols)} categorical columns. "
            + (f"Strongest: {top[0]['description']}" if top else
               "No strong relationships at the current thresholds.")
        ),
    }


# --------------------------------------------------------------------------
# Cross-dataset linking
# --------------------------------------------------------------------------
def _norm(col):
    return re.sub(r"[\s_\-]+", "", str(col).strip().lower())


def suggest_links(datasets_meta):
    """Given ``[{"id","name","columns":[...]}, ...]`` find columns shared across
    datasets (normalised name match) — candidate join keys for cross-functional
    analysis. Returns the links sorted by how many datasets share each column."""
    by_norm = {}  # norm_name -> {"display": original, "datasets": [(id,name,col)]}
    for ds in datasets_meta or []:
        for col in ds.get("columns") or []:
            key = _norm(col)
            if not key:
                continue
            entry = by_norm.setdefault(key, {"column": col, "datasets": []})
            entry["datasets"].append({"id": ds.get("id"), "name": ds.get("name"),
                                      "column": col})
    links = []
    for key, entry in by_norm.items():
        ds_list = entry["datasets"]
        if len({d["id"] for d in ds_list}) >= 2:  # shared by 2+ datasets
            links.append({
                "shared_column": entry["column"],
                "dataset_count": len({d["id"] for d in ds_list}),
                "datasets": ds_list,
            })
    links.sort(key=lambda l: -l["dataset_count"])
    return {
        "test": "dataset_link_suggestions",
        "datasets_considered": len(datasets_meta or []),
        "link_count": len(links),
        "links": links[:40],
        "interpretation": (
            f"Found {len(links)} column(s) shared across 2+ datasets — candidate "
            "join keys for cross-functional analysis. "
            + (f"Most common: '{links[0]['shared_column']}' "
               f"(in {links[0]['dataset_count']} datasets)." if links else "")
        ),
    }
