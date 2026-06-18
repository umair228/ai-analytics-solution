# Release Notes — DSE Release 0

**Version:** 0 · **Date:** 2026‑06‑21 · **Audience:** customers / pilot users
**Theme:** self‑service analytics workflows, best‑in‑class forecasting, comparative charting, and one‑click publishing — all running fully on‑premise.

## ✨ New features

**Standalone Anomaly Detection.** A dedicated workspace to find unusual records — single‑parameter (z‑score, IQR, robust **MAD**), seasonal time‑series (**STL**), or multivariate (**Isolation Forest, Local Outlier Factor, Elliptic Envelope/Mahalanobis, Autoencoder**, or an **Auto** consensus). Multivariate results explain *which variables* drove each flag and plot anomalies on a PCA map.

**Standalone Statistical Calculations.** A organised catalogue (Descriptive / Comparison / Relationship / Quality‑SPC) with guided forms: descriptive (single + **all‑columns**), normality, trend, t‑tests, ANOVA + Tukey, F/Levene, chi‑square, correlation, regression, outliers, control charts, process capability — now with **effect sizes**, **confidence intervals**, and optional **assumption checks** (with non‑parametric fallbacks).

**Forecast Workbench — 10 methods + Auto.** Naive, Moving Average, Exponential Smoothing, Holt, Holt‑Winters, ARIMA, SARIMA, Prophet, Regression/ARIMAX, and XGBoost/LSTM behind one engine. **Auto** mode backtests every method (MAE/RMSE/MAPE/sMAPE) and selects the most accurate, showing a transparent **accuracy leaderboard** and confidence bands. Automatic seasonality detection.

**Saved Run History.** Every run (anomaly/statistics/forecast) is saved with its config, result and accuracy metrics — list, filter, re‑open, re‑explain, export, or delete.

**AI Explanations (local).** One click turns any result into a plain‑English brief for non‑specialists, and answers follow‑up questions — grounded strictly in the numbers, served by the on‑prem model.

**Chart Studio.** Build comparative charts that overlay **multiple datasets and parameters** on one axis (line, bar, area, scatter, pie, mixed/combo) with dynamic series selection.

**Chart Publishing & Distribution.** Export any chart to **PNG / JPEG / PDF** (and data to CSV/Excel), **email** a chart on demand, and **schedule** recurring delivery of a dataset, a saved run, or a chart (daily/weekly/monthly).

**Agent tools.** The local AI agent can now run advanced forecasting and anomaly detection and narrate results conversationally.

## ⚙️ Under the hood
- New `AnalysisRun` persistence; analytics endpoints persist + audit each run.
- Heavy/optional ML libraries (XGBoost, PyTorch‑LSTM, NeuralProphet) are **lazy‑loaded** and **skipped gracefully** where unavailable — a host without one simply offers the rest.
- `numpy < 2` / `pandas 2.2` compatibility preserved; analytics need **no GPU**.

## 🔒 Security & privacy
No third‑party API calls at runtime; all inference is local. Analytics run over cached, access‑controlled dataset snapshots (read‑only). Every action is audited. See the Cybersecurity document.

## ⚠️ Known limitations
- Forecasting an **unordered** column (not a time series) yields weak accuracy by design — use a date‑resampled metric.
- **XGBoost** requires the OpenMP runtime on the host; if absent it is listed as *skipped* (other 9 methods run). It is present in the production container image.
- **AI explanations** require the local model to be reachable; otherwise they degrade to a notice.
- Root‑Cause & Relationship discovery remain in the combined **Advanced Analytics** page (not yet split into standalone workflows).

## 📦 Upgrade / deployment notes
- Database migrations apply automatically on container start (`migrate --noinput`).
- New backend dependency: `xgboost` (installed under the pinned‑numpy constraints in the image).
- No configuration changes required for existing installs.

`[SITE]` Add build tag / commit SHA and customer name on final formatting.
