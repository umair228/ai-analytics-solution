"""Unified forecasting engine — 10 methods behind one interface.

Implements the Release-0 forecasting catalogue:

  1 naive  2 moving_average  3 ses (simple exp smoothing)  4 holt
  5 holt_winters  6 arima  7 sarima  8 prophet (NeuralProphet)
  9 regression (lag/ARIMAX-style)  10 xgboost + lstm

Each method exposes ``fit_predict(y, periods, season, exog, future_exog, ci,
params) -> ForecastResult``. :func:`run_forecast` runs the requested methods (or
the ``auto`` panel), **backtests** each with rolling-origin evaluation
(MAE/RMSE/MAPE/sMAPE), and selects the most accurate. Heavy/optional libs
(statsmodels for the classical models, xgboost, torch, neuralprophet) are
imported lazily and degrade gracefully — the module imports cleanly with only
numpy/pandas present, and missing methods are reported as ``skipped``.

Deterministic: all seeds are fixed so the same series + params reproduce the
same forecast — required for a QC audit trail.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm

from .engine import AnalyticsError, _num

RANDOM_STATE = 42
MAX_PERIODS = 60
_FREQ_SEASON = {"D": 7, "W": 52, "M": 12, "Q": 4, "Y": 1}


class EngineSkip(Exception):
    """Raised by a method when it cannot be applied to this series (e.g. no
    seasonality) — reported as a skip rather than a hard failure."""


@dataclass
class ForecastResult:
    method: str
    history: np.ndarray
    forecast: np.ndarray
    lower: np.ndarray | None = None
    upper: np.ndarray | None = None
    params: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class Method:
    name: str
    fit_predict: callable
    requires: tuple = ()
    needs_season: bool = False
    in_auto: bool = True
    prepare: callable | None = None


def _z(ci):
    ci = float(ci or 0.95)
    ci = min(max(ci, 0.5), 0.999)
    return float(norm.ppf(0.5 + ci / 2.0))


def _supervised(y, k):
    """Build a lag-feature matrix: row i = [y[i-1], …, y[i-k]] → target y[i]."""
    X, t = [], []
    for i in range(k, len(y)):
        X.append(y[i - k:i][::-1])
        t.append(y[i])
    return np.asarray(X, float), np.asarray(t, float)


def _default_lags(y, season):
    base = season if (season and season >= 2) else min(max(2, len(y) // 3), 12)
    return int(min(base, max(1, len(y) - 2)))


# --------------------------------------------------------------------------
# Seasonality detection
# --------------------------------------------------------------------------
def detect_season(y, freq=None):
    """Seasonal period from a known frequency, else the first strong ACF peak."""
    y = np.asarray(y, float)
    if freq:
        s = _FREQ_SEASON.get(str(freq).upper())
        if s and s >= 2 and len(y) >= 2 * s:
            return s
        if s and s < 2:
            return None
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
        if a[lag] > best_val and a[lag] >= a[lag - 1] and a[lag] >= a[lag + 1] and n >= 2 * lag:
            best, best_val = lag, a[lag]
    return best


# --------------------------------------------------------------------------
# The methods
# --------------------------------------------------------------------------
def _m_naive(y, periods, season, exog, fexog, ci, params):
    last = float(y[-1])
    fc = np.full(periods, last)
    resid = np.diff(y) if len(y) > 1 else np.array([0.0])
    band = _z(ci) * (float(np.std(resid)) if len(resid) else 0.0)
    return ForecastResult("naive", y, fc, fc - band, fc + band, {"value": _num(last)})


def _m_moving_average(y, periods, season, exog, fexog, ci, params):
    w = season if (season and season >= 2) else max(2, min(len(y) // 3, 12))
    w = int(min(w, len(y)))
    fc = np.full(periods, float(np.mean(y[-w:])))
    roll = pd.Series(y).rolling(w).mean()
    resid = (pd.Series(y) - roll).dropna().to_numpy()
    band = _z(ci) * (float(np.std(resid)) if len(resid) else float(np.std(y)))
    return ForecastResult("moving_average", y, fc, fc - band, fc + band, {"window": w})


def _m_ses(y, periods, season, exog, fexog, ci, params):
    from statsmodels.tsa.holtwinters import SimpleExpSmoothing
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = SimpleExpSmoothing(y, initialization_method="estimated").fit()
        fc = np.asarray(fit.forecast(periods), float)
        resid = y - np.asarray(fit.fittedvalues, float)
    band = _z(ci) * float(np.std(resid))
    return ForecastResult("ses", y, fc, fc - band, fc + band,
                          {"alpha": _num(float(fit.params.get("smoothing_level", np.nan)))})


def _m_holt(y, periods, season, exog, fexog, ci, params):
    try:
        from statsmodels.tsa.holtwinters import Holt
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = Holt(y, initialization_method="estimated").fit()
            fc = np.asarray(fit.forecast(periods), float)
            resid = y - np.asarray(fit.fittedvalues, float)
    except Exception:  # noqa: BLE001 - numpy fallback to the existing Holt
        from . import predict
        level, trend, fitted = predict._holt(y)
        fc = np.array([level + trend * (h + 1) for h in range(periods)], float)
        resid = y - np.asarray(fitted, float)
    band = _z(ci) * float(np.std(resid))
    return ForecastResult("holt", y, fc, fc - band, fc + band, {})


def _m_holt_winters(y, periods, season, exog, fexog, ci, params):
    if not season or season < 2 or len(y) < 2 * season:
        raise EngineSkip("Holt-Winters needs ≥2 full seasons.")
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(
            y, trend="add", seasonal="add", seasonal_periods=int(season),
            initialization_method="estimated").fit()
        fc = np.asarray(fit.forecast(periods), float)
        resid = y - np.asarray(fit.fittedvalues, float)
    band = _z(ci) * float(np.std(resid))
    return ForecastResult("holt_winters", y, fc, fc - band, fc + band, {"season": int(season)})


def _ndiffs(y, max_d=2):
    from statsmodels.tsa.stattools import adfuller
    cur, d = np.asarray(y, float), 0
    while d < max_d and len(cur) > 10:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                p = adfuller(cur, autolag="AIC")[1]
        except Exception:  # noqa: BLE001
            break
        if p < 0.05:
            break
        cur = np.diff(cur)
        d += 1
    return d


def _prep_arima(y, season):
    from statsmodels.tsa.arima.model import ARIMA
    d = _ndiffs(y)
    best, best_aic = (1, d, 0), np.inf
    for p in range(4):
        for q in range(4):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    aic = ARIMA(y, order=(p, d, q)).fit().aic
                if np.isfinite(aic) and aic < best_aic:
                    best, best_aic = (p, d, q), aic
            except Exception:  # noqa: BLE001
                continue
    return {"order": best}


def _m_arima(y, periods, season, exog, fexog, ci, params):
    from statsmodels.tsa.arima.model import ARIMA
    order = (params or {}).get("order") or (1, _ndiffs(y), 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ARIMA(y, order=order).fit()
        f = fit.get_forecast(periods)
        fc = np.asarray(f.predicted_mean, float)
        conf = np.asarray(f.conf_int(alpha=1 - ci), float)
    return ForecastResult("arima", y, fc, conf[:, 0], conf[:, 1], {"order": list(order)})


def _prep_sarima(y, season):
    return {"order": (1, _ndiffs(y), 1),
            "seasonal_order": (1, 1, 1, int(season)) if season else (0, 0, 0, 0)}


def _m_sarima(y, periods, season, exog, fexog, ci, params):
    if not season or season < 2 or len(y) < 2 * season:
        raise EngineSkip("SARIMA needs ≥2 full seasons.")
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    order = (params or {}).get("order", (1, _ndiffs(y), 1))
    sorder = (params or {}).get("seasonal_order", (1, 1, 1, int(season)))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = SARIMAX(y, order=order, seasonal_order=sorder,
                      enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
        f = fit.get_forecast(periods)
        fc = np.asarray(f.predicted_mean, float)
        conf = np.asarray(f.conf_int(alpha=1 - ci), float)
    return ForecastResult("sarima", y, fc, conf[:, 0], conf[:, 1],
                          {"order": list(order), "seasonal_order": list(sorder)})


def _m_regression(y, periods, season, exog, fexog, ci, params):
    """Linear autoregression on lag features + time trend (+ optional exog,
    carried forward). A robust, explainable ARIMAX-style baseline."""
    import statsmodels.api as sm
    k = (params or {}).get("k") or _default_lags(y, season)
    Xl, t = _supervised(y, k)
    start = k
    trend_idx = np.arange(start, len(y)).reshape(-1, 1).astype(float)
    parts = [Xl, trend_idx]
    if exog is not None:
        exog = np.asarray(exog, float)
        if exog.ndim == 1:
            exog = exog.reshape(-1, 1)
        parts.append(exog[start:])
    X = np.hstack(parts)
    Xc = sm.add_constant(X, has_constant="add")
    model = sm.OLS(t, Xc).fit()
    resid = t - model.predict(Xc)
    band = _z(ci) * float(np.std(resid))
    last_exog = exog[-1] if exog is not None else None
    hist, preds = list(np.asarray(y, float)), []
    for h in range(periods):
        lags = np.asarray(hist[-k:])[::-1]
        row = [1.0, *lags, float(len(hist))]
        if last_exog is not None:
            fe = (np.asarray(fexog, float)[h] if fexog is not None and h < len(fexog) else last_exog)
            row += list(np.atleast_1d(fe))
        p = float(model.predict(np.asarray(row).reshape(1, -1))[0])
        preds.append(p)
        hist.append(p)
    fc = np.asarray(preds, float)
    return ForecastResult("regression", y, fc, fc - band, fc + band,
                          {"lags": k, "exog": exog is not None})


def _m_xgboost(y, periods, season, exog, fexog, ci, params):
    import xgboost as xgb
    k = (params or {}).get("k") or _default_lags(y, season)
    Xl, t = _supervised(y, k)
    model = xgb.XGBRegressor(n_estimators=300, max_depth=3, learning_rate=0.08,
                             subsample=0.9, random_state=RANDOM_STATE, verbosity=0)
    model.fit(Xl, t)
    resid = t - model.predict(Xl)
    band = _z(ci) * float(np.std(resid))
    hist, preds = list(np.asarray(y, float)), []
    for _ in range(periods):
        lags = np.asarray(hist[-k:])[::-1].reshape(1, -1)
        p = float(model.predict(lags)[0])
        preds.append(p)
        hist.append(p)
    fc = np.asarray(preds, float)
    return ForecastResult("xgboost", y, fc, fc - band, fc + band, {"lags": k})


def _m_lstm(y, periods, season, exog, fexog, ci, params):
    import torch
    from torch import nn
    torch.manual_seed(RANDOM_STATE)
    k = (params or {}).get("k") or _default_lags(y, season)
    k = max(2, k)
    mu, sd = float(np.mean(y)), float(np.std(y)) or 1.0
    yn = (np.asarray(y, float) - mu) / sd
    Xl, t = _supervised(yn, k)
    if len(Xl) < 4:
        raise EngineSkip("Series too short for an LSTM.")
    Xt = torch.tensor(Xl, dtype=torch.float32).unsqueeze(-1)
    tt = torch.tensor(t, dtype=torch.float32).unsqueeze(-1)

    class _Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(1, 16, batch_first=True)
            self.fc = nn.Linear(16, 1)

        def forward(self, x):
            o, _ = self.lstm(x)
            return self.fc(o[:, -1, :])

    net = _Net()
    opt = torch.optim.Adam(net.parameters(), lr=0.01)
    loss_fn = nn.MSELoss()
    net.train()
    for _ in range(200):
        opt.zero_grad()
        loss_fn(net(Xt), tt).backward()
        opt.step()
    net.eval()
    with torch.no_grad():
        fitted = net(Xt).squeeze(-1).numpy()
        resid = t - fitted
        hist, preds = list(yn), []
        for _ in range(periods):
            seq = torch.tensor(np.asarray(hist[-k:]), dtype=torch.float32).reshape(1, k, 1)
            p = float(net(seq).item())
            preds.append(p)
            hist.append(p)
    band = _z(ci) * float(np.std(resid)) * sd
    fc = np.asarray(preds, float) * sd + mu
    return ForecastResult("lstm", y, fc, fc - band, fc + band, {"lags": k})


def _m_prophet(y, periods, season, exog, fexog, ci, params):
    try:
        from neuralprophet import NeuralProphet
    except Exception as exc:  # noqa: BLE001
        raise EngineSkip("NeuralProphet not installed") from exc
    import logging
    logging.getLogger("NP").setLevel(logging.ERROR)
    n = len(y)
    ds = pd.date_range("2000-01-01", periods=n, freq="D")
    dfp = pd.DataFrame({"ds": ds, "y": np.asarray(y, float)})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m = NeuralProphet(epochs=60, n_lags=0, n_forecasts=1,
                          yearly_seasonality=False, weekly_seasonality=bool(season),
                          daily_seasonality=False, quiet=True)
        m.fit(dfp, freq="D", progress=None)
        future = m.make_future_dataframe(dfp, periods=periods)
        fcst = m.predict(future)
    yhat = fcst["yhat1"].to_numpy(float)[-periods:]
    resid = float(np.std(np.diff(y)))
    band = _z(ci) * resid
    fc = np.asarray(yhat, float)
    return ForecastResult("prophet", y, fc, fc - band, fc + band, {"backend": "neuralprophet"})


REGISTRY: dict[str, Method] = {
    "naive": Method("naive", _m_naive),
    "moving_average": Method("moving_average", _m_moving_average),
    "ses": Method("ses", _m_ses, requires=("statsmodels",)),
    "holt": Method("holt", _m_holt),
    "holt_winters": Method("holt_winters", _m_holt_winters,
                           requires=("statsmodels",), needs_season=True),
    "arima": Method("arima", _m_arima, requires=("statsmodels",), prepare=_prep_arima),
    "sarima": Method("sarima", _m_sarima, requires=("statsmodels",),
                     needs_season=True, prepare=_prep_sarima),
    "regression": Method("regression", _m_regression, requires=("statsmodels",)),
    "xgboost": Method("xgboost", _m_xgboost, requires=("xgboost",)),
    "prophet": Method("prophet", _m_prophet, requires=("neuralprophet",), in_auto=False),
    "lstm": Method("lstm", _m_lstm, requires=("torch",), in_auto=False),
}


# --------------------------------------------------------------------------
# Backtesting + accuracy metrics
# --------------------------------------------------------------------------
def _metrics(actual, pred):
    actual, pred = np.asarray(actual, float), np.asarray(pred, float)
    err = actual - pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = np.where(actual == 0, np.nan, np.abs(actual))
        mape_arr = np.abs(err) / denom
        mape = float(np.nanmean(mape_arr) * 100) if np.any(np.isfinite(mape_arr)) else None
        sden = np.abs(actual) + np.abs(pred)
        smape = float(np.mean(np.where(sden == 0, 0.0, 2 * np.abs(err) / sden)) * 100)
    return {"mae": _num(mae), "rmse": _num(rmse),
            "mape": _num(mape) if mape is not None else None, "smape": _num(smape)}


def backtest(y, method, horizon, season, ci, params, scheme="rolling", folds=3):
    """Rolling-origin (or single-holdout) backtest. Returns accuracy metrics."""
    y = np.asarray(y, float)
    n = len(y)
    h = max(1, min(horizon, max(1, n // 5)))
    min_train = max(2 * (season or 2), 8, 2 * h)
    if n - h < min_train:
        if n - h < 4:
            return {"ok": False, "reason": "series too short to backtest"}
        cuts = [n - h]
    elif scheme == "holdout":
        cuts = [n - h]
    else:
        span = n - h - min_train
        step = max(1, span // max(1, folds - 1)) if folds > 1 else 1
        cuts = list(range(min_train, n - h + 1, step))[-folds:]

    acts, preds = [], []
    for cut in cuts:
        train, test = y[:cut], y[cut:cut + h]
        if len(test) < h:
            continue
        try:
            fr = method.fit_predict(train, h, season, None, None, ci, params)
        except Exception:  # noqa: BLE001
            continue
        pred = np.asarray(fr.forecast, float)[:h]
        if len(pred) < h or not np.all(np.isfinite(pred)):
            continue
        acts.append(test)
        preds.append(pred)
    if not acts:
        return {"ok": False, "reason": "no valid backtest folds"}
    return {"ok": True, "n_folds": len(acts),
            **_metrics(np.concatenate(acts), np.concatenate(preds))}


# --------------------------------------------------------------------------
# Output assembly + orchestration
# --------------------------------------------------------------------------
def _trend(y, next_value):
    y = np.asarray(y, float)
    n = len(y)
    x = np.arange(n)
    slope, intercept = np.polyfit(x, y, 1)
    fit = slope * x + intercept
    ssr = float(np.sum((y - fit) ** 2))
    sst = float(np.sum((y - y.mean()) ** 2))
    r2 = (1 - ssr / sst) if sst else 1.0
    direction = "increasing" if slope > 1e-9 else "decreasing" if slope < -1e-9 else "flat"
    pct = float((y[-1] - y[0]) / abs(y[0]) * 100) if y[0] else 0.0
    return {"slope": _num(slope), "intercept": _num(intercept), "r_squared": _num(r2),
            "direction": direction, "pct_change": _num(pct), "next_value": _num(next_value)}


def _finalize(fr: ForecastResult, y):
    nxt = float(fr.forecast[0]) if len(fr.forecast) else None
    out = {
        "method": fr.method,
        "history": [_num(float(v)) for v in fr.history],
        "forecast": [_num(float(v)) for v in fr.forecast],
        "lower": [_num(float(v)) for v in fr.lower] if fr.lower is not None else None,
        "upper": [_num(float(v)) for v in fr.upper] if fr.upper is not None else None,
        "trend": _trend(y, nxt),
        "params": fr.params,
    }
    if fr.extra:
        out["extra"] = fr.extra
    return out


def _available(method):
    for mod in method.requires:
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            return False
    return True


def _narrative(selected, scores, season, metric):
    if not selected:
        return "No forecasting method could be fit to this series."
    s = scores.get(selected, {})
    acc = f"{metric.upper()}={s.get(metric)}" if s.get(metric) is not None else "backtest n/a"
    seas = f", seasonality≈{season}" if season else ""
    return (f"Best method: {selected} ({acc}){seas}. The selected forecast is shown; "
            f"see the leaderboard for every method's accuracy.")


def run_forecast(y, *, periods=6, methods="auto", freq=None, exog=None,
                 future_exog=None, metric="smape", ci=0.95, backtest_scheme="rolling"):
    """Forecast a 1-D series with one or many methods, backtest, and auto-select.

    ``methods`` is "auto" (the default panel), a single method name, or a list.
    Returns a JSON-safe dict with ``selected``, per-method ``scores`` (the
    backtest leaderboard) and ``results`` (history/forecast/bands/trend).
    """
    y = np.asarray(y, float)
    y = y[~np.isnan(y)]
    if len(y) < 5:
        raise AnalyticsError("Need at least 5 numeric points to forecast.")
    periods = int(min(max(periods or 6, 1), MAX_PERIODS))
    metric = metric if metric in ("mae", "rmse", "mape", "smape") else "smape"
    season = detect_season(y, freq)

    if methods in (None, "", "auto") or methods == ["auto"]:
        requested = "auto"
        chosen = [m for m in REGISTRY.values() if m.in_auto]
    else:
        names = [methods] if isinstance(methods, str) else list(methods)
        requested = names
        chosen = [REGISTRY[n] for n in names if n in REGISTRY]
        if not chosen:
            raise AnalyticsError(
                f"Unknown forecast method(s). Available: {', '.join(REGISTRY)} or 'auto'.")

    horizon = max(2, min(periods, max(2, len(y) // 5)))
    scores, results, skipped = {}, {}, []

    for m in chosen:
        if not _available(m):
            skipped.append(m.name)
            continue
        if m.needs_season and (not season or len(y) < 2 * season):
            scores[m.name] = {"ok": False, "reason": "no seasonality / too few seasons"}
            continue
        try:
            params = m.prepare(y, season) if m.prepare else None
        except Exception:  # noqa: BLE001
            params = None
        try:
            fr = m.fit_predict(y, periods, season, exog, future_exog, ci, params)
        except EngineSkip as exc:
            scores[m.name] = {"ok": False, "reason": str(exc)}
            continue
        except Exception as exc:  # noqa: BLE001
            scores[m.name] = {"ok": False, "error": str(exc)[:200]}
            continue
        results[m.name] = _finalize(fr, y)
        try:
            scores[m.name] = backtest(y, m, horizon, season, ci, params, scheme=backtest_scheme)
        except Exception as exc:  # noqa: BLE001
            scores[m.name] = {"ok": False, "error": str(exc)[:200]}

    ranked = [(nm, s) for nm, s in scores.items()
              if s.get("ok") and nm in results and s.get(metric) is not None]
    selected = (min(ranked, key=lambda kv: kv[1][metric])[0] if ranked
                else (next(iter(results), None)))

    return {
        "test": "forecast",
        "season": season,
        "periods": periods,
        "metric": metric,
        "methods_requested": requested,
        "methods_run": list(results),
        "methods_skipped": skipped,
        "selected": selected,
        "scores": scores,
        "results": results,
        "forecast": results.get(selected),
        "interpretation": _narrative(selected, scores, season, metric),
    }
