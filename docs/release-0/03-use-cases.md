# Use Cases — DSE Release 0

**Status:** Draft · **Owner:** Zia (verify each clicks through; add `[SCREENSHOT]`) · **Version:** 0.9

Actors: **Analyst** (runs analyses), **Lab Manager** (consumes results/reports), **Admin** (access & scheduling). All actions are over the existing dataset access model.

---

### UC‑1 Detect multivariate anomalies in QC results
**Actor:** Analyst · **Goal:** find samples that are unusual across several measured parameters.
1. Open **Anomaly Detection**, pick a dataset (e.g. *EGPC Off‑Spec by Operator*).
2. Scope = *Multivariate*, Method = *Auto*, contamination = 0.05; Run.
3. System flags rows, shows a **PCA scatter** (anomalies highlighted), **per‑feature contributions** for the top row, and a flagged‑rows table.
4. Analyst clicks **Explain this** → plain‑English summary of what's unusual and what to check.
5. Run is auto‑saved; Analyst optionally **exports** PNG/PDF or **emails** it to the Lab Manager.
**Alt:** insufficient numeric rows → clear message, no flags. `[SCREENSHOT]`

### UC‑2 Robust / seasonal single‑parameter anomaly check
**Actor:** Analyst · **Goal:** flag outliers in one parameter, robust to skew or seasonality.
1. Anomaly Detection → Scope *Univariate*, Method *Modified z‑score (MAD)*, value column = `sulphur_value`; Run → flagged points + bounds.
2. For a time series: Scope *Time series* → *STL seasonal‑residual* (period auto‑detected) highlights points whose de‑seasonalized residual is extreme. `[SCREENSHOT]`

### UC‑3 Compare two groups with assumption checking
**Actor:** Analyst · **Goal:** is mean turnaround different between two sections, defensibly?
1. **Statistical Calculations** → category *Comparison* → *Two‑Sample t‑Test* card.
2. Pick value + group columns, tick **assumption checks**; Run.
3. Result shows t, p, **Cohen's d** + magnitude, per‑group means±CI, and an **assumptions** banner; if non‑normal it also reports **Mann‑Whitney**.
4. **Explain this** translates significance + effect size for a non‑statistician. `[SCREENSHOT]`

### UC‑4 One‑click descriptive profile of every parameter
**Actor:** Analyst · **Goal:** summarise all numeric columns at once.
1. Statistics → *Descriptive (All Columns)* → Run → table of mean/median/std/quartiles/skew/kurtosis/missing% per column. `[SCREENSHOT]`

### UC‑5 SPC / process capability for a controlled parameter
**Actor:** Lab Manager · **Goal:** is a parameter in control and capable?
1. Statistics → *Quality* → *Control Chart (I‑MR)* → SPC chart with CL/UCL/LCL + violations.
2. *Process Capability* with LSL/USL → Cp/Cpk/Pp/Ppk + % out‑of‑spec + DPMO. `[SCREENSHOT]`

### UC‑6 Forecast a metric with the best model, automatically
**Actor:** Analyst · **Goal:** project next 6 months of workload/turnaround/cost with the most accurate method.
1. **Forecast Workbench** → source *Resample a date column* → date column, agg, freq = Monthly, periods = 6, Method = **Auto**.
2. System backtests all available methods, shows a **leaderboard** (sMAPE/MAPE/RMSE/MAE), picks the best (e.g. Holt‑Winters), and plots history + forecast + confidence band.
3. **Explain this** cites the chosen method and its accuracy. Run saved; exportable. `[SCREENSHOT]`
**Alt:** pin a specific method (e.g. SARIMA) from the dropdown and re‑run.

### UC‑7 Forecast with external regressors (ARIMAX)
**Actor:** Analyst · Source *numeric column* → Method *Regression/ARIMAX* → choose exogenous columns → forecast incorporates them. `[SCREENSHOT]`

### UC‑8 Compare datasets in one chart (Chart Studio)
**Actor:** Lab Manager · **Goal:** overlay this year vs last year, or two sections, on one axis.
1. **Chart Studio** → add Series 1 (dataset A, X = month, value = count) and Series 2 (dataset B, …).
2. Pick chart type (line/bar/**combo**); series align on the **union of X labels**; sparse‑overlap warning if datasets share few X values.
3. Export PNG/PDF or **Email**/**Schedule**. `[SCREENSHOT]`

### UC‑9 Publish & distribute
**Actor:** Lab Manager.
1. From any result chart or Chart Studio → **Export** menu → PNG / JPEG / PDF / CSV / Excel.
2. **Email this…** → recipients + note → chart image (+ data) delivered now.
3. **Schedule…** → creates a recurring report (dataset CSV, **saved run**, or chart) on the Reports page. `[SCREENSHOT]`

### UC‑10 Revisit history & re‑open a run
**Actor:** Analyst → **Saved Runs** → filter by workflow → open a run → see the saved result, export, or **Explain** again. Delete when done. `[SCREENSHOT]`

### UC‑11 Ask the agent to do it conversationally
**Actor:** Analyst → **AI Assistant** (agent mode) → "Forecast next quarter's sample volume with the best model and explain why." The agent calls `forecast_advanced` + `explain_analysis` and answers with the leaderboard‑backed result. `[SCREENSHOT]`
