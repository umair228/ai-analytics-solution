"""
Seed the DSE platform with the Refinery LIMS demo (Refinery Lab).

Registers the refinery SQLite warehouse (built by
``seed_refinery/build_warehouse.py``) as a data source, then creates the saved
queries, cached datasets, interactive dashboards, alerts and scheduled reports
that show the customer's use cases end-to-end:

    Instruments · Inventory · Sample & QC analytics · Computer-vision results
    · Document register (chatbot) · Forecasts (separate forecasting app)

This is additive — it does not touch the existing Water Quality demo.

Run:
    .venv/bin/python -m seed_refinery.build_warehouse      # build the warehouse
    .venv/bin/python seed_refinery_demo.py                 # wire up the platform
"""
import os
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.utils import timezone  # noqa: E402

from accounts.models import User  # noqa: E402
from connections.models import DataSource  # noqa: E402
from dashboards.models import Dashboard, Widget  # noqa: E402
from datasets.models import (  # noqa: E402
    AlertEvent, Dataset, DatasetAlert, DatasetReport,
)
from datasets.services import refresh_dataset  # noqa: E402
from querybuilder.models import QueryDefinition  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
# Committed warehouse (forecasting/warehouse/) preferred; legacy media/ fallback.
_COMMITTED_WH = BASE_DIR / "forecasting" / "warehouse" / "refinery_lims.sqlite3"
WAREHOUSE = _COMMITTED_WH if _COMMITTED_WH.exists() else BASE_DIR / "media" / "refinery_lims.sqlite3"
DS_NAME = "Refinery LIMS"

# ──────────────────────────────────────────────────────────────────────────
# Saved queries (key, name, description, SQL) — SQLite dialect.
# ──────────────────────────────────────────────────────────────────────────
QUERIES = [
    # ---- Operations -----------------------------------------------------
    ("sample_register", "Sample Register (live)",
     "Row-level sample login register — every sample with product, section, "
     "priority, analyst, turnaround and result. Powers the Data Explorer.",
     """SELECT TEXT_ID, LOGIN_DATE, SECTION, PRODUCT_NAME, SAMPLING_POINT,
       PRIORITY, STATUS_LABEL, ANALYST, ROUND(TAT_HOURS,1) AS TAT_HOURS, RESULT
FROM SAMPLE
ORDER BY LOGIN_DATE DESC
LIMIT 2000"""),

    ("samples_by_section", "Samples by Section",
     "Total samples processed by each laboratory section.",
     """SELECT SECTION, COUNT(*) AS samples
FROM SAMPLE
GROUP BY SECTION
ORDER BY samples DESC"""),

    ("monthly_volume_tat", "Monthly Volume & Turnaround",
     "Samples logged per month with average turnaround and on-time %% "
     "(last 24 months).",
     """SELECT LOGIN_MONTH AS month,
       COUNT(*) AS samples,
       ROUND(AVG(TAT_HOURS),1) AS avg_tat_hours,
       ROUND(100.0*SUM(CASE WHEN ON_TIME=1 THEN 1 ELSE 0 END)
             / SUM(CASE WHEN STATUS='C' THEN 1 ELSE 0 END),1) AS on_time_pct
FROM SAMPLE
WHERE LOGIN_MONTH >= '2024-06'
GROUP BY LOGIN_MONTH
ORDER BY month"""),

    ("status_breakdown", "Sample Status Breakdown",
     "How many samples sit in each workflow status.",
     """SELECT STATUS_LABEL AS status, COUNT(*) AS samples
FROM SAMPLE
GROUP BY STATUS_LABEL
ORDER BY samples DESC"""),

    ("tat_by_priority", "Turnaround by Priority",
     "Average turnaround and on-time %% by sample priority.",
     """SELECT PRIORITY,
       COUNT(*) AS samples,
       ROUND(AVG(TAT_HOURS),1) AS avg_tat_hours,
       ROUND(100.0*SUM(CASE WHEN ON_TIME=1 THEN 1 ELSE 0 END)
             / SUM(CASE WHEN STATUS='C' THEN 1 ELSE 0 END),1) AS on_time_pct
FROM SAMPLE
GROUP BY PRIORITY
ORDER BY avg_tat_hours DESC"""),

    ("samples_by_product", "Top Products by Volume",
     "The 20 most-tested product streams.",
     """SELECT PRODUCT_NAME AS product, SECTION, COUNT(*) AS samples
FROM SAMPLE
GROUP BY PRODUCT_NAME, SECTION
ORDER BY samples DESC
LIMIT 20"""),

    # ---- Quality / spec compliance --------------------------------------
    ("spec_by_product", "Spec Compliance by Product",
     "First-pass in-spec rate per product stream (worst first).",
     """SELECT PRODUCT AS product,
       COUNT(*) AS tests,
       SUM(in_spec) AS in_spec,
       ROUND(100.0*SUM(in_spec)/COUNT(*),1) AS in_spec_pct
FROM test_results
GROUP BY PRODUCT
HAVING tests > 50
ORDER BY in_spec_pct ASC
LIMIT 20"""),

    ("quality_trend", "Monthly Off-Spec Trend",
     "Monthly test volume and off-spec %% (last 24 months).",
     """SELECT result_month AS month,
       COUNT(*) AS tests,
       SUM(1-in_spec) AS off_spec,
       ROUND(100.0*SUM(1-in_spec)/COUNT(*),1) AS off_spec_pct
FROM test_results
WHERE result_month >= '2024-06'
GROUP BY result_month
ORDER BY month"""),

    ("firstpass_by_section", "First-Pass Quality by Section",
     "In-spec %% of all results by laboratory section.",
     """SELECT SECTION,
       COUNT(*) AS tests,
       ROUND(100.0*SUM(in_spec)/COUNT(*),1) AS in_spec_pct
FROM test_results
GROUP BY SECTION
ORDER BY in_spec_pct ASC"""),

    ("sulfur_trend", "Sulfur Results Trend (ppm)",
     "Monthly average total-sulfur (ppm / mg-kg methods) — the key product "
     "release metric.",
     """SELECT result_month AS month,
       ROUND(AVG(value),1) AS avg_sulfur_ppm,
       COUNT(*) AS tests
FROM test_results
WHERE test_name LIKE '%ulfur%'
  AND (unit LIKE '%ppm%' OR unit LIKE '%mg/kg%')
  AND result_month >= '2024-06'
GROUP BY result_month
ORDER BY month"""),

    ("offspec_recent", "Off-Spec Results (recent)",
     "Latest 500 individual results that breached their ASTM spec limits.",
     """SELECT TEXT_ID, PRODUCT, test_name, astm_method,
       ROUND(value,3) AS value, unit, spec_min, spec_max, instrument, result_date
FROM test_results
WHERE in_spec = 0
ORDER BY result_date DESC
LIMIT 500"""),

    ("test_by_method", "Test Volume by ASTM Method",
     "The 20 highest-volume ASTM/IP methods run in the lab.",
     """SELECT astm_method AS method,
       COUNT(*) AS tests,
       ROUND(100.0*SUM(in_spec)/COUNT(*),1) AS in_spec_pct
FROM test_results
GROUP BY astm_method
ORDER BY tests DESC
LIMIT 20"""),

    # ---- Instruments ----------------------------------------------------
    ("instr_uptime", "Instrument Uptime Scorecard",
     "Per-instrument rated uptime, status and calibration position.",
     """SELECT asset_code AS instrument, name, section,
       uptime_pct, status, next_calibration, calibration_overdue
FROM instruments
ORDER BY uptime_pct ASC"""),

    ("monthly_downtime", "Monthly Downtime (fleet hours)",
     "Total instrument downtime hours per month across the fleet "
     "(from the ON/OFF event log).",
     """WITH ev AS (
   SELECT INSTRUMENT, EVENT_TYPE, ENTERED_ON,
          LEAD(ENTERED_ON) OVER (PARTITION BY INSTRUMENT ORDER BY ENTERED_ON) AS nxt
   FROM INSTRUMENTS1_LOG
)
SELECT strftime('%Y-%m', ENTERED_ON) AS month,
       ROUND(SUM((julianday(nxt)-julianday(ENTERED_ON))*24),0) AS downtime_hours
FROM ev
WHERE EVENT_TYPE='OFF' AND nxt IS NOT NULL
  AND strftime('%Y-%m', ENTERED_ON) >= '2024-06'
GROUP BY month
ORDER BY month"""),

    ("downtime_by_instrument", "Downtime by Instrument (12 mo)",
     "Total downtime hours per instrument over the last 12 months.",
     """WITH ev AS (
   SELECT INSTRUMENT, EVENT_TYPE, ENTERED_ON,
          LEAD(ENTERED_ON) OVER (PARTITION BY INSTRUMENT ORDER BY ENTERED_ON) AS nxt
   FROM INSTRUMENTS1_LOG
)
SELECT INSTRUMENT AS instrument,
       ROUND(SUM((julianday(nxt)-julianday(ENTERED_ON))*24),0) AS downtime_hours
FROM ev
WHERE EVENT_TYPE='OFF' AND nxt IS NOT NULL
  AND ENTERED_ON >= '2025-06-01'
GROUP BY INSTRUMENT
ORDER BY downtime_hours DESC
LIMIT 20"""),

    ("calibration_status", "Calibration Status",
     "Each instrument's last/next calibration and overdue flag.",
     """SELECT asset_code AS instrument, name, section,
       last_calibration, next_calibration,
       CASE WHEN calibration_overdue=1 THEN 'OVERDUE' ELSE 'OK' END AS calibration_status
FROM instruments
ORDER BY calibration_overdue DESC, next_calibration ASC"""),

    # ---- Inventory ------------------------------------------------------
    ("stock_levels", "Inventory Stock Levels",
     "Current stock vs reorder level for every reagent / standard / gas.",
     """SELECT DESCRIPTION AS item, INVENTORY_TYPE AS category,
       CURRENT_STOCK, REORDER_LEVEL, UNITS,
       CASE WHEN BELOW_REORDER=1 THEN 'BELOW REORDER' ELSE 'OK' END AS stock_status,
       SUPPLIER
FROM INVENTORY_ITEM
ORDER BY BELOW_REORDER DESC, CURRENT_STOCK ASC"""),

    ("below_reorder", "Items Below Reorder Level",
     "Reagents / standards that have dropped below their reorder point.",
     """SELECT DESCRIPTION AS item, INVENTORY_TYPE AS category,
       CURRENT_STOCK, REORDER_LEVEL, UNITS, SUPPLIER
FROM INVENTORY_ITEM
WHERE BELOW_REORDER = 1
ORDER BY CURRENT_STOCK ASC"""),

    ("monthly_consumption", "Monthly Reagent Consumption",
     "Total quantity issued (PULL) per month (last 24 months).",
     """SELECT TRANS_MONTH AS month,
       ROUND(SUM(QUANTITY),0) AS qty_pulled,
       COUNT(*) AS transactions
FROM INVENTORY_TRANS
WHERE TRANSACTION_TYPE='PULL' AND TRANS_MONTH >= '2024-06'
GROUP BY TRANS_MONTH
ORDER BY month"""),

    ("top_consumed", "Top Consumed Items",
     "The 15 highest-consumption stock items by total quantity pulled.",
     """SELECT it.DESCRIPTION AS item, it.UNITS,
       ROUND(SUM(tr.QUANTITY),0) AS total_pulled
FROM INVENTORY_TRANS tr
JOIN INVENTORY_ITEM it ON tr.INVENTORY_ITEM = it.ITEM_NUMBER
WHERE tr.TRANSACTION_TYPE='PULL'
GROUP BY it.DESCRIPTION, it.UNITS
ORDER BY total_pulled DESC
LIMIT 15"""),

    ("meoh_etoh", "Methanol vs Ethanol Consumption",
     "Monthly methanol / ethanol consumption — the stocks driving the "
     "inventory forecast.",
     """SELECT TRANS_MONTH AS month, STOCK,
       ROUND(SUM(QUANTITY),0) AS qty
FROM INVENTORY_TRANS
WHERE STOCK IN ('METHANOL','ETHANOL') AND TRANSACTION_TYPE='PULL'
  AND TRANS_MONTH >= '2024-01'
GROUP BY TRANS_MONTH, STOCK
ORDER BY month"""),

    # ---- Computer-vision ------------------------------------------------
    ("vision_by_usecase", "Vision Inspections by Use Case",
     "Image inspections, pass-rate and model confidence per vision use case.",
     """SELECT use_case,
       COUNT(*) AS inspections,
       ROUND(100.0*SUM(CASE WHEN status='Pass' THEN 1 ELSE 0 END)/COUNT(*),1) AS pass_pct,
       ROUND(AVG(confidence),3) AS avg_confidence
FROM vision_inspections
GROUP BY use_case
ORDER BY inspections DESC"""),

    ("vision_trend", "Vision Pass/Fail Trend",
     "Monthly image-inspection volumes by outcome.",
     """SELECT inspect_month AS month,
       COUNT(*) AS inspections,
       SUM(CASE WHEN status='Pass' THEN 1 ELSE 0 END) AS passed,
       SUM(CASE WHEN status='Fail' THEN 1 ELSE 0 END) AS failed,
       SUM(CASE WHEN status='Review' THEN 1 ELSE 0 END) AS review
FROM vision_inspections
WHERE inspect_month >= '2025-01'
GROUP BY inspect_month
ORDER BY month"""),

    ("vision_review", "Vision — Needs Review",
     "Low-confidence or failed image inspections queued for analyst review.",
     """SELECT image_code, use_case, category_predicted,
       ROUND(confidence,3) AS confidence, status, section, inspected_on
FROM vision_inspections
WHERE status='Review' OR confidence < 0.70
ORDER BY confidence ASC
LIMIT 300"""),

    # ---- Documents ------------------------------------------------------
    ("doc_register", "Document Register",
     "Controlled lab documents — SOPs, ASTM/IP methods, COA, MSDS, calibration "
     "certs. Backs the document-search assistant.",
     """SELECT doc_code, title, doc_type, section, standard_ref, version, status
FROM documents
ORDER BY doc_type, doc_code"""),

    ("docs_by_type", "Documents by Type",
     "Controlled-document count by type.",
     """SELECT doc_type, COUNT(*) AS documents
FROM documents
GROUP BY doc_type
ORDER BY documents DESC"""),
]

# ──────────────────────────────────────────────────────────────────────────
# Dashboards — list of (name, description, [widget dicts]).
# widget dict: title, kind, ds (query key), config, x, y, w, h
# ──────────────────────────────────────────────────────────────────────────
DASHBOARDS = [
    ("Refinery Lab Operations",
     "Live operational view of sample throughput, turnaround and quality "
     "across all laboratory sections.",
     [
        dict(title="Total Samples", kind="kpi", ds="samples_by_section",
             config={"value_field": "samples", "rollup": "sum"}, x=0, y=0, w=3, h=3),
        dict(title="Avg Turnaround (hrs)", kind="kpi", ds="monthly_volume_tat",
             config={"value_field": "avg_tat_hours", "rollup": "avg"}, x=3, y=0, w=3, h=3),
        dict(title="On-Time %", kind="kpi", ds="monthly_volume_tat",
             config={"value_field": "on_time_pct", "rollup": "avg"}, x=6, y=0, w=3, h=3),
        dict(title="Off-Spec %", kind="kpi", ds="quality_trend",
             config={"value_field": "off_spec_pct", "rollup": "avg"}, x=9, y=0, w=3, h=3),
        dict(title="Monthly Sample Volume", kind="line", ds="monthly_volume_tat",
             config={"category_field": "month", "value_field": "samples", "smooth": True},
             x=0, y=3, w=8, h=6),
        dict(title="Sample Status", kind="pie", ds="status_breakdown",
             config={"category_field": "status", "value_field": "samples"}, x=8, y=3, w=4, h=6),
        dict(title="Samples by Section", kind="bar", ds="samples_by_section",
             config={"category_field": "SECTION", "value_field": "samples"}, x=0, y=9, w=6, h=6),
        dict(title="Top Products by Volume", kind="bar", ds="samples_by_product",
             config={"category_field": "product", "value_field": "samples"}, x=6, y=9, w=6, h=6),
        dict(title="Turnaround by Priority", kind="bar", ds="tat_by_priority",
             config={"category_field": "PRIORITY", "value_field": "avg_tat_hours"}, x=0, y=15, w=5, h=6),
        dict(title="Sample Register", kind="table", ds="sample_register",
             config={}, x=5, y=15, w=7, h=6),
     ]),

    ("Quality & Spec Compliance",
     "Product spec-compliance, off-spec trend and the sulfur release metric "
     "across all streams.",
     [
        dict(title="Tests Run", kind="kpi", ds="spec_by_product",
             config={"value_field": "tests", "rollup": "sum"}, x=0, y=0, w=3, h=3),
        dict(title="Off-Spec %", kind="kpi", ds="quality_trend",
             config={"value_field": "off_spec_pct", "rollup": "avg"}, x=3, y=0, w=3, h=3),
        dict(title="Products Monitored", kind="kpi", ds="spec_by_product",
             config={"value_field": "product", "rollup": "count"}, x=6, y=0, w=3, h=3),
        dict(title="Avg Sulfur (ppm)", kind="kpi", ds="sulfur_trend",
             config={"value_field": "avg_sulfur_ppm", "rollup": "avg"}, x=9, y=0, w=3, h=3),
        dict(title="Spec Compliance by Product", kind="bar", ds="spec_by_product",
             config={"category_field": "product", "value_field": "in_spec_pct"}, x=0, y=3, w=7, h=6),
        dict(title="First-Pass Quality by Section", kind="bar", ds="firstpass_by_section",
             config={"category_field": "SECTION", "value_field": "in_spec_pct"}, x=7, y=3, w=5, h=6),
        dict(title="Monthly Off-Spec Trend", kind="line", ds="quality_trend",
             config={"category_field": "month", "value_field": "off_spec_pct", "smooth": True},
             x=0, y=9, w=6, h=6),
        dict(title="Sulfur Results Trend (ppm)", kind="line", ds="sulfur_trend",
             config={"category_field": "month", "value_field": "avg_sulfur_ppm", "smooth": True},
             x=6, y=9, w=6, h=6),
        dict(title="Off-Spec Results (recent)", kind="table", ds="offspec_recent",
             config={}, x=0, y=15, w=12, h=6),
     ]),

    ("Instrument Performance & Calibration",
     "Instrument uptime, downtime trend and calibration position — the data "
     "behind the downtime forecast.",
     [
        dict(title="Avg Uptime %", kind="kpi", ds="instr_uptime",
             config={"value_field": "uptime_pct", "rollup": "avg"}, x=0, y=0, w=3, h=3),
        dict(title="Instruments", kind="kpi", ds="instr_uptime",
             config={"value_field": "instrument", "rollup": "count"}, x=3, y=0, w=3, h=3),
        dict(title="Calibrations Overdue", kind="kpi", ds="instr_uptime",
             config={"value_field": "calibration_overdue", "rollup": "sum"}, x=6, y=0, w=3, h=3),
        dict(title="Downtime Hrs (peak mo)", kind="kpi", ds="monthly_downtime",
             config={"value_field": "downtime_hours", "rollup": "max"}, x=9, y=0, w=3, h=3),
        dict(title="Instrument Uptime Scorecard", kind="bar", ds="instr_uptime",
             config={"category_field": "instrument", "value_field": "uptime_pct"}, x=0, y=3, w=7, h=6),
        dict(title="Downtime by Instrument (12 mo)", kind="bar", ds="downtime_by_instrument",
             config={"category_field": "instrument", "value_field": "downtime_hours"}, x=7, y=3, w=5, h=6),
        dict(title="Monthly Fleet Downtime (hrs)", kind="line", ds="monthly_downtime",
             config={"category_field": "month", "value_field": "downtime_hours", "smooth": True},
             x=0, y=9, w=12, h=6),
        dict(title="Calibration Status", kind="table", ds="calibration_status",
             config={}, x=0, y=15, w=12, h=6),
     ]),

    ("Inventory & Reagent Consumption",
     "Reagent / standard / gas stock levels and consumption — the data behind "
     "the inventory forecast.",
     [
        dict(title="Stock Items", kind="kpi", ds="stock_levels",
             config={"value_field": "item", "rollup": "count"}, x=0, y=0, w=3, h=3),
        dict(title="Below Reorder", kind="kpi", ds="below_reorder",
             config={"value_field": "item", "rollup": "count"}, x=3, y=0, w=3, h=3),
        dict(title="Qty Pulled (peak mo)", kind="kpi", ds="monthly_consumption",
             config={"value_field": "qty_pulled", "rollup": "max"}, x=6, y=0, w=3, h=3),
        dict(title="PULL Transactions", kind="kpi", ds="monthly_consumption",
             config={"value_field": "transactions", "rollup": "sum"}, x=9, y=0, w=3, h=3),
        dict(title="Monthly Consumption", kind="line", ds="monthly_consumption",
             config={"category_field": "month", "value_field": "qty_pulled", "smooth": True},
             x=0, y=3, w=7, h=6),
        dict(title="Methanol vs Ethanol", kind="line", ds="meoh_etoh",
             config={"category_field": "month", "value_field": "qty", "series_field": "STOCK"},
             x=7, y=3, w=5, h=6),
        dict(title="Top Consumed Items", kind="bar", ds="top_consumed",
             config={"category_field": "item", "value_field": "total_pulled"}, x=0, y=9, w=7, h=6),
        dict(title="Items Below Reorder", kind="table", ds="below_reorder",
             config={}, x=7, y=9, w=5, h=6),
        dict(title="Inventory Stock Levels", kind="table", ds="stock_levels",
             config={}, x=0, y=15, w=12, h=6),
     ]),

    ("Computer-Vision Inspections",
     "AI image-processing results across the lab's vision use cases — colour "
     "grading, corrosion scoring, chromatogram QC, gauge OCR and more.",
     [
        dict(title="Total Inspections", kind="kpi", ds="vision_by_usecase",
             config={"value_field": "inspections", "rollup": "sum"}, x=0, y=0, w=3, h=3),
        dict(title="Avg Pass %", kind="kpi", ds="vision_by_usecase",
             config={"value_field": "pass_pct", "rollup": "avg"}, x=3, y=0, w=3, h=3),
        dict(title="Avg Confidence", kind="kpi", ds="vision_by_usecase",
             config={"value_field": "avg_confidence", "rollup": "avg"}, x=6, y=0, w=3, h=3),
        dict(title="Use Cases", kind="kpi", ds="vision_by_usecase",
             config={"value_field": "use_case", "rollup": "count"}, x=9, y=0, w=3, h=3),
        dict(title="Inspections by Use Case", kind="bar", ds="vision_by_usecase",
             config={"category_field": "use_case", "value_field": "inspections"}, x=0, y=3, w=7, h=6),
        dict(title="Pass-Rate by Use Case", kind="bar", ds="vision_by_usecase",
             config={"category_field": "use_case", "value_field": "pass_pct"}, x=7, y=3, w=5, h=6),
        dict(title="Inspection Volume Trend", kind="line", ds="vision_trend",
             config={"category_field": "month", "value_field": "inspections", "smooth": True},
             x=0, y=9, w=7, h=6),
        dict(title="Needs Review", kind="table", ds="vision_review",
             config={}, x=7, y=9, w=5, h=6),
     ]),

    ("Document Register & Knowledge Base",
     "Controlled laboratory documents (SOPs, ASTM/IP methods, COA, MSDS) that "
     "the document-search assistant answers over.",
     [
        dict(title="Total Documents", kind="kpi", ds="doc_register",
             config={"value_field": "doc_code", "rollup": "count"}, x=0, y=0, w=3, h=3),
        dict(title="Document Types", kind="kpi", ds="docs_by_type",
             config={"value_field": "doc_type", "rollup": "count"}, x=3, y=0, w=3, h=3),
        dict(title="Documents by Type", kind="pie", ds="docs_by_type",
             config={"category_field": "doc_type", "value_field": "documents"}, x=6, y=0, w=6, h=6),
        dict(title="Document Register", kind="table", ds="doc_register",
             config={}, x=0, y=6, w=12, h=8),
     ]),
]

# (name, ds key, column, aggregation, condition, threshold, email)
ALERTS = [
    ("Off-Spec Rate High — Lab", "quality_trend", "off_spec_pct", "max", "gt", 8.0),
    ("Sulfur Above Release Limit", "sulfur_trend", "avg_sulfur_ppm", "max", "gt", 10.0),
    ("On-Time Turnaround Below Target", "monthly_volume_tat", "on_time_pct", "min", "lt", 85.0),
    ("Instrument Calibration Overdue", "instr_uptime", "calibration_overdue", "sum", "gt", 0.0),
    ("Reagent Below Reorder Level", "below_reorder", "CURRENT_STOCK", "count", "gt", 0.0),
    ("Low Vision Model Confidence", "vision_by_usecase", "avg_confidence", "min", "lt", 0.80),
]

# (name, ds key, recipients, schedule)
REPORTS = [
    ("Daily Lab Operations Digest", "monthly_volume_tat",
     "lab.supervisor@refinery-lab.com,quality@refinery-lab.com", "daily"),
    ("Weekly Spec-Compliance Report", "spec_by_product",
     "quality@refinery-lab.com,operations@refinery-lab.com", "weekly"),
    ("Monthly Inventory & Reagent Report", "stock_levels",
     "stores@refinery-lab.com,lab.supervisor@refinery-lab.com", "monthly"),
]


# ──────────────────────────────────────────────────────────────────────────
def main():
    if not WAREHOUSE.exists():
        raise SystemExit(
            f"Warehouse not found at {WAREHOUSE}. Run:\n"
            f"  .venv/bin/python -m seed_refinery.build_warehouse"
        )

    admin = (User.objects.filter(role=User.Role.ADMIN).order_by("id").first()
             or User.objects.order_by("id").first())
    print(f"Owner: {admin.username}")

    # --- Connection -------------------------------------------------------
    ds, _ = DataSource.objects.update_or_create(
        name=DS_NAME,
        defaults=dict(
            description="the refinery LabWare LIMS "
                        "warehouse — samples, ASTM test results, instruments, "
                        "inventory, vision inspections and the document register.",
            source_type="sqlite",
            options={"path": str(WAREHOUSE)},
            owner=admin,
            visibility="shared",
            test_status="ok",
            last_tested_at=timezone.now(),
            last_test_error="",
        ),
    )
    print(f"Connection: {ds.name} -> {WAREHOUSE}")

    # Idempotency: clear this data source's prior queries/datasets and the
    # refinery dashboards/alerts/reports.
    Dataset.objects.filter(query__datasource=ds).delete()
    QueryDefinition.objects.filter(datasource=ds).delete()
    dash_names = [d[0] for d in DASHBOARDS]
    Dashboard.objects.filter(name__in=dash_names, owner=admin).delete()

    # --- Saved queries + datasets ----------------------------------------
    print("\n=== Queries & Datasets ===")
    datasets = {}
    for key, name, desc, sql in QUERIES:
        query = QueryDefinition.objects.create(
            name=name, description=desc, datasource=ds,
            mode=QueryDefinition.Mode.RAW, raw_sql=sql, generated_sql=sql,
            owner=admin, visibility="shared",
        )
        dataset = Dataset.objects.create(
            name=name, description=desc, query=query,
            owner=admin, visibility="shared",
            refresh_interval=Dataset.RefreshInterval.DAILY,
        )
        try:
            result = refresh_dataset(dataset)
            query.last_run_at = timezone.now()
            query.last_row_count = result["row_count"]
            query.save(update_fields=["last_run_at", "last_row_count"])
            print(f"  {name:42s} {result['row_count']:>6,d} rows")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:42s} ERROR: {exc}")
        datasets[key] = dataset

    # --- Dashboards -------------------------------------------------------
    print("\n=== Dashboards ===")
    for name, desc, widgets in DASHBOARDS:
        dash = Dashboard.objects.create(
            name=name, description=desc, owner=admin, visibility="shared",
        )
        for order, w in enumerate(widgets):
            Widget.objects.create(
                dashboard=dash, title=w["title"], widget_type=w["kind"],
                dataset=datasets[w["ds"]], config=w["config"],
                x=w["x"], y=w["y"], w=w["w"], h=w["h"], order=order,
            )
        print(f"  {name:42s} {len(widgets)} widgets")

    # --- Alerts -----------------------------------------------------------
    print("\n=== Alerts ===")
    DatasetAlert.objects.filter(name__in=[a[0] for a in ALERTS]).delete()
    alert_objs = {}
    for name, key, column, agg, cond, threshold in ALERTS:
        a = DatasetAlert.objects.create(
            name=name, dataset=datasets[key], column=column, aggregation=agg,
            condition=cond, threshold=threshold, is_active=True,
            notify_email="lab.supervisor@refinery-lab.com", owner=admin,
        )
        alert_objs[name] = a
    print(f"  {len(alert_objs)} alerts")

    # Seed a couple of past alert events so the Events tab isn't empty.
    AlertEvent.objects.get_or_create(
        alert=alert_objs["Sulfur Above Release Limit"], triggered_value=12.4,
        defaults=dict(message="Alert 'Sulfur Above Release Limit' triggered: "
                              "max(avg_sulfur_ppm) > 10.0 (current value: 12.4 ppm)",
                      acknowledged=False),
    )
    AlertEvent.objects.get_or_create(
        alert=alert_objs["Instrument Calibration Overdue"], triggered_value=3,
        defaults=dict(message="Alert 'Instrument Calibration Overdue' triggered: "
                              "sum(calibration_overdue) > 0 (current value: 3 instruments)",
                      acknowledged=False),
    )
    AlertEvent.objects.get_or_create(
        alert=alert_objs["Reagent Below Reorder Level"], triggered_value=6,
        defaults=dict(message="Alert 'Reagent Below Reorder Level' triggered: "
                              "count below reorder > 0 (current value: 6 items)",
                      acknowledged=True),
    )

    # --- Reports ----------------------------------------------------------
    print("\n=== Reports ===")
    DatasetReport.objects.filter(name__in=[r[0] for r in REPORTS]).delete()
    for name, key, emails, schedule in REPORTS:
        DatasetReport.objects.create(
            name=name, dataset=datasets[key], recipient_emails=emails,
            schedule=schedule, is_active=True,
        )
    print(f"  {len(REPORTS)} reports")

    print("\n✓ Refinery LIMS demo seeded.")
    print(f"  Connection : {DS_NAME}")
    print(f"  Datasets   : {len(QUERIES)}")
    print(f"  Dashboards : {len(DASHBOARDS)}")
    print(f"  Alerts     : {len(ALERTS)}  |  Reports: {len(REPORTS)}")


if __name__ == "__main__":
    main()
