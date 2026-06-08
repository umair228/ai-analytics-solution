"""Dedicated ML anomaly-detection models (Phase 3).

Complements the univariate z-score / IQR rules in :mod:`analytics.predict` with
*multivariate* models that flag rows anomalous across several columns at once
(e.g. a sample whose pH is fine and turbidity is fine but the COMBINATION is
unusual). Uses scikit-learn IsolationForest and LocalOutlierFactor.

Deterministic: ``random_state`` is fixed so the same data + params give the same
flags every run — important for a QC audit trail.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import AnalyticsError, _num

RANDOM_STATE = 42
MAX_FLAGGED = 200


def _feature_matrix(df, columns):
    """Build a scaled numeric matrix from the requested columns (default: all
    numeric columns). Returns (X_scaled, used_columns, valid_row_index)."""
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise AnalyticsError(f"Columns not found: {', '.join(missing)}.")
        cols = list(columns)
    else:
        cols = [c for c in df.columns
                if pd.to_numeric(df[c], errors="coerce").notna().sum() >= max(10, 0.5 * len(df))]
    if len(cols) < 1:
        raise AnalyticsError("No usable numeric columns for anomaly detection.")

    numeric = df[cols].apply(pd.to_numeric, errors="coerce")
    valid = numeric.dropna()
    if len(valid) < 10:
        raise AnalyticsError("Need at least 10 complete numeric rows.")

    from sklearn.preprocessing import StandardScaler
    X = StandardScaler().fit_transform(valid.to_numpy(float))
    return X, cols, valid.index, valid


def _flagged_rows(valid_df, cols, mask, scores, index):
    rows = []
    for pos, idx in enumerate(index):
        if mask[pos]:
            rows.append({
                "row": int(idx),
                "score": _num(float(scores[pos])),
                "values": {c: _num(float(valid_df.iloc[pos][c]))
                           if pd.notna(valid_df.iloc[pos][c]) else None for c in cols},
            })
    rows.sort(key=lambda r: (r["score"] if r["score"] is not None else 0))
    return rows[:MAX_FLAGGED]


def isolation_forest(df, columns=None, contamination=0.05):
    from sklearn.ensemble import IsolationForest

    X, cols, index, valid = _feature_matrix(df, columns)
    contamination = float(contamination or 0.05)
    contamination = min(max(contamination, 0.001), 0.5)
    model = IsolationForest(contamination=contamination, random_state=RANDOM_STATE,
                            n_estimators=200)
    pred = model.fit_predict(X)
    scores = model.decision_function(X)  # lower = more anomalous
    mask = pred == -1
    rows = _flagged_rows(valid, cols, mask, scores, index)
    return {
        "test": "isolation_forest",
        "feature_columns": cols,
        "n": int(len(valid)),
        "contamination": contamination,
        "anomaly_count": int(mask.sum()),
        "anomalies": rows,
        "interpretation": (
            f"IsolationForest flagged {int(mask.sum())} of {len(valid)} rows "
            f"({mask.mean()*100:.1f}%) as multivariate anomalies across "
            f"{len(cols)} feature(s): {', '.join(cols[:6])}"
            + ("…" if len(cols) > 6 else "") + "."
        ),
    }


def local_outlier_factor(df, columns=None, contamination=0.05, n_neighbors=20):
    from sklearn.neighbors import LocalOutlierFactor

    X, cols, index, valid = _feature_matrix(df, columns)
    contamination = min(max(float(contamination or 0.05), 0.001), 0.5)
    n_neighbors = int(min(max(n_neighbors, 5), max(5, len(valid) - 1)))
    model = LocalOutlierFactor(n_neighbors=n_neighbors, contamination=contamination)
    pred = model.fit_predict(X)
    scores = model.negative_outlier_factor_  # lower = more anomalous
    mask = pred == -1
    rows = _flagged_rows(valid, cols, mask, scores, index)
    return {
        "test": "local_outlier_factor",
        "feature_columns": cols,
        "n": int(len(valid)),
        "contamination": contamination,
        "n_neighbors": n_neighbors,
        "anomaly_count": int(mask.sum()),
        "anomalies": rows,
        "interpretation": (
            f"LocalOutlierFactor flagged {int(mask.sum())} of {len(valid)} rows "
            f"({mask.mean()*100:.1f}%) as local-density anomalies across "
            f"{len(cols)} feature(s)."
        ),
    }


def detect_multivariate(df, columns=None, method="isolation_forest", contamination=0.05):
    method = (method or "isolation_forest").lower()
    if method in ("isolation_forest", "iforest", "if"):
        return isolation_forest(df, columns, contamination)
    if method in ("local_outlier_factor", "lof"):
        return local_outlier_factor(df, columns, contamination)
    raise AnalyticsError("method must be 'isolation_forest' or 'local_outlier_factor'.")
