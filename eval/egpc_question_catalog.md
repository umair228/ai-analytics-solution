# EGPC Analytics — Question Catalog & Data-Readiness Map

Source: **"Specimen Questions of Data Analytics" v2** (LabWare Arabia, Mar 2022) —
~180 questions. This maps every section to (a) the DIS capability that answers it
and (b) whether the **current EGPC data** can actually answer it.

## ⚠️ The headline finding (read this first)
The document is framed around a **multi-company "across EGPC"** dataset
("by each company", "top 3 companies", interlaboratory, etc.). The data we have
is essentially **one organisation's refinery lab operations**:

- `SAMPLE.CUSTOMER` is populated on only **4,472 / 60,220 samples (~7%)**, with
  **11 distinct values dominated by NORPETCO** (2,951) and PETROSILAH (1,422).
- There is no company/site dimension on the other ~93% of samples.

➡️ So the many **"by each company / top-N companies"** questions are **not
answerable as written** — they need the multi-company dataset, not a code change.
What *is* richly populated and answerable: **products (42), tests/analyses (267),
out-of-spec results (3,020), analysts (75), sample types, statuses, and dates**.

**Recommendation:** benchmark on the **answerable** set now (OOS, products, tests,
methods, ranges, TAT, trends), and separately flag the company-dimension questions
as a **data requirement** for EGPC (load multi-company data) — not an AI gap.

## Legend
- ✅ **Answerable now** — fields present and populated
- 🏢 **Company-gap** — needs multi-company `CUSTOMER`/site data (currently ~single org)
- 📂 **Needs other data** — a field/table is sparse or absent (reasons, investigations, inventory prices, instrument calibration, interlab requests, retention/disposal)
- 🔮 **Prediction** — forecasting; feasible for time-series metrics, some are research-grade

## Sample Data Analytics
| Section | Capability | Readiness | Notes |
|---|---|---|---|
| Sample Processing Time (delivery, TAT) | NL→SQL + stats | ✅ / 🏢 | dates `LOGIN/RECD/STARTED/COMPLETED/DUE` present; "by company" splits 🏢 |
| Cancellation of samples | NL→SQL | ✅ counts / 📂 reason / 🏢 | `STATUS='X'` (3,784); "with reason (pre-defined list)" needs a cancel-reason field |
| Rejection of samples | NL→SQL | 📂 / 🏢 | needs a reject-reason field/flag |
| Dropping of samples | NL→SQL | 📂 / 🏢 | "dropped" not a clear status in the data |
| Unscheduled samples | NL→SQL | ✅ / 🏢 | `SAMPLE_TYPE='UNSCHEDULED'` (654) |
| Finished-product samples | NL→SQL | ✅ partial / 🏢 | via `PRODUCT` / `T_PRODUCT_TYPE` |
| **Out of Specification** | NL→SQL + stats | ✅✅ | `RESULT.IN_SPEC='F'` (3,020) by test/product/analyst/process-unit. Investigations 📂 |
| Test added to samples | NL→SQL | ✅ / 🏢 | `TEST/RESULT` by `ANALYSIS`, TAT from test dates |
| Sample registration | NL→SQL | ✅ / 🏢 | `SAMPLE_TYPE`, `PRODUCT`, `SAMPLING_POINT`, process unit |
| Test methods | NL→SQL | ✅ partial / 🏢 | `ANALYSIS` + method codes |
| Product added | NL→SQL | ✅ | `SAMPLE.PRODUCT` (42 products) |
| Result entry (parameters) | NL→SQL | ✅ | `RESULT.NAME / ANALYSIS` (267) |
| Completed samples (+ on-time) | NL→SQL | ✅ / 🏢 | `STATUS`, `DATE_COMPLETED` vs `DUE_DATE` |
| Pending / overdue | NL→SQL | ✅ partial / 🏢 | `DUE_DATE` vs completion |
| Repeatability / reproducibility | NL→SQL | 📂 / 🏢 | `C_REP/C_REPROD`/replicates need verification |
| Interlaboratory testing | NL→SQL | 📂 / 🏢 | needs request tables + company dimension |
| Authorization of tests/samples | NL→SQL | ✅ partial / 🏢 | status transitions; rates "by company" 🏢 |
| Rejection of completed samples/tests | NL→SQL | 📂 / 🏢 | needs reject reasons |
| Retention of samples | NL→SQL | 📂 partial / 🏢 | `PRODUCT.C_RETENTION_PERIOD` exists; events 📂 |
| Disposal of samples | NL→SQL | 📂 / 🏢 | needs disposal records |

## Inventory Data Analytics
| Section | Capability | Readiness | Notes |
|---|---|---|---|
| Chemicals consumption / suppliers / price | NL→SQL | 📂 / 🏢 | inventory tables present; **prices** likely sparse; verify |
| Inventory consumption / suppliers / price | NL→SQL | 📂 / 🏢 | same |

## Instrument Data Analytics
| Section | Capability | Readiness | Notes |
|---|---|---|---|
| Instrument vendor | NL→SQL | 📂 | instrument/vendor tables — verify population |
| Instrument management (calibration, offline, maintenance) | NL→SQL | 📂 | `TEST/RESULT.INSTRUMENT` is largely empty in this data → instrument-linked questions sparse |

## Advanced Data Analytics
| Section | Capability | Readiness | Notes |
|---|---|---|---|
| Ranges (Cetane, RON, RVP, Sulphur, Paraffins) | NL→SQL + stats | ✅ partial | Cetane ✅ (46–74), Octane ✅ (44–95.7); Sulphur/RVP/Paraffins need exact analysis-code mapping |
| Quality trend of finished products | forecasting/stats | ✅ | trend over result values by product/time |
| Best analysts / most effective testing | RCA / stats | ✅ partial | by OOS rate / accuracy proxies |
| Failures due to human error / instrument failure | RCA | 📂 | needs error/failure reason fields |
| Prediction Analytics (next month/year volumes, TAT, authorizations, expiry, overload) | forecasting | 🔮 | time-series ones feasible; "failure reason next month" research-grade |

## Rough split (of ~180)
- **✅ Answerable now:** OOS (all angles), products, tests/methods, parameters, sample types, statuses, ranges, TAT, trends — the core analytics value.
- **🏢 Company-gap:** the large "by each company / top-N companies / interlaboratory" set — **data requirement**, not code.
- **📂 Needs other data:** reasons (cancel/reject), investigations, inventory prices, instrument calibration, retention/disposal.
- **🔮 Prediction:** the forecasting section.

The verbatim, scored subset with real expected answers is in [golden_egpc.jsonl](golden_egpc.jsonl); see [README.md](README.md).
