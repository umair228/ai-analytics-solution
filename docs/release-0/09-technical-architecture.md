# Technical Architecture — DSE Release 0

**Status:** Draft · **Owner:** Umair Khan · **AI/ML section review:** `[REVIEW: ML]` (Kulsoom) · **Extends:** `../../DIS_ENTERPRISE_ARCHITECTURE.md`

## 1. System context
DSE is an on‑premise analytics platform over LabWare LIMS data. Users work through a React SPA; a Django/DRF backend serves data access, an analytics engine, and a tool‑using AI agent backed by a **local** LLM. No third‑party inference at runtime.

```
Browser (React SPA, ECharts)
      │  HTTPS/HTTP (/api)
      ▼
nginx (web container) ── proxy ──► Django/DRF (api container, gunicorn)
                                     ├─ analytics engine (pandas/scipy/sklearn/statsmodels + lazy xgboost/torch/neuralprophet)
                                     ├─ AnalysisRun persistence ─► Postgres (pgvector)
                                     ├─ AI agent (tools) ─► local LLM (on‑prem / Tailscale, OpenAI‑compatible)
                                     └─ exporters / email (SMTP)
scheduler container ─ cron ─► run_scheduled_refreshes, send_scheduled_reports
```

## 2. Backend modules (Release 0 additions in **bold**)
- `analytics/engine.py` — `build_dataframe(dataset)` from a dataset's cached rows; JSON‑safe coercion (`_num`/`_cell`); `AnalyticsError`.
- **`analytics/forecasting_models.py`** — method registry (11 entries / 10 method families) behind a common `fit_predict`; `detect_season`; rolling‑origin `backtest`; `run_forecast(methods="auto")` → selected + leaderboard + per‑method results. Heavy libs lazy‑imported, `available()`‑guarded.
- `analytics/anomaly.py` (**extended**) — `detect(scope, method, …)` dispatcher; univariate (z/IQR/**MAD**), series (**STL**), multivariate (IsolationForest/LOF/**EllipticEnvelope·Mahalanobis**/**autoencoder**/**auto** consensus); **per‑feature contributions** + **PCA projection**.
- `analytics/stats_tests.py` + `stats_catalog.py` (**extended**) — 15 catalogued tests; **batch descriptives**, **assumption checks**, **effect sizes**, **group CIs**.
- **`analytics/explain.py`** — builds a compact numbers‑grounded context and calls `ai.client.run_chat`; always returns a structured, graceful payload.
- **`analytics/models.py` `AnalysisRun`** — workflow‑discriminated saved runs (config/result/metrics/status JSON; owner + shared_with). Serializers + `AnalysisRunViewSet`.
- **`analytics/reporting.py`** — `run_to_envelope` / `chart_to_envelope` adapters for export.
- `analytics/views.py` (**extended**) — `_run_and_save` plumbing (auth → df → run → persist → audit); endpoints: `/anomaly/`, `/stats/run/`, **`/forecast/`**, **`/explain/`**, **`/runs/`**, `/forecast-metric/` (now `method=`).
- `ai/tools.py` (**extended**) — `forecast_advanced`, `detect_anomalies_advanced`, `explain_analysis` (21 tools total).
- `datasets/` (**extended**) — `DatasetReport.kind` (dataset_csv | analysis_run | chart) + run/chart targets + `email-now`; `send_scheduled_reports` branches via `ai/exporters.py`.

**Design invariants:** engines are pure & deterministic (fixed seeds) — math is exact and reproducible; the LLM only *narrates*. Optional dependencies never block. The app DB is the analytics‑replica/Postgres; analytics read cached dataset snapshots only.

## 3. Frontend (React 19, Vite, react‑query, zustand, ECharts)
- Pages: `AnomalyDetectionPage`, `StatisticsPage`, `MetricForecastPage`, `SavedRunsPage`, `RunDetailPage`, `ChartStudioPage` (routes under `/analytics/*` + `/chart-studio`).
- Shared toolkit `src/components/analytics/`: `ResultView` + `resultRenderers` (chart/table/stat per result type), `ParamFields`, `AIExplainPanel`, `DatasetPickerBar`; hooks `useDatasetPicker`, `useRunner`, `useChartRef`.
- `src/lib/chartStudio.js` — union‑aligned multi‑dataset series builder. `src/components/publish/` — `ChartActionsMenu`, `EmailChartModal`; `src/lib/exporters.js` chart‑native PNG/JPEG/PDF (ECharts `getDataURL`).
- Sidebar grouped: Workspace / Analytics / AI / Operations / Admin.

## 4. Data & control flow (a workflow run)
1. SPA POSTs `{dataset, …config}` to an analytics endpoint.
2. View authorizes the dataset, `build_dataframe`, runs the engine, persists an `AnalysisRun`, audits, returns `{result, run_id, metrics}` (+ optional `explanation`).
3. SPA renders via `ResultView`; **Explain** calls `/explain/` (by `run_id`); **Export/Email/Schedule** use client export + `/reports/…`.

## 5. Deployment topology
See `08-deployment-guide.md`. Compose: `db` (pgvector pg16), `api` (+migrate on start), `scheduler`, `web` (nginx). Images on GHCR; Python 3.12 image; `numpy<2`/`pandas 2.2` pinned via constraints; no GPU required for analytics.

## 6. Scaling / future
Synchronous today; `AnalysisRun.status` (pending/running) is the seam for an async worker (Celery/django‑q) if long deep‑model runs or batch scheduling demand it. `[REVIEW: ML]` Kulsoom to expand §2 forecasting/anomaly methodology and validation references.
