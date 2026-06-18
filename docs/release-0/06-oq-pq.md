# Operational & Performance Qualification (OQ / PQ) — DSE Release 0

**Status:** Draft · **Owner:** Kulsoom `[REVIEW: ML]` (review protocols + sign off); Zia executes the runs · **System:** `[SITE]`
**Purpose:** demonstrate that, once installed (see IQ), DSE R0 **operates per specification** (OQ) and **performs acceptably on real lab data** (PQ).

---

## Part A — Operational Qualification (OQ)
Each function exercised against a controlled input with a known/expected behaviour. Record Pass/Fail + evidence.

| # | Function | Controlled test | Acceptance criterion | Result |
|---|---|---|---|---|
| OQ‑1 | Anomaly — multivariate | Dataset with injected extreme rows | The injected rows appear among the flagged set; PCA + contributions render | `[SIGN‑OFF]` |
| OQ‑2 | Anomaly — robust univariate | Column with known outliers + skew | MAD method flags the known outliers; symmetric inliers not flagged | |
| OQ‑3 | Anomaly — seasonal | Synthetic seasonal series + one injected spike | STL flags the spike; period auto‑detected = known period | |
| OQ‑4 | Statistics — comparison | Two groups with a known mean gap | Test significant; Cohen's d sign/magnitude as expected; CIs computed | |
| OQ‑5 | Statistics — assumption check | Non‑normal groups | `recommended = nonparametric`; Mann‑Whitney/Kruskal reported | |
| OQ‑6 | Statistics — capability | Column vs known LSL/USL | Cp/Cpk match hand calculation within rounding | |
| OQ‑7 | Forecast — seasonal auto | Synthetic trend+season series | `auto` selects a seasonal method (Holt‑Winters/SARIMA); leaderntries finite | |
| OQ‑8 | Forecast — graceful skip | Host without an optional lib | Method appears in `methods_skipped`; run still succeeds | |
| OQ‑9 | Saved runs | Run each workflow | A run row persists with correct workflow/method/metrics; re‑openable | |
| OQ‑10 | AI explanation | Any result, model online | Returns a grounded narrative; with model offline → graceful notice (no error) | |
| OQ‑11 | Publishing | Export PNG/PDF/Excel; Email now; Schedule | Files correct; email delivered; scheduled report row created | |
| OQ‑12 | Determinism | Re‑run identical params | Byte‑identical numeric result (QC audit requirement) | |

Automated backstop for OQ‑1/3/7/8/12: `analytics.tests_release0` (14 tests) — attach the green run as evidence.

## Part B — Performance Qualification (PQ)
Acceptance on **real, representative** datasets in the production environment. `[REVIEW: ML]` Kulsoom to confirm thresholds.

### PQ‑1 Forecast accuracy (the headline ML claim)
- **Protocol:** on ≥3 real time‑series metrics (e.g. monthly sample volume, monthly off‑spec %, turnaround), run Forecast Workbench in **Auto** mode; record the backtest leaderboard.
- **Acceptance:** the selected method's **sMAPE ≤ [REVIEW: ML, e.g. 20%]** on at least 2 of 3 series, and **Auto's** selected method is no worse than the best single method by > 1 sMAPE point. Document any series where no method meets threshold (data‑quality note, not a defect).

### PQ‑2 Anomaly validation
- **Protocol:** on a labelled/known dataset (or analyst‑adjudicated sample), run multivariate **Auto**; have an analyst review the top‑N flagged rows.
- **Acceptance:** ≥ **[REVIEW: ML, e.g. 70%]** of top‑10 flags judged "worth investigating"; per‑feature contributions agree with the analyst's reasoning.

### PQ‑3 Statistical correctness
- **Protocol:** reproduce 2–3 results (t‑test, ANOVA, capability) in a reference tool (R/Minitab/Excel).
- **Acceptance:** statistic, p‑value and effect size agree within rounding.

### PQ‑4 Responsiveness
- **Protocol:** time a typical run of each workflow on a representative dataset (record dataset size).
- **Acceptance:** interactive workflows (anomaly/stats, forecast non‑deep) return within **[REVIEW, e.g. ≤ 10 s]**; deep/Prophet methods, when explicitly chosen, within **[REVIEW, e.g. ≤ 60 s]**. (Engine uses bounded grids/epochs; async seam reserved if limits are exceeded.)

### PQ‑5 Stability
- **Protocol:** run the full black‑box suite (`04-test-scripts.md` §B) on prod; monitor `api` logs.
- **Acceptance:** no unhandled 500s; all graceful‑degradation paths behave as specified.

## Sign‑off
OQ executed: **Name / Date** `[SIGN‑OFF]` · PQ reviewed & accepted: **Kulsoom / Date** `[SIGN‑OFF]` · QA approval: **Name / Date** `[SIGN‑OFF]`.
