"""Refinery LIMS demo *platform* seed — shared by the standalone
``seed_refinery_demo.py`` script and the ``manage.py seed_demo`` command.

Registers the committed refinery SQLite warehouse as a data source and creates
the saved queries, cached datasets, interactive dashboards, threshold alerts, a
realistic alert-event (notification) history and scheduled reports that show the
oil-&-gas laboratory use cases end-to-end.

Importing this module does NOT call ``django.setup()`` — the caller is either a
management command (Django already configured) or the thin standalone wrapper,
which sets Django up first. That is what lets the same logic run on production
(``config.settings.prod``) instead of being pinned to the dev settings.
"""
import random
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from accounts.models import User
from connections.models import DataSource
from dashboards.models import Dashboard, Widget
from datasets.models import AlertEvent, Dataset, DatasetAlert, DatasetReport
from datasets.services import refresh_dataset
from querybuilder.models import QueryDefinition

# Repo root = seed_refinery/..  →  the committed warehouse ships in the image.
BASE_DIR = Path(__file__).resolve().parent.parent
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

# (name, ds key, column, aggregation, condition, threshold)
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


def alert_report_dataset_keys():
    """The minimal set of dataset keys the alerts + reports attach to.

    A DatasetAlert / DatasetReport requires a dataset FK, so these datasets
    cannot be skipped even in an "alerts only" seed. Currently 8 of the 26.
    """
    return {a[1] for a in ALERTS} | {r[1] for r in REPORTS}

# Realistic triggered values per alert, so the notification feed reads like real
# breaches instead of one repeated number. Condition symbols mirror
# datasets.services._COND_LABELS so a seeded event is byte-identical to one the
# live alert engine would write.
_COND_SYMBOL = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "="}
EVENT_VALUE_POOL = {
    "Off-Spec Rate High — Lab":        [8.3, 9.1, 8.7, 10.2, 9.6, 11.4, 8.9, 12.6],
    "Sulfur Above Release Limit":      [10.4, 11.2, 12.4, 10.9, 13.1, 11.8, 14.2, 10.6],
    "On-Time Turnaround Below Target": [83.1, 79.4, 81.7, 76.2, 84.0, 78.5, 82.3, 74.8],
    "Instrument Calibration Overdue":  [1, 2, 3, 2, 4, 1, 3, 5],
    "Reagent Below Reorder Level":     [3, 5, 4, 6, 2, 7, 4, 8],
    "Low Vision Model Confidence":     [0.79, 0.74, 0.77, 0.71, 0.78, 0.69, 0.76, 0.73],
}


def _alert_message(name, agg, column, cond, threshold, value):
    """Reproduce the exact message datasets.services.evaluate_alerts writes."""
    return (f"Alert '{name}' triggered: {agg}({column}) "
            f"{_COND_SYMBOL.get(cond, cond)} {threshold} "
            f"(current value: {value:.4g})")


def _seed_alert_events(alert_objs, admin, n_events, days, now, log):
    """Create a realistic alert-event (notification) history.

    Events are spread across the last ``days`` days (denser toward now); the most
    recent handful are left UNACKNOWLEDGED so the notification bell shows an
    unread badge, and older ones are acknowledged by the admin. created_at is
    auto_now_add, so it is back-dated with a follow-up UPDATE (which bypasses it).
    """
    meta = {name: (agg, column, cond, thr)
            for name, _key, column, agg, cond, thr in ALERTS}
    names = [n for n in alert_objs]  # preserves ALERTS order
    rng = random.Random(20260708)    # deterministic → idempotent re-runs
    n_unack = min(n_events, max(4, round(n_events * 0.28)))
    latest_ts = {}

    for i in range(n_events):
        # i = 0 is the newest event; spread the rest back over `days`, + jitter.
        frac = i / max(1, n_events - 1)
        minutes_ago = max(6.0, frac * days * 24 * 60 + rng.uniform(-240, 240))
        ts = now - timedelta(minutes=minutes_ago)

        name = names[i % len(names)] if i < len(names) else rng.choice(names)
        agg, column, cond, thr = meta[name]
        value = rng.choice(EVENT_VALUE_POOL[name])
        acked = i >= n_unack

        ev = AlertEvent.objects.create(
            alert=alert_objs[name],
            triggered_value=float(value),
            message=_alert_message(name, agg, column, cond, thr, value),
            acknowledged=acked,
            acknowledged_by=admin if acked else None,
        )
        fields = {"created_at": ts, "updated_at": ts}
        if acked:
            fields["acknowledged_at"] = min(now, ts + timedelta(hours=rng.uniform(1, 20)))
        AlertEvent.objects.filter(pk=ev.pk).update(**fields)
        latest_ts[name] = max(latest_ts.get(name, ts), ts)

    # Reflect the history on each alert's last-checked / last-triggered stamps.
    for name, a in alert_objs.items():
        DatasetAlert.objects.filter(pk=a.pk).update(
            last_checked_at=now, last_triggered_at=latest_ts.get(name),
        )
    unread = min(n_unack, n_events)
    log(f"  {n_events} alert events ({unread} unread → notification badge)")
    return n_events, unread


# ──────────────────────────────────────────────────────────────────────────
def seed_platform(admin=None, *, log=None, rich_events=True, n_events=24,
                  event_days=21, now=None, with_dashboards=True,
                  dataset_keys=None):
    """Seed the Refinery LIMS demo platform. Returns a counts dict.

    ``admin`` — the User to own everything (defaults to the first admin, else the
    first user). ``log`` — a callable taking one string (self.stdout.write /
    print); defaults to silent. Safe to re-run: prior refinery queries, datasets,
    dashboards, alerts, events and reports are cleared first.

    ``dataset_keys`` — if given, only these dataset keys are seeded (e.g.
    ``alert_report_dataset_keys()`` for an alerts/notifications/reports-only
    seed). ``with_dashboards`` — set False to skip the dashboards; dashboards are
    only built when the FULL dataset set is seeded (they reference all keys).
    """
    log = log or (lambda *_a: None)

    if not WAREHOUSE.exists():
        raise RuntimeError(
            f"Warehouse not found at {WAREHOUSE}. Build it first:\n"
            f"  python -m seed_refinery.build_warehouse"
        )

    if admin is None:
        admin = (User.objects.filter(role=User.Role.ADMIN).order_by("id").first()
                 or User.objects.order_by("id").first())
    if admin is None:
        raise RuntimeError(
            "No user to own the demo. Seed a user first "
            "(manage.py seed_lab or manage.py bootstrap)."
        )
    now = now or timezone.now()
    log(f"Owner: {admin.username}")

    # --- Connection -------------------------------------------------------
    ds, _ = DataSource.objects.update_or_create(
        name=DS_NAME,
        defaults=dict(
            description="the refinery LabWare LIMS warehouse — samples, ASTM "
                        "test results, instruments, inventory, vision "
                        "inspections and the document register.",
            source_type="sqlite",
            options={"path": str(WAREHOUSE)},
            owner=admin,
            visibility="shared",
            test_status="ok",
            last_tested_at=now,
            last_test_error="",
        ),
    )
    log(f"Connection: {ds.name} -> {WAREHOUSE}")

    # Idempotency: clear THIS demo's prior objects only. Everything is scoped to
    # the refinery data source or the admin owner so a re-run can never touch a
    # different user's same-named alert/report/dashboard (deleting an alert would
    # cascade-delete that user's whole event history). Demo alerts/reports also
    # hang off the demo datasets, so the Dataset delete already cascades them —
    # the explicit owner-scoped deletes are a defensive backstop.
    Dataset.objects.filter(query__datasource=ds).delete()
    QueryDefinition.objects.filter(datasource=ds).delete()
    Dashboard.objects.filter(name__in=[d[0] for d in DASHBOARDS], owner=admin).delete()
    DatasetAlert.objects.filter(name__in=[a[0] for a in ALERTS], owner=admin).delete()
    DatasetReport.objects.filter(name__in=[r[0] for r in REPORTS], owner=admin).delete()

    # --- Saved queries + datasets ----------------------------------------
    seed_keys = None if dataset_keys is None else set(dataset_keys)
    log("\n=== Queries & Datasets ===")
    datasets = {}
    for key, name, desc, sql in QUERIES:
        if seed_keys is not None and key not in seed_keys:
            continue
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
            query.last_run_at = now
            query.last_row_count = result["row_count"]
            query.save(update_fields=["last_run_at", "last_row_count"])
            log(f"  {name:42s} {result['row_count']:>6,d} rows")
        except Exception as exc:  # noqa: BLE001
            log(f"  {name:42s} ERROR: {exc}")
        datasets[key] = dataset

    # --- Dashboards -------------------------------------------------------
    # Dashboards reference every dataset key, so they are only built when the
    # full dataset set is seeded (skipped for an alerts-only seed).
    log("\n=== Dashboards ===")
    n_dash = 0
    if with_dashboards and seed_keys is None:
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
            log(f"  {name:42s} {len(widgets)} widgets")
            n_dash += 1
    else:
        log("  (skipped)")

    # --- Alerts -----------------------------------------------------------
    log("\n=== Alerts ===")
    alert_objs = {}
    for name, key, column, agg, cond, threshold in ALERTS:
        if key not in datasets:  # its backing dataset wasn't seeded
            log(f"  (skipped '{name}': dataset '{key}' not seeded)")
            continue
        alert_objs[name] = DatasetAlert.objects.create(
            name=name, dataset=datasets[key], column=column, aggregation=agg,
            condition=cond, threshold=threshold, is_active=True,
            notify_email="lab.supervisor@refinery-lab.com", owner=admin,
        )
    log(f"  {len(alert_objs)} alerts")

    # --- Alert events (notifications) ------------------------------------
    log("\n=== Notifications (alert events) ===")
    if rich_events and n_events > 0:
        n_ev, unread = _seed_alert_events(
            alert_objs, admin, n_events, event_days, now, log)
    else:
        n_ev = unread = 0
        log("  (skipped)")

    # --- Reports ----------------------------------------------------------
    log("\n=== Reports ===")
    n_reports = 0
    for name, key, emails, schedule in REPORTS:
        if key not in datasets:
            continue
        DatasetReport.objects.create(
            name=name, dataset=datasets[key], recipient_emails=emails,
            schedule=schedule, is_active=True, owner=admin,
        )
        n_reports += 1
    log(f"  {n_reports} reports")

    return {
        "connection": ds.name,
        "datasets": len(datasets),
        "dashboards": n_dash,
        "alerts": len(alert_objs),
        "events": n_ev,
        "unread": unread,
        "reports": n_reports,
    }
