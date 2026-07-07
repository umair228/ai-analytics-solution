# Requirements & Design Document — DSE Release 0

**Document status:** Draft · **Owner:** Umair Khan · **ML methodology review:** `[REVIEW: ML]` (Umair Khan) · **Version:** 0.9

## 1. Purpose & scope
Release 0 turns three previously-embedded analytics capabilities into first-class,
self-service **workflows** and adds comparative charting and chart distribution,
keeping all inference **on-premise / local-LLM only**. In scope:

1. Standalone **Anomaly Detection** workflow.
2. Standalone **Statistical Calculations** workflow.
3. Unified **Forecasting** workflow (10 methods + automatic best-selection).
4. **Saved run history** for all three.
5. **Advanced charting** (multi-dataset / comparative) — Chart Studio.
6. **Chart publishing & distribution** (export, email, schedule).
7. Local‑LLM **AI explanations** of every result.

Out of scope for R0 (planned later): deep omics pipelines, real-time streaming, multi-tenant SSO hardening.

## 2. Functional requirements

### FR‑A Anomaly Detection
- FR‑A1 The user selects a dataset and a **scope**: univariate, time‑series, or multivariate.
- FR‑A2 Methods: univariate **z‑score / IQR / modified z‑score (MAD, robust)**; time‑series **STL seasonal‑residual**; multivariate **Isolation Forest, Local Outlier Factor, Elliptic Envelope (Mahalanobis), Autoencoder**, and **`auto`** (consensus vote).
- FR‑A3 Multivariate results MUST report, per flagged row, the **per‑feature contribution** (which variables drove the flag) and provide a **2‑D PCA projection** for plotting.
- FR‑A4 Configurable contamination/threshold with safe defaults; results are deterministic (fixed seeds).
- FR‑A5 Graceful, explicit errors when data is insufficient (e.g. < 10 complete rows, < 2 seasons for STL).

### FR‑B Statistical Calculations
- FR‑B1 A catalogue of tests grouped by **Descriptive / Comparison / Relationship / Quality (SPC)**.
- FR‑B2 Tests: descriptive (single + **all‑columns batch**), normality, trend (+ Mann‑Kendall), one/two‑sample t, one‑way ANOVA (+ Tukey), F‑test/Levene, chi‑square (+ Cramér's V), correlation (Pearson/Spearman/Kendall), regression (linear/polynomial/non‑linear), outliers, control chart (I‑MR), process capability (Cp/Cpk/Pp/Ppk), and **assumption checks**.
- FR‑B3 Comparison tests MUST report **effect size** (Cohen's d, η², Cramér's V, r) and group **confidence intervals**.
- FR‑B4 Optional **assumption checks** (normality + variance homogeneity) that advise parametric vs non‑parametric and, when warranted, compute the matching non‑parametric statistic — advisory only, never blocking.

### FR‑C Forecasting
- FR‑C1 Forecast a numeric series or a date‑resampled metric (D/W/M/Q/Y, agg = count/sum/mean/min/max/median).
- FR‑C2 Ten methods: **naive, moving average, exponential smoothing, Holt, Holt‑Winters, ARIMA, SARIMA, Prophet (NeuralProphet), Regression/ARIMAX, XGBoost + LSTM**.
- FR‑C3 `auto` mode MUST **backtest** all available methods (rolling‑origin) reporting **MAE/RMSE/MAPE/sMAPE** and select the most accurate by a chosen metric; a **leaderboard** is returned.
- FR‑C4 Forecasts MUST include confidence bands and a trend summary; **automatic seasonality detection**.
- FR‑C5 Optional methods absent on a host (xgboost/torch/neuralprophet) are reported as **skipped**, never erroring.

### FR‑D Saved runs
- FR‑D1 Every explicit workflow run is persisted (owner‑scoped) with config, result, accuracy metrics, status.
- FR‑D2 Users can list/filter, re‑open, and delete their runs; runs are shareable per existing dataset ACLs.

### FR‑E Charting & publishing
- FR‑E1 **Chart Studio** overlays multiple series from **multiple datasets** aligned on a shared x‑axis; types line/bar/area/scatter/pie/**mixed (combo)**; dynamic dataset + series selection.
- FR‑E2 Any chart exports to **PNG / JPEG / PDF** and its data to **CSV / Excel**.
- FR‑E3 **Email now** sends a chart image (+ optional data) to recipients; **scheduling** delivers a dataset, a saved run, or a chart on a daily/weekly/monthly cadence.

### FR‑F AI explanation
- FR‑F1 Every result can be explained in plain English by the **local** model, grounded strictly in the numbers; free‑form follow‑up questions supported.
- FR‑F2 If the model is unavailable, the feature degrades gracefully with a clear message (no error, no blocking).

## 3. Non‑functional requirements
- **NFR‑1 Locality/security:** no third‑party API calls at runtime; LLM served on‑prem/over Tailscale. Read paths only; analytics operate on cached dataset snapshots.
- **NFR‑2 Determinism/auditability:** identical inputs+params reproduce identical results (QC requirement); every run audited.
- **NFR‑3 Graceful degradation:** missing optional ML libs or LLM never break a workflow.
- **NFR‑4 Compatibility:** numpy < 2 / pandas 2.2 pin preserved; runs on Python 3.12 image, Postgres (pgvector) app DB, no GPU required for analytics.
- **NFR‑5 Performance:** synchronous request/response; bounded model grids and epochs so a run completes within interactive time on typical lab series. (Async seam reserved in the data model.)

## 4. Design overview
- **Engines** (`analytics/`): pure functions over a pandas DataFrame built from a dataset's cached rows; JSON‑safe outputs with an `interpretation` string; `AnalyticsError` for user errors. New: `forecasting_models.py` (method registry + backtest + `run_forecast`), extended `anomaly.py` (`detect()` dispatcher), extended `stats_tests.py`, `explain.py`, `reporting.py`.
- **Persistence:** `AnalysisRun` model (one table, `workflow` discriminator; config/result/metrics JSON) → `/api/analytics/runs/`.
- **API:** thin DRF views funnel through `_run_and_save` (auth → build df → run → persist → audit), preserving the historical `{dataset, result}` response and adding `run_id`/`metrics`/`explanation`.
- **Agent:** new tools `forecast_advanced`, `detect_anomalies_advanced`, `explain_analysis` expose the same engines to the local agent (math in tools, narration by LLM).
- **Frontend:** standalone pages reuse a shared toolkit (`ResultView`, `ParamFields`, `AIExplainPanel`, `useDatasetPicker`, `useRunner`); ECharts rendering; client + server export.
- **Distribution:** generalized `DatasetReport` (kind = dataset_csv | analysis_run | chart) + cron‑driven `send_scheduled_reports`, reusing `ai/exporters.py`.

`[REVIEW: ML]` Kulsoom to confirm §FR‑A2 method choices, §FR‑C2/C3 backtest protocol, and the determinism claims for XGBoost/LSTM.

## 5. Traceability
Each FR maps to a Test Case in `04-test-scripts.md` (IDs mirror: FR‑A1 → TC‑A1, …) and to an OQ/PQ protocol in `06-oq-pq.md`.
