# Test Scripts / Test Cases — DSE Release 0

**Status:** Draft · **Owner:** Umair Khan (execute black‑box on the running app, record actual result + pass/fail + `[SCREENSHOT]`) · **Version:** 0.9

Two layers: **white‑box** (automated, already in the repo) and **black‑box** (manual UI scripts below). Environment for manual runs: `[SITE: URL]`, seeded EGPC datasets, logged in as an analyst.

## A. White‑box (automated) — evidence
Run on the backend; attach console output as evidence.
```
cd ai-analytics-solution && .venv/bin/python manage.py test
```
Expected: **OK**, 188 tests, 0 failures; `manage.py makemigrations --check` → *No changes detected*; `manage.py check` → 0 issues. Targeted suite: `manage.py test analytics.tests_release0` (14 tests) covers the new engines, graceful skips and the numpy<2 pin. Frontend: `npm run build` completes with no errors.
Recorded: ____ (date / tester / result) `[SIGN‑OFF]`

## B. Black‑box (manual UI) — test cases
Fill **Actual** + **P/F** for each. IDs trace to requirements in `01-requirements-design.md`.

| TC | Maps | Steps | Expected | Actual / P‑F |
|---|---|---|---|---|
| TC‑A1 | FR‑A1/A3 | Anomaly → dataset, Scope=Multivariate, Method=Auto, contamination 0.05 → Run | Rows flagged; PCA scatter renders with anomalies highlighted; "top drivers" bars + flagged table shown; run saved (badge + in Saved Runs) | |
| TC‑A2 | FR‑A2 | Scope=Univariate, Method=Modified z‑score, value column numeric → Run | Flagged points listed with scores; bounds shown | |
| TC‑A3 | FR‑A2 | Scope=Time series, Method=STL, a value column, period blank → Run | Period auto‑detected; series + flagged points; residual available | |
| TC‑A4 | FR‑A5 | Univariate on a near‑empty / tiny column | Clear "need at least N" message; no crash | |
| TC‑B1 | FR‑B2 | Statistics → Descriptive (All Columns) → Run | Table with one row per numeric column (mean/median/std/quartiles/skew/kurtosis/missing%) | |
| TC‑B2 | FR‑B3/B4 | Comparison → Two‑Sample t‑Test, value+group, **check assumptions** ✓ → Run | t, p, **Cohen's d** + magnitude, per‑group mean±CI, assumptions banner; non‑parametric shown if non‑normal | |
| TC‑B3 | FR‑B2 | Relationship → Regression (linear, y + x) → Run | Fit chart (actual scatter + predicted line), coefficient/R² stats, equation | |
| TC‑B4 | FR‑B2 | Quality → Control Chart (I‑MR) → Run | SPC chart with CL/UCL/LCL + violations listed | |
| TC‑B5 | FR‑B2 | Quality → Process Capability with LSL/USL → Run | Cp/Cpk/Pp/Ppk, % out‑of‑spec, DPMO | |
| TC‑C1 | FR‑C1/C3 | Forecast → Resample a date column, Monthly, periods 6, Method **Auto** → Forecast | Leaderboard with sMAPE/MAPE/RMSE/MAE per method; one method ★ selected; history+forecast+band plotted; saved | |
| TC‑C2 | FR‑C2 | Re‑run with Method = SARIMA (explicit) | SARIMA forecast returned (or clean "needs ≥2 seasons" note) | |
| TC‑C3 | FR‑C5 | Observe the **skipped** list when a host lacks XGBoost | "xgboost" listed as skipped; no error; other methods ran | |
| TC‑C4 | FR‑C2 | Source=numeric column, Method=Regression/ARIMAX, add exogenous columns → Forecast | Forecast incorporates regressors; bands shown | |
| TC‑D1 | FR‑D1/D2 | Saved Runs → filter by workflow → open a run → delete one | Runs listed with metrics summary; detail re‑renders result; delete removes it | |
| TC‑E1 | FR‑E1 | Chart Studio → 2 series from 2 datasets, type=Line then Combo | Series overlay on shared X; combo allows per‑series type; sparse‑overlap warning when applicable | |
| TC‑E2 | FR‑E2 | Any chart → Export → PNG, then PDF, then Excel | Files download; PNG/PDF are the chart, Excel is the data | |
| TC‑E3 | FR‑E3 | Export → Email this… → recipient + send | Success toast; email received (or printed by console backend in non‑prod) with PNG attached | |
| TC‑E4 | FR‑E3 | Export → Schedule… → creates a saved‑run report on Reports | Report row created (type "Saved run"); appears active | |
| TC‑F1 | FR‑F1 | On any result → AI Explanation → "Explain this" | Plain‑English brief grounded in the numbers; follow‑up question answered | |
| TC‑F2 | FR‑F2 | With local LLM stopped → "Explain this" | Friendly "model unavailable" notice; workflow unaffected | |
| TC‑G1 | NFR‑1 | (Network capture during a run, optional) | No outbound third‑party API calls; LLM traffic only to the on‑prem/Tailscale endpoint | |
| TC‑G2 | NFR‑2 | Re‑run the same anomaly/forecast with identical params | Identical flagged rows / identical selected method + numbers (determinism) | |

## C. Regression check
Confirm pre‑existing pages still work: Advanced Analytics (all 5 tabs), LIMS Forecasting, Data Explorer, Reports (dataset CSV), Ask Data / Ask the Database, Dashboards. Record P/F. `[SIGN‑OFF]`
