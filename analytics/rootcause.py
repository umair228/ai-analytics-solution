"""Root-cause analysis & pattern discovery (Phase 3).

Implements the MOM "root cause analysis" + "association rule mining, clustering,
machine learning" asks:

  * :func:`driver_analysis` — given an OUTCOME (e.g. out-of-spec, rejected, late
    TAT), rank the factors most associated with it: a per-value LIFT table
    ("out-of-spec rate is 3.2× higher when instrument=GC-401") plus a
    RandomForest feature-importance ranking of which COLUMNS matter.
  * :func:`cluster_analysis` — KMeans / DBSCAN clustering with per-cluster
    profiles, to find natural groupings of samples/results.
  * :func:`association_rules_mining` — apriori association rules over categorical
    columns (optionally targeting an outcome value).

All deterministic (fixed random_state) and JSON-safe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import AnalyticsError, _num

RANDOM_STATE = 42
_MAX_CARDINALITY = 60   # skip categoricals with more distinct values than this
_TOP_VALUES = 12        # per-column value cap for association mining


# --------------------------------------------------------------------------
# Outcome definition
# --------------------------------------------------------------------------
def _build_outcome(df, target_column, target_value=None, op=None, threshold=None):
    if target_column not in df.columns:
        raise AnalyticsError(f"target_column '{target_column}' not found.")
    col = df[target_column]
    if target_value is not None and target_value != "":
        y = col.astype(str) == str(target_value)
        desc = f"{target_column} == '{target_value}'"
    elif op and threshold is not None:
        num = pd.to_numeric(col, errors="coerce")
        cmp = {"gt": num > threshold, "gte": num >= threshold,
               "lt": num < threshold, "lte": num <= threshold,
               "eq": num == threshold}.get(op)
        if cmp is None:
            raise AnalyticsError("op must be one of gt/gte/lt/lte/eq.")
        y = cmp.fillna(False)
        desc = f"{target_column} {op} {threshold}"
    else:
        raise AnalyticsError(
            "Define the outcome: pass target_value (categorical) OR op+threshold (numeric)."
        )
    y = y.astype(int)
    if y.sum() == 0:
        raise AnalyticsError(f"No rows match the outcome ({desc}).")
    if y.sum() == len(y):
        raise AnalyticsError(f"All rows match the outcome ({desc}) — nothing to contrast.")
    return y, desc


def _candidate_features(df, target_column, feature_columns):
    if feature_columns:
        cols = [c for c in feature_columns if c in df.columns and c != target_column]
    else:
        cols = []
        for c in df.columns:
            if c == target_column:
                continue
            nunique = df[c].nunique(dropna=True)
            # keep categoricals/low-card numerics; drop ids (≈unique) and free text
            if 1 < nunique <= _MAX_CARDINALITY:
                cols.append(c)
    if not cols:
        raise AnalyticsError("No suitable factor columns found (all too unique or constant).")
    return cols


# --------------------------------------------------------------------------
# Driver analysis (lift table + RandomForest importance)
# --------------------------------------------------------------------------
def driver_analysis(df, target_column, target_value=None, op=None, threshold=None,
                    feature_columns=None, min_support=0.02, max_factors=25):
    y, outcome_desc = _build_outcome(df, target_column, target_value, op, threshold)
    cols = _candidate_features(df, target_column, feature_columns)
    n = len(df)
    base = float(y.mean())

    # ---- per-value lift table ----
    factors = []
    for col in cols:
        series = df[col].astype(str)
        grp = y.groupby(series)
        rates, counts = grp.mean(), grp.count()
        for val in rates.index:
            cnt = int(counts[val])
            support = cnt / n
            if support < min_support:
                continue
            rate = float(rates[val])
            factors.append({
                "factor": col, "value": str(val), "count": cnt,
                "support": _num(support), "outcome_rate": _num(rate),
                "lift": _num(rate / base) if base else None,
            })
    factors.sort(key=lambda f: (f["lift"] is None, -(f["lift"] or 0)))
    top_factors = factors[:max_factors]

    # ---- RandomForest feature importance (which COLUMN matters) ----
    importance = _rf_importance(df, cols, y)

    top = top_factors[0] if top_factors else None
    return {
        "test": "driver_analysis",
        "outcome": outcome_desc,
        "n": n,
        "base_rate": _num(base),
        "outcome_count": int(y.sum()),
        "top_factors": top_factors,
        "feature_importance": importance,
        "interpretation": (
            f"Outcome '{outcome_desc}' occurs at a base rate of {base*100:.1f}%. "
            + (f"Strongest association: {top['factor']}={top['value']} "
               f"(rate {top['outcome_rate']*100:.1f}%, {top['lift']}× the base, "
               f"n={top['count']}). " if top else "")
            + (f"Most influential factor overall: {importance[0]['factor']} "
               f"(importance {importance[0]['importance']})." if importance else "")
        ),
    }


def _rf_importance(df, cols, y):
    try:
        from sklearn.ensemble import RandomForestClassifier
    except Exception:  # noqa: BLE001
        return []
    parts, source = [], []
    for col in cols:
        s = df[col]
        num = pd.to_numeric(s, errors="coerce")
        if num.notna().sum() >= 0.8 * len(s):  # treat as numeric
            parts.append(num.fillna(num.median()).to_frame(col))
            source.append(col)
        else:
            dummies = pd.get_dummies(s.astype(str), prefix=col)
            parts.append(dummies)
            source.extend([col] * dummies.shape[1])
    if not parts:
        return []
    X = pd.concat(parts, axis=1).fillna(0)
    clf = RandomForestClassifier(n_estimators=200, random_state=RANDOM_STATE,
                                 class_weight="balanced", n_jobs=-1)
    clf.fit(X.to_numpy(float), y.to_numpy())
    agg = {}
    for src, imp in zip(source, clf.feature_importances_):
        agg[src] = agg.get(src, 0.0) + float(imp)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1])
    return [{"factor": k, "importance": _num(v)} for k, v in ranked]


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------
def cluster_analysis(df, columns=None, method="kmeans", k=4):
    from sklearn.preprocessing import StandardScaler

    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise AnalyticsError(f"Columns not found: {', '.join(missing)}.")
        cols = list(columns)
    else:
        cols = [c for c in df.columns
                if pd.to_numeric(df[c], errors="coerce").notna().sum() >= max(10, 0.5 * len(df))]
    if len(cols) < 2:
        raise AnalyticsError("Need at least 2 numeric columns to cluster.")

    numeric = df[cols].apply(pd.to_numeric, errors="coerce").dropna()
    if len(numeric) < 10:
        raise AnalyticsError("Need at least 10 complete numeric rows.")
    X = StandardScaler().fit_transform(numeric.to_numpy(float))

    method = (method or "kmeans").lower()
    if method == "kmeans":
        from sklearn.cluster import KMeans
        k = int(min(max(k or 4, 2), 12))
        model = KMeans(n_clusters=k, random_state=RANDOM_STATE, n_init=10)
        labels = model.fit_predict(X)
    elif method == "dbscan":
        from sklearn.cluster import DBSCAN
        labels = DBSCAN().fit_predict(X)
    else:
        raise AnalyticsError("method must be 'kmeans' or 'dbscan'.")

    sil = None
    uniq = set(labels) - {-1}
    if len(uniq) > 1:
        try:
            from sklearn.metrics import silhouette_score
            sil = float(silhouette_score(X, labels))
        except Exception:  # noqa: BLE001
            sil = None

    profiles = []
    work = numeric.copy()
    work["_cluster"] = labels
    for cl, sub in work.groupby("_cluster"):
        profiles.append({
            "cluster": int(cl),
            "size": int(len(sub)),
            "is_noise": bool(cl == -1),
            "means": {c: _num(float(sub[c].mean())) for c in cols},
        })
    profiles.sort(key=lambda p: -p["size"])
    return {
        "test": "clustering",
        "method": method,
        "feature_columns": cols,
        "n": int(len(numeric)),
        "n_clusters": len([p for p in profiles if not p["is_noise"]]),
        "silhouette": _num(sil) if sil is not None else None,
        "clusters": profiles,
        "interpretation": (
            f"{method.upper()} found {len([p for p in profiles if not p['is_noise']])} "
            f"cluster(s) over {len(cols)} features"
            + (f" (silhouette={sil:.3f})." if sil is not None else ".")
        ),
    }


# --------------------------------------------------------------------------
# Association rule mining
# --------------------------------------------------------------------------
def association_rules_mining(df, columns, target_column=None, target_value=None,
                            min_support=0.05, min_confidence=0.5, max_len=3):
    from mlxtend.frequent_patterns import apriori, association_rules

    cols = [c for c in (columns or []) if c in df.columns]
    if not cols:
        raise AnalyticsError("Provide at least one categorical 'columns' entry.")

    onehot_parts = []
    for col in cols + ([target_column] if target_column and target_column not in cols else []):
        if col not in df.columns:
            continue
        s = df[col].astype(str)
        top = s.value_counts().head(_TOP_VALUES).index
        s = s.where(s.isin(top), other="(other)")
        onehot_parts.append(pd.get_dummies(s, prefix=col))
    onehot = pd.concat(onehot_parts, axis=1).astype(bool)
    if onehot.shape[1] > 200:
        raise AnalyticsError("Too many category values — narrow 'columns'.")

    min_support = min(max(float(min_support or 0.05), 0.001), 0.95)
    freq = apriori(onehot, min_support=min_support, use_colnames=True,
                   max_len=int(max_len or 3))
    if freq.empty:
        return {"test": "association_rules", "rule_count": 0, "rules": [],
                "interpretation": "No frequent itemsets at this support — lower min_support."}
    rules = association_rules(freq, metric="confidence",
                             min_threshold=float(min_confidence or 0.5))
    if rules.empty:
        return {"test": "association_rules", "rule_count": 0, "rules": [],
                "interpretation": "No rules at this confidence — lower min_confidence."}

    if target_column and target_value is not None:
        tag = f"{target_column}_{target_value}"
        rules = rules[rules["consequents"].apply(lambda s: tag in set(s))]

    rules = rules.sort_values("lift", ascending=False).head(25)
    out = [{
        "antecedents": sorted(r.antecedents),
        "consequents": sorted(r.consequents),
        "support": _num(float(r.support)),
        "confidence": _num(float(r.confidence)),
        "lift": _num(float(r.lift)),
    } for r in rules.itertuples()]
    return {
        "test": "association_rules",
        "columns": cols,
        "rule_count": len(out),
        "rules": out,
        "interpretation": (
            f"Found {len(out)} association rule(s)"
            + (f" leading to {target_column}={target_value}" if target_column and target_value else "")
            + (f". Strongest: {{{', '.join(out[0]['antecedents'])}}} ⟹ "
               f"{{{', '.join(out[0]['consequents'])}}} (lift {out[0]['lift']}, "
               f"confidence {out[0]['confidence']})." if out else ".")
        ),
    }
