# Refinery Refinery (Refinery Lab) — DSE Demo Walkthrough

A self-contained LabWare-LIMS analytics demo for **Saudi Refinery Refinery Refinery
Complex**, built on the DSE Interactive Reporting & AI Analytics platform. Every
feature runs off **one SQLite warehouse** (`media/refinery_lims.sqlite3`) — no SQL
Server needed during the demo.

Maps directly to the customer's stated use cases:
**instruments · inventory · computer vision · document search (chatbot) ·
interactive reports & charts · forecasts.**

---

## 1. Rebuild / run (do this once before the demo)

```bash
cd ai-analytics-solution
.venv/bin/python -m seed_refinery.build_warehouse   # build the SQLite warehouse (~2s)
.venv/bin/python seed_refinery_demo.py              # connection, datasets, dashboards, alerts, reports
.venv/bin/python -m seed_refinery.prewarm           # train + cache all forecast models (~6-8 min, one-time)

# start the stack
.venv/bin/python manage.py runserver                # backend  :8000
# (frontend) interactive-analytics-dse:  npm run dev  ->  :5173
```

Forecasts read SQLite because `.env` now has `DB_ENGINE=sqlite`. To point the
forecasts back at the live LabWare LIMS, set `DB_ENGINE=mssql` in `.env`.

Log in with the platform admin (`admin` / see `.env` `DSE_ADMIN_PASSWORD`).

---

## 2. The data (what's in the warehouse)

| Area | Table(s) | Volume |
|---|---|---|
| Lab sections | `sections` (8) + `analysts` (16) | Crude & Distillation, Gasoline/Mogas, Naphtha & Aromatics, Middle Distillates, Fuel Oil & Asphalt, Gas & LPG, Environmental & Water, QA/QC |
| Samples | `SAMPLE` | ~22,000 (2021→2026), live WIP backlog |
| QC test results | `test_results` | ~95,000 real ASTM/IP results vs spec limits (~3% off-spec) |
| Instruments | `instruments` (30) + `INSTRUMENTS1_LOG` | ON/OFF event log, 2021→2026 |
| Calibration | `calibrations` | per-instrument history + overdue flags |
| Inventory | `INVENTORY_ITEM` (41) + `INVENTORY_TRANS` | PULL/RECEIVE, incl. methanol/ethanol |
| Computer vision | `vision_inspections` | ~1,500 inspections across 6 use cases |
| Documents | `documents` (27) | SOPs, ASTM methods, COA, MSDS, cal certs |

Authentic petroleum products (Mogas 91/95, Jet A-1, ULSD 10 ppm, LPG, benzene,
bitumen 60/70, RFO 380, Arabian Heavy crude…) and methods (D2699 octane, D5191
RVP, D5453 sulfur, D86 distillation, D93 flash, D613 cetane, D445 viscosity…).

---

## 3. Demo script — use case by use case

### A. Database connectivity → dataset builder (the platform story)
- **Connections** → show **"Refinery LIMS"** (SQLite) — *Test connection* = OK.
  Mention the same builder connects to MSSQL/Oracle/Postgres/MySQL/Excel/CSV.
- **Query Builder / Queries** → open *"Spec Compliance by Product"* — show the SQL,
  *Run*, then it's saved as a reusable **Dataset**.
- **Data Explorer** → *"Sample Register (live)"* — row-level sample login register.

### B. Interactive reports & charts (dashboards)
Six ready dashboards (Dashboards page):
1. **Refinery Lab Operations** — KPIs (samples, turnaround, on-time %, off-spec %),
   monthly volume, samples by section, status, top products.
2. **Quality & Spec Compliance** — spec compliance by product, off-spec trend,
   first-pass by section, **sulfur release trend**, off-spec results table.
3. **Instrument Performance & Calibration** — uptime scorecard, downtime by
   instrument, monthly fleet downtime, calibration status.
4. **Inventory & Reagent Consumption** — stock vs reorder, monthly consumption,
   methanol vs ethanol, top consumed, below-reorder list.
5. **Computer-Vision Inspections** — inspections & pass-rate by use case, trend,
   needs-review queue.
6. **Document Register & Knowledge Base** — documents by type + register table.

### C. Instruments  → **Instrument Downtime** forecast page
- Default instrument **GC-401** (Detailed Hydrocarbon Analysis GC). Shows
  historical downtime hours, 6-month NeuralProphet forecast, uptime %, control
  limit and an AI insight brief. Try **CFR-OCTANE-01**, **ADU-05**.
- Pair with dashboard #3 for the calibration/overdue story.

### D. Inventory  → **Inventory Forecast** page
- Stock **Methanol** (and **Ethanol**) — monthly consumption forecast with past
  vs forecast quartiles. Pair with dashboard #4.

### E. Sample workload  → **Sample Forecast** page
- *All sections* combined, then drill into a section (e.g. **Middle Distillates**,
  **Gasoline/Mogas**) — per-section monthly sample-volume forecast.

### F. Document search via chatbot  → **AI Assistant**
- Open **Assistant**, attach the **"Document Register"** dataset, ask e.g.
  *"Which method covers gasoline RVP?"* or *"Do we have an SOP for QC control charts?"*
  Claude answers grounded in the controlled-document register.
- Also: attach any dataset and click **Generate Insights**, or use
  **AI widget suggestions** when building a dashboard.

### G. Computer vision for image processing  → dashboard #5
- Six refinery vision use cases: ASTM colour/appearance grading, chromatogram
  peak QC, crude/sediment classification, **corrosion-coupon scoring (D130)**,
  gauge/meter OCR, distillation-flask boil monitoring — with pass-rates,
  confidence and a review queue. (Results modelled in LIMS; live inference is the
  next integration step.)

### H. Alerts & scheduled reports
- **Alerts** page: off-spec rate, sulfur above limit, turnaround below target,
  calibration overdue, reagent below reorder, low vision confidence (with a few
  seeded events). **Reports**: daily ops digest, weekly spec compliance, monthly
  inventory.

---

## 4. Notes
- Additive: the existing **Water Quality** demo is untouched.
- Old Sharjah-municipality forecast models are archived under
  `forecasting/artifacts/_municipality_backup/`.
- Reference data the warehouse is generated from: `seed_refinery/reference.json`
  (researched ASTM specs, instruments, reagents, documents, KPIs).
