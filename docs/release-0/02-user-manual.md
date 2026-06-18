# User Manual — DSE Release 0 Analytics Workflows

**Status:** Draft · **Owner:** Zia (add `[SCREENSHOT]`s, verify steps) · **Audience:** lab analysts & managers

## Getting started
Sign in at `[SITE: https://…]`. The left sidebar is grouped: **Workspace**, **Analytics**, **AI**, **Operations**, **Admin**. The new workflows live under **Analytics**: *Anomaly Detection*, *Statistical Calculations*, *Forecast Workbench*, *Chart Studio*, *Saved Runs* (plus *LIMS Forecasting*, *Data Explorer*, *Advanced Analytics*). `[SCREENSHOT: sidebar]`

Every workflow follows the same shape: **pick a dataset → configure → Run → read the result (chart + numbers + interpretation) → Explain / Export / it's saved automatically.**

---

## 1. Anomaly Detection  (`Analytics → Anomaly Detection`)
Finds unusual records. `[SCREENSHOT: anomaly page]`
1. **Dataset** — choose at the top (filter by database if several).
2. **Scope:**
   - *Multivariate* — unusual across several numeric columns together. Method **Auto** (recommended) votes across Isolation Forest, LOF and Elliptic Envelope; or pick one, or **Autoencoder** (deep). Set **Contamination** (expected outlier fraction, default 0.05) and optionally restrict **feature columns**.
   - *Univariate* — one column. **Modified z‑score (MAD)** is robust to skew; or Z‑score / IQR.
   - *Time series* — **STL seasonal‑residual**; pick the value column, optionally an order/date column and seasonal period (auto‑detected if blank).
3. **Run name** (optional) — labels the saved run.
4. **Detect anomalies.**
**Reading the result:** a multivariate run shows a **PCA scatter** (red = anomalies), **top drivers** bars for the most anomalous row (which variables pushed it out), and a table of flagged rows with scores. Univariate/time‑series runs show the series with flagged points and bounds.

## 2. Statistical Calculations  (`Analytics → Statistical Calculations`)
A catalogue of tests by category. `[SCREENSHOT: statistics page]`
1. Pick a **dataset**, then a **category** tab (Descriptive / Comparison / Relationship / Quality), then a **test card**.
2. Fill the **guided form** (column pickers adapt to the test). Tip: tick **check assumptions** on t‑test/ANOVA to get normality/variance advice (and a non‑parametric result when appropriate).
3. **Run test.** Results show the statistic, p‑value, **effect size**, a tailored chart (regression fit, ANOVA group means, SPC limits, etc.), and a plain‑English interpretation.
- *Descriptive (All Columns)* summarises every numeric column at once.

## 3. Forecast Workbench  (`Analytics → Forecast Workbench`)
Projects a series forward with the best of 10 methods. `[SCREENSHOT: forecast page]`
1. **Series source:** *Resample a date column* (choose date, aggregation, frequency D/W/M/Q/Y) or *A numeric column*.
2. **Method:** **Auto** (recommended) tries every available method, **backtests** them, and picks the most accurate; or pick a specific one (Naive … Holt‑Winters … ARIMA/SARIMA … Prophet … XGBoost/LSTM).
3. **Pick best by** (Auto only): sMAPE/MAPE/RMSE/MAE. Set **periods ahead**. For *Regression/ARIMAX* you may add **exogenous regressors**.
4. **Forecast.** You get the history + forecast + confidence band, a **method leaderboard** (accuracy per method; the chosen one marked ★), and which methods were skipped (e.g. if XGBoost isn't installed on the host).
> For meaningful forecasts use a genuine time series (a date‑resampled metric), not an unordered results column.

## 4. Chart Studio  (`Analytics → Chart Studio`)
Compare multiple datasets/parameters on one chart. `[SCREENSHOT: chart studio]`
1. Choose a **chart type** (line/bar/area/scatter/pie/**combo**).
2. **Add series** — each series picks its own dataset, X (category/date) field, aggregation and value; give it a label/colour; in **combo** each series can have its own type.
3. Series align on the **shared set of X values**. A note appears if the datasets share few common X values.
4. Use **Title / Smooth / Stack** options; export or email/schedule from the **Export** menu.

## 5. AI Explanation
On every result, the **AI Explanation** panel can generate a plain‑English brief ("Explain this") and answer follow‑up questions, grounded only in the numbers. If the local model is offline it shows a friendly notice instead of failing. `[SCREENSHOT: explain panel]`

## 6. Export, Email & Schedule
From a result chart or Chart Studio, the **Export** menu offers:
- **PNG / JPEG / PDF** (chart image) and **CSV / Excel** (underlying data).
- **Email this…** — enter recipients + a note; the chart (and run data) is emailed now.
- **Schedule…** — opens **Reports** pre‑filled to deliver this saved run on a daily/weekly/monthly cadence. `[SCREENSHOT: export menu + email modal]`

## 7. Saved Runs  (`Analytics → Saved Runs`)
Every run you execute is saved here. Filter by workflow, open a run to see its full result (re‑export, re‑explain), or delete it. `[SCREENSHOT: saved runs]`

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| "Need at least N rows…" | Not enough data for that method — pick a richer dataset/column. |
| Forecast picks Naive with high error | Input isn't a real time series — use a date‑resampled metric. |
| "XGBoost not available" in skipped list | Optional lib not installed on this host; other methods still run. |
| AI Explanation says "model not configured / connection error" | Local LLM offline — analytics still work; retry when it's up. |
