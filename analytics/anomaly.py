"""Anomaly-detection engine (Phase 3, extended for Release 0).

Three scopes, one entry point (:func:`detect`):

* **univariate**  — z-score, IQR (in :mod:`analytics.predict`) and robust
  modified z-score (MAD) on a single column.
* **series**      — STL seasonal-residual detection for time series.
* **multivariate**— IsolationForest, LocalOutlierFactor, EllipticEnvelope /
  Mahalanobis, an optional PyTorch autoencoder, and an ``auto`` consensus vote.

Multivariate results carry **per-feature contributions** (which variable drove
each flag — the standardized value of each feature) and an optional **PCA 2-D
projection** for plotting. Convention: lower ``score`` = more anomalous, across
every multivariate method, so the frontend can sort uniformly.

Deterministic: ``random_state``/seeds are fixed so the same data + params give
the same flags every run — important for a QC audit trail. Heavy/optional libs
(torch) are imported lazily and degrade with a clear error.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import AnalyticsError, _num

RANDOM_STATE = 42
MAX_FLAGGED = 200
MAX_PROJECTION_POINTS = 1500


# --------------------------------------------------------------------------
# Shared matrix + enrichment helpers
# --------------------------------------------------------------------------
def _feature_matrix(df, columns):
    """Build a scaled numeric matrix from the requested columns (default: all
    numeric columns). Returns (X_scaled, used_columns, valid_row_index, valid_df)."""
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


def _pca_projection(X, mask, index, scores, max_points=MAX_PROJECTION_POINTS):
    """Project the scaled matrix to 2-D for plotting; keep all anomalies and
    deterministically downsample the normal points if there are very many."""
    from sklearn.decomposition import PCA

    n = len(X)
    if X.shape[1] >= 2:
        pca = PCA(n_components=2, random_state=RANDOM_STATE)
        coords = pca.fit_transform(X)
        evr = [float(v) for v in pca.explained_variance_ratio_]
    else:
        coords = np.column_stack([X[:, 0], np.zeros(n)])
        evr = [1.0, 0.0]

    keep = range(n)
    if n > max_points:
        anom = [i for i in range(n) if mask[i]]
        normal = [i for i in range(n) if not mask[i]]
        step = max(1, len(normal) // max(1, max_points - len(anom)))
        keep = sorted(anom + normal[::step])

    points = [{"x": _num(float(coords[i, 0])), "y": _num(float(coords[i, 1])),
               "row": int(index[i]), "flag": bool(mask[i]),
               "score": _num(float(scores[i]))} for i in keep]
    return {"points": points, "explained_variance_ratio": evr}


def _enrich(valid_df, cols, X, mask, scores, index, projection=True, votes=None):
    """Build flagged-row records (+ per-feature contributions) and an optional
    PCA projection. ``X`` is standardized, so each feature's value IS its
    contribution: large |value| → that variable drove the flag."""
    rows = []
    for pos, idx in enumerate(index):
        if not mask[pos]:
            continue
        rec = {
            "row": int(idx),
            "score": _num(float(scores[pos])),
            "values": {c: (_num(float(valid_df.iloc[pos][c]))
                           if pd.notna(valid_df.iloc[pos][c]) else None) for c in cols},
            "contributions": {c: _num(float(X[pos, j])) for j, c in enumerate(cols)},
        }
        if votes is not None:
            rec["votes"] = int(votes[pos])
        rows.append(rec)
    rows.sort(key=lambda r: (r["score"] if r["score"] is not None else 0))
    rows = rows[:MAX_FLAGGED]
    pca = _pca_projection(X, mask, index, scores) if (projection and len(X) >= 2) else None
    return rows, pca


def _result(test, cols, valid, mask, rows, pca, extra=None, interpretation=""):
    out = {
        "test": test, "method": test, "scope": "multivariate",
        "feature_columns": cols, "n": int(len(valid)),
        "anomaly_count": int(np.sum(mask)),
        "anomalies": rows,
        "interpretation": interpretation,
    }
    if extra:
        out.update(extra)
    if pca:
        out["pca_projection"] = pca
    return out


# --------------------------------------------------------------------------
# Multivariate methods
# --------------------------------------------------------------------------
def isolation_forest(df, columns=None, contamination=0.05, projection=True):
    from sklearn.ensemble import IsolationForest

    X, cols, index, valid = _feature_matrix(df, columns)
    contamination = min(max(float(contamination or 0.05), 0.001), 0.5)
    model = IsolationForest(contamination=contamination, random_state=RANDOM_STATE,
                            n_estimators=200)
    pred = model.fit_predict(X)
    scores = model.decision_function(X)  # lower = more anomalous
    mask = pred == -1
    rows, pca = _enrich(valid, cols, X, mask, scores, index, projection)
    return _result(
        "isolation_forest", cols, valid, mask, rows, pca,
        extra={"contamination": contamination},
        interpretation=(
            f"IsolationForest flagged {int(mask.sum())} of {len(valid)} rows "
            f"({mask.mean()*100:.1f}%) as multivariate anomalies across "
            f"{len(cols)} feature(s): {', '.join(cols[:6])}"
            + ("…" if len(cols) > 6 else "") + "."),
    )


def local_outlier_factor(df, columns=None, contamination=0.05, n_neighbors=20,
                         projection=True):
    from sklearn.neighbors import LocalOutlierFactor

    X, cols, index, valid = _feature_matrix(df, columns)
    contamination = min(max(float(contamination or 0.05), 0.001), 0.5)
    n_neighbors = int(min(max(n_neighbors, 5), max(5, len(valid) - 1)))
    model = LocalOutlierFactor(n_neighbors=n_neighbors, contamination=contamination)
    pred = model.fit_predict(X)
    scores = model.negative_outlier_factor_  # lower = more anomalous
    mask = pred == -1
    rows, pca = _enrich(valid, cols, X, mask, scores, index, projection)
    return _result(
        "local_outlier_factor", cols, valid, mask, rows, pca,
        extra={"contamination": contamination, "n_neighbors": n_neighbors},
        interpretation=(
            f"LocalOutlierFactor flagged {int(mask.sum())} of {len(valid)} rows "
            f"({mask.mean()*100:.1f}%) as local-density anomalies across "
            f"{len(cols)} feature(s)."),
    )


def elliptic_envelope(df, columns=None, contamination=0.05, projection=True):
    """Gaussian / Mahalanobis-distance outlier detection (robust covariance)."""
    from sklearn.covariance import EllipticEnvelope

    X, cols, index, valid = _feature_matrix(df, columns)
    contamination = min(max(float(contamination or 0.05), 0.001), 0.5)
    model = EllipticEnvelope(contamination=contamination, random_state=RANDOM_STATE,
                             support_fraction=None)
    pred = model.fit_predict(X)
    md = model.mahalanobis(X)          # squared Mahalanobis distance (higher = worse)
    scores = -md                        # negate → lower = more anomalous (uniform)
    mask = pred == -1
    rows, pca = _enrich(valid, cols, X, mask, scores, index, projection)
    # surface raw Mahalanobis distance on each flagged row for interpretability
    md_by_pos = {int(index[p]): float(md[p]) for p in range(len(index))}
    for r in rows:
        r["mahalanobis"] = _num(md_by_pos.get(r["row"]))
    return _result(
        "elliptic_envelope", cols, valid, mask, rows, pca,
        extra={"contamination": contamination},
        interpretation=(
            f"EllipticEnvelope (robust Mahalanobis) flagged {int(mask.sum())} of "
            f"{len(valid)} rows ({mask.mean()*100:.1f}%) as anomalies across "
            f"{len(cols)} feature(s)."),
    )


def autoencoder(df, columns=None, contamination=0.05, projection=True,
                epochs=150, lr=0.01):
    """Deep reconstruction-error anomaly detection (optional, needs torch)."""
    try:
        import torch
        from torch import nn
    except Exception as exc:  # noqa: BLE001
        raise AnalyticsError(
            "Autoencoder anomaly detection requires PyTorch (torch), which is "
            "not available in this environment."
        ) from exc

    X, cols, index, valid = _feature_matrix(df, columns)
    contamination = min(max(float(contamination or 0.05), 0.001), 0.5)
    torch.manual_seed(RANDOM_STATE)

    Xt = torch.tensor(X, dtype=torch.float32)
    d = X.shape[1]
    hidden = max(2, d // 2 + 1)
    bottleneck = max(1, hidden // 2)
    model = nn.Sequential(
        nn.Linear(d, hidden), nn.ReLU(),
        nn.Linear(hidden, bottleneck), nn.ReLU(),
        nn.Linear(bottleneck, hidden), nn.ReLU(),
        nn.Linear(hidden, d),
    )
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.MSELoss()
    model.train()
    for _ in range(int(epochs)):
        opt.zero_grad()
        loss = loss_fn(model(Xt), Xt)
        loss.backward()
        opt.step()
    model.eval()
    with torch.no_grad():
        recon = model(Xt).numpy()
    err = np.mean((X - recon) ** 2, axis=1)         # reconstruction error per row
    thresh = float(np.quantile(err, 1 - contamination))
    mask = err > thresh
    scores = -err                                   # lower = more anomalous (uniform)
    rows, pca = _enrich(valid, cols, X, mask, scores, index, projection)
    err_by_pos = {int(index[p]): float(err[p]) for p in range(len(index))}
    for r in rows:
        r["reconstruction_error"] = _num(err_by_pos.get(r["row"]))
    return _result(
        "autoencoder", cols, valid, mask, rows, pca,
        extra={"contamination": contamination, "threshold": _num(thresh)},
        interpretation=(
            f"Autoencoder (reconstruction error) flagged {int(mask.sum())} of "
            f"{len(valid)} rows ({mask.mean()*100:.1f}%) as anomalies across "
            f"{len(cols)} feature(s)."),
    )


def _multivariate_auto(df, columns=None, contamination=0.05, projection=True):
    """Consensus vote across IsolationForest + LOF + EllipticEnvelope. A row is
    flagged when a majority of the available models agree."""
    from sklearn.ensemble import IsolationForest
    from sklearn.neighbors import LocalOutlierFactor

    X, cols, index, valid = _feature_matrix(df, columns)
    contamination = min(max(float(contamination or 0.05), 0.001), 0.5)
    votes = np.zeros(len(X), dtype=int)
    per_model = {}

    iff = IsolationForest(contamination=contamination, random_state=RANDOM_STATE,
                          n_estimators=200)
    p_if = iff.fit_predict(X)
    s_if = iff.decision_function(X)        # used as the continuous ranking score
    votes += (p_if == -1).astype(int)
    per_model["isolation_forest"] = int((p_if == -1).sum())

    nn_ = int(min(max(20, 5), max(5, len(X) - 1)))
    lof = LocalOutlierFactor(n_neighbors=nn_, contamination=contamination)
    p_lof = lof.fit_predict(X)
    votes += (p_lof == -1).astype(int)
    per_model["local_outlier_factor"] = int((p_lof == -1).sum())

    n_models = 2
    try:
        from sklearn.covariance import EllipticEnvelope
        ee = EllipticEnvelope(contamination=contamination, random_state=RANDOM_STATE)
        p_ee = ee.fit_predict(X)
        votes += (p_ee == -1).astype(int)
        per_model["elliptic_envelope"] = int((p_ee == -1).sum())
        n_models = 3
    except Exception as exc:  # noqa: BLE001
        per_model["elliptic_envelope_error"] = str(exc)

    mask = votes >= 2                      # majority of 2-3 models
    rows, pca = _enrich(valid, cols, X, mask, s_if, index, projection, votes=votes)
    return _result(
        "anomaly_consensus", cols, valid, mask, rows, pca,
        extra={"contamination": contamination, "method": "auto",
               "models_run": n_models, "per_model_counts": per_model},
        interpretation=(
            f"Consensus of {n_models} models flagged {int(mask.sum())} of "
            f"{len(valid)} rows ({mask.mean()*100:.1f}%) as anomalies "
            f"(majority vote across {', '.join(k for k in per_model if not k.endswith('_error'))})."),
    )


def detect_multivariate(df, columns=None, method="isolation_forest",
                        contamination=0.05, projection=True):
    method = (method or "isolation_forest").lower()
    if method in ("isolation_forest", "iforest", "if"):
        return isolation_forest(df, columns, contamination, projection)
    if method in ("local_outlier_factor", "lof"):
        return local_outlier_factor(df, columns, contamination, projection=projection)
    if method in ("elliptic_envelope", "elliptic", "mahalanobis", "robust_covariance"):
        return elliptic_envelope(df, columns, contamination, projection)
    if method in ("autoencoder", "ae", "deep"):
        return autoencoder(df, columns, contamination, projection)
    if method in ("auto", "consensus"):
        return _multivariate_auto(df, columns, contamination, projection)
    raise AnalyticsError(
        "method must be one of: isolation_forest, local_outlier_factor, "
        "elliptic_envelope, mahalanobis, autoencoder, auto."
    )


# --------------------------------------------------------------------------
# Univariate (robust) + time-series (seasonal) methods
# --------------------------------------------------------------------------
def modified_zscore(df, value_column, threshold=3.5):
    """Robust outlier detection via the modified z-score (median + MAD)."""
    if value_column not in df.columns:
        raise AnalyticsError(f"Column '{value_column}' not found.")
    s = pd.to_numeric(df[value_column], errors="coerce").dropna()
    if len(s) < 3:
        raise AnalyticsError("Need at least 3 numeric points.")
    y = s.to_numpy(float)
    idx = s.index
    threshold = float(threshold or 3.5)
    median = float(np.median(y))
    mad = float(np.median(np.abs(y - median)))
    if mad == 0:
        std = float(np.std(y)) or 1.0
        mz = 0.6745 * (y - median) / std
    else:
        mz = 0.6745 * (y - median) / mad
    flags = np.abs(mz) > threshold
    anomalies = [{"index": int(idx[i]), "value": _num(float(y[i])), "score": _num(float(mz[i]))}
                 for i in range(len(y)) if flags[i]]
    anomalies.sort(key=lambda r: -abs(r["score"] or 0))
    return {
        "test": "modified_zscore", "scope": "univariate", "method": "modified_zscore",
        "value_column": value_column, "threshold": threshold,
        "n": len(y), "total": len(y), "anomaly_count": int(flags.sum()),
        "values": [_num(float(v)) for v in y],
        "flags": [bool(f) for f in flags],
        "anomalies": anomalies[:MAX_FLAGGED],
        "bounds": {"median": _num(median), "mad": _num(mad)},
        "interpretation": (
            f"Robust modified z-score (median+MAD) flagged {int(flags.sum())} of "
            f"{len(y)} points in {value_column} (|Mᵢ| > {threshold})."),
    }


def _infer_period(y):
    """First strong autocorrelation peak (lag ≥ 2) as a seasonal period, else None."""
    n = len(y)
    if n < 8:
        return None
    try:
        from statsmodels.tsa.stattools import acf
        a = acf(y, nlags=min(n // 2, 366), fft=True)
    except Exception:  # noqa: BLE001
        return None
    best, best_val = None, 0.2
    for lag in range(2, len(a) - 1):
        if a[lag] > best_val and a[lag] >= a[lag - 1] and a[lag] >= a[lag + 1]:
            best, best_val = lag, a[lag]
    return best


def stl_residual(df, value_column, index_column=None, period=None, threshold=3.0):
    """STL seasonal-trend decomposition; flag points whose residual z-score is large."""
    if value_column not in df.columns:
        raise AnalyticsError(f"Column '{value_column}' not found.")
    threshold = float(threshold or 3.0)
    series = pd.to_numeric(df[value_column], errors="coerce")

    if index_column and index_column in df.columns:
        tmp = pd.DataFrame({"_v": series, "_o": df[index_column]}).dropna(subset=["_v"])
        try:
            tmp["_o"] = pd.to_datetime(tmp["_o"], errors="raise")
        except Exception:  # noqa: BLE001
            pass
        tmp = tmp.sort_values("_o")
        y = tmp["_v"].to_numpy(float)
        labels = [str(o) for o in tmp["_o"].tolist()]
        idx = tmp.index
    else:
        sd = series.dropna()
        y = sd.to_numpy(float)
        labels = [str(i) for i in sd.index.tolist()]
        idx = sd.index

    n = len(y)
    period = int(period) if period else _infer_period(y)
    if not period or period < 2:
        raise AnalyticsError(
            "Could not determine a seasonal period automatically — pass 'period' (≥2)."
        )
    if n < 2 * period:
        raise AnalyticsError(
            f"Need at least {2 * period} points (2 full seasons of period {period}) for STL."
        )

    from statsmodels.tsa.seasonal import STL
    res = STL(y, period=period, robust=True).fit()
    resid = np.asarray(res.resid, float)
    rstd = float(np.std(resid)) or 1.0
    z = resid / rstd
    flags = np.abs(z) > threshold
    anomalies = [{"index": int(idx[i]), "label": labels[i], "value": _num(float(y[i])),
                  "score": _num(float(z[i]))} for i in range(n) if flags[i]]
    anomalies.sort(key=lambda r: -abs(r["score"] or 0))
    return {
        "test": "stl_residual", "scope": "series", "method": "stl_residual",
        "value_column": value_column, "period": period, "threshold": threshold,
        "n": n, "total": n, "anomaly_count": int(flags.sum()),
        "labels": labels,
        "values": [_num(float(v)) for v in y],
        "trend": [_num(float(v)) for v in np.asarray(res.trend, float)],
        "seasonal": [_num(float(v)) for v in np.asarray(res.seasonal, float)],
        "resid": [_num(float(v)) for v in resid],
        "flags": [bool(f) for f in flags],
        "anomalies": anomalies[:MAX_FLAGGED],
        "interpretation": (
            f"STL seasonal-residual detection (period={period}) flagged "
            f"{int(flags.sum())} of {n} points in {value_column} as anomalies."),
    }


# --------------------------------------------------------------------------
# Unified entry point
# --------------------------------------------------------------------------
def detect(df, *, scope="multivariate", method="auto", columns=None,
           value_column=None, index_column=None, contamination=0.05,
           threshold=None, period=None, projection=True):
    """Dispatch anomaly detection by ``scope`` and ``method``.

    scope='univariate' → zscore | iqr | modified_zscore (needs ``value_column``)
    scope='series'     → stl_residual (needs ``value_column``, optional ``period``)
    scope='multivariate' → isolation_forest | local_outlier_factor |
                           elliptic_envelope | mahalanobis | autoencoder | auto
    """
    scope = (scope or "multivariate").lower()
    method = (method or "auto").lower()

    if scope == "univariate":
        col = value_column or (columns[0] if columns else None)
        if not col:
            raise AnalyticsError("Univariate anomaly detection needs a 'value_column'.")
        if method in ("mad", "modified_zscore", "robust_zscore"):
            return modified_zscore(df, col, threshold=threshold if threshold else 3.5)
        from . import predict
        m = "iqr" if method == "iqr" else "zscore"
        res = predict.detect_anomalies(df, col, method=m,
                                       threshold=float(threshold) if threshold else 3.0)
        res.setdefault("scope", "univariate")
        res.setdefault("test", f"{m}_anomaly")
        res.setdefault("n", res.get("total"))
        return res

    if scope in ("series", "timeseries", "time_series"):
        col = value_column or (columns[0] if columns else None)
        if not col:
            raise AnalyticsError("Time-series anomaly detection needs a 'value_column'.")
        return stl_residual(df, col, index_column=index_column, period=period,
                            threshold=float(threshold) if threshold else 3.0)

    return detect_multivariate(df, columns=columns, method=method,
                               contamination=contamination, projection=projection)
