"""
Seed the DIS platform with the REAL EGPC LabWare LIMS (live SQL Server EGPC_DEV).

Registers EGPC_DEV as a DIS data source, then creates saved queries + cached
datasets + dashboards covering the agreed use cases:

    Spec compliance & SPC · Out-of-spec root cause · Operations forecasting
    · Sulphur analysis (featured) · Analysis-type volumes · Instruments/calibration

Datasets power the Advanced Analytics dashboards, Ask Data and the AI agent.
Additive — does not touch the Refinery / Water demos.

Run (with EGPC_DEV reachable):
    .venv/bin/python seed_egpc_demo.py
"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from decouple import config  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import User  # noqa: E402
from core.encryption import encrypt  # noqa: E402
from connections.models import DataSource  # noqa: E402
from dashboards.models import Dashboard, Widget  # noqa: E402
from datasets.models import Dataset, DatasetAlert  # noqa: E402
from datasets.services import refresh_dataset  # noqa: E402
from querybuilder.models import QueryDefinition  # noqa: E402

DS_NAME = "EGPC LIMS"

# Total-sulfur analyses discovered in EGPC RESULT (US spelling).
SULFUR_FILTER = "(r.ANALYSIS LIKE 'SULFUR%' OR r.NAME LIKE '%Sulfur%')"
STATUS_CASE = (
    "CASE s.STATUS WHEN 'A' THEN 'Authorized' WHEN 'C' THEN 'Complete' "
    "WHEN 'X' THEN 'Cancelled' WHEN 'R' THEN 'Rejected' WHEN 'U' THEN 'Unreceived' "
    "WHEN 'I' THEN 'In Progress' WHEN 'P' THEN 'Pending' WHEN 'D' THEN 'Disposed' "
    "ELSE s.STATUS END"
)

# ──────────────────────────────────────────────────────────────────────────
# Saved queries (key, name, description, T-SQL) — SQL Server dialect.
# ──────────────────────────────────────────────────────────────────────────
QUERIES = [
    # ---- Operations / forecasting ---------------------------------------
    ("egpc_sample_register", "EGPC Sample Register (live)",
     "Row-level sample login register — product, status, in-spec and turnaround. "
     "Powers Data Explorer / Ask Data / the agent.",
     """SELECT TOP 2000 s.TEXT_ID, s.LOGIN_DATE, s.PRODUCT,
       """ + STATUS_CASE + """ AS STATUS,
       s.IN_SPEC,
       DATEDIFF(hour, s.LOGIN_DATE, s.DATE_COMPLETED) AS TAT_HOURS
FROM dbo.SAMPLE s
ORDER BY s.LOGIN_DATE DESC"""),

    ("egpc_monthly_volume", "EGPC Monthly Sample Volume",
     "Samples logged per month across the full 3-year history (forecasting input).",
     """SELECT CONVERT(char(7), s.LOGIN_DATE, 126) AS month,
       COUNT(*) AS samples,
       AVG(CAST(DATEDIFF(hour, s.LOGIN_DATE, s.DATE_COMPLETED) AS float)) AS avg_tat_hours
FROM dbo.SAMPLE s
WHERE s.LOGIN_DATE IS NOT NULL
GROUP BY CONVERT(char(7), s.LOGIN_DATE, 126)
ORDER BY month"""),

    ("egpc_samples_by_product", "EGPC Samples by Product",
     "Sample volume by product stream (top 25).",
     """SELECT TOP 25 s.PRODUCT AS product, COUNT(*) AS samples,
       SUM(CASE WHEN s.IN_SPEC='T' THEN 1 ELSE 0 END) AS in_spec
FROM dbo.SAMPLE s
WHERE s.PRODUCT IS NOT NULL
GROUP BY s.PRODUCT
ORDER BY samples DESC"""),

    ("egpc_status_breakdown", "EGPC Sample Status Breakdown",
     "How many samples sit in each workflow status.",
     """SELECT """ + STATUS_CASE + """ AS status, COUNT(*) AS samples
FROM dbo.SAMPLE s
GROUP BY """ + STATUS_CASE + """
ORDER BY samples DESC"""),

    ("egpc_tat_by_product", "EGPC Turnaround by Product",
     "Average turnaround time (hours) by product, for completed samples.",
     """SELECT TOP 20 s.PRODUCT AS product, COUNT(*) AS completed_samples,
       AVG(CAST(DATEDIFF(hour, s.LOGIN_DATE, s.DATE_COMPLETED) AS float)) AS avg_tat_hours
FROM dbo.SAMPLE s
WHERE s.DATE_COMPLETED IS NOT NULL AND s.PRODUCT IS NOT NULL
  AND DATEDIFF(hour, s.LOGIN_DATE, s.DATE_COMPLETED) BETWEEN 0 AND 8760
GROUP BY s.PRODUCT
HAVING COUNT(*) >= 20
ORDER BY avg_tat_hours DESC"""),

    # ---- Results / spec compliance / SPC --------------------------------
    ("egpc_results", "EGPC Results (row-level)",
     "A representative full-population sample (~every 49th result) of numeric "
     "results joined to their sample — analyte, value, units, in-spec, operator. "
     "The core analytics dataset; systematic sampling (not 'most recent') so "
     "anomaly/root-cause reflect the WHOLE dataset, not a time slice.",
     """SELECT r.SAMPLE_NUMBER, s.TEXT_ID, s.PRODUCT,
       r.ANALYSIS, r.NAME AS component,
       TRY_CONVERT(float, r.NUMERIC_ENTRY) AS value, r.UNITS,
       r.IN_SPEC, r.ENTERED_BY, r.ENTERED_ON
FROM dbo.RESULT r JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER = s.SAMPLE_NUMBER
WHERE r.NUMERIC_ENTRY IS NOT NULL AND r.RESULT_NUMBER % 49 = 0
ORDER BY r.RESULT_NUMBER"""),

    ("egpc_spec_by_product", "EGPC Spec Compliance by Product",
     "In-spec rate of individual results per product stream (worst first).",
     """SELECT TOP 25 s.PRODUCT AS product, COUNT(*) AS results,
       SUM(CASE WHEN r.IN_SPEC='T' THEN 1 ELSE 0 END) AS in_spec,
       ROUND(100.0*SUM(CASE WHEN r.IN_SPEC='T' THEN 1 ELSE 0 END)/COUNT(*),1) AS in_spec_pct
FROM dbo.RESULT r JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER = s.SAMPLE_NUMBER
WHERE s.PRODUCT IS NOT NULL
GROUP BY s.PRODUCT
HAVING COUNT(*) >= 50
ORDER BY in_spec_pct ASC"""),

    ("egpc_test_by_analysis", "EGPC Test Volume by Analysis",
     "The 25 highest-volume analyses (test methods) with their in-spec rate.",
     """SELECT TOP 25 r.ANALYSIS AS analysis, COUNT(*) AS results,
       ROUND(100.0*SUM(CASE WHEN r.IN_SPEC='T' THEN 1 ELSE 0 END)/COUNT(*),1) AS in_spec_pct
FROM dbo.RESULT r
GROUP BY r.ANALYSIS
ORDER BY results DESC"""),

    ("egpc_monthly_offspec", "EGPC Monthly Off-Spec Trend",
     "Monthly result volume and off-spec %% (by result entry date).",
     """SELECT CONVERT(char(7), r.ENTERED_ON, 126) AS month,
       COUNT(*) AS results,
       SUM(CASE WHEN r.IN_SPEC='F' THEN 1 ELSE 0 END) AS off_spec,
       ROUND(100.0*SUM(CASE WHEN r.IN_SPEC='F' THEN 1 ELSE 0 END)/COUNT(*),2) AS off_spec_pct
FROM dbo.RESULT r
WHERE r.ENTERED_ON IS NOT NULL
GROUP BY CONVERT(char(7), r.ENTERED_ON, 126)
ORDER BY month"""),

    # ---- Out-of-spec root cause -----------------------------------------
    ("egpc_offspec_results", "EGPC Off-Spec Results",
     "Individual results that breached their spec limit — analyte, product, "
     "operator, value. Root-cause analysis input.",
     """SELECT TOP 3000 s.TEXT_ID, s.PRODUCT, r.ANALYSIS, r.NAME AS component,
       TRY_CONVERT(float, r.NUMERIC_ENTRY) AS value, r.UNITS,
       r.ENTERED_BY, r.ENTERED_ON
FROM dbo.RESULT r JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER = s.SAMPLE_NUMBER
WHERE r.IN_SPEC = 'F'
ORDER BY r.ENTERED_ON DESC"""),

    ("egpc_offspec_by_operator", "EGPC Off-Spec by Operator",
     "Off-spec count and rate by the analyst who entered the result.",
     """SELECT r.ENTERED_BY AS operator, COUNT(*) AS results,
       SUM(CASE WHEN r.IN_SPEC='F' THEN 1 ELSE 0 END) AS off_spec,
       ROUND(100.0*SUM(CASE WHEN r.IN_SPEC='F' THEN 1 ELSE 0 END)/COUNT(*),2) AS off_spec_pct
FROM dbo.RESULT r
WHERE r.ENTERED_BY IS NOT NULL
GROUP BY r.ENTERED_BY
HAVING COUNT(*) >= 100
ORDER BY off_spec_pct DESC"""),

    # ---- Sulphur analysis (featured) ------------------------------------
    ("egpc_sulphur_results", "EGPC Sulphur Results",
     "Total-sulfur results across products — value, units, in-spec, operator. "
     "Featured analyte for SPC, anomaly detection and capability.",
     """SELECT TOP 5000 s.TEXT_ID, s.PRODUCT, r.ANALYSIS, r.NAME AS component,
       TRY_CONVERT(float, r.NUMERIC_ENTRY) AS sulphur_value, r.UNITS,
       r.IN_SPEC, r.ENTERED_BY, r.ENTERED_ON
FROM dbo.RESULT r JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER = s.SAMPLE_NUMBER
WHERE """ + SULFUR_FILTER + """ AND r.NUMERIC_ENTRY IS NOT NULL
ORDER BY r.ENTERED_ON DESC"""),

    ("egpc_sulphur_by_product", "EGPC Sulphur by Product",
     "Average / max total-sulfur by product (release-critical metric).",
     """SELECT TOP 20 s.PRODUCT AS product, COUNT(*) AS results,
       ROUND(AVG(TRY_CONVERT(float, r.NUMERIC_ENTRY)),4) AS avg_sulphur,
       ROUND(MAX(TRY_CONVERT(float, r.NUMERIC_ENTRY)),4) AS max_sulphur,
       MAX(r.UNITS) AS units
FROM dbo.RESULT r JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER = s.SAMPLE_NUMBER
WHERE """ + SULFUR_FILTER + """ AND r.NUMERIC_ENTRY IS NOT NULL AND s.PRODUCT IS NOT NULL
GROUP BY s.PRODUCT
ORDER BY avg_sulphur DESC"""),

    # ---- Instruments / calibration --------------------------------------
    ("egpc_instruments", "EGPC Instruments & Calibration",
     "Instrument register with calibration / PM dates and overdue flag.",
     """SELECT TOP 200 NAME AS instrument, LOCATION, VENDOR, SERIAL_NO,
       CALIB_DATE, CALIB_EXPIRATION, PM_DATE, PM_EXPIRATION,
       CASE WHEN CALIB_EXPIRATION < GETDATE() THEN 'OVERDUE' ELSE 'OK' END AS calibration_status
FROM dbo.INSTRUMENTS
WHERE REMOVED IS NULL OR REMOVED <> 'T'
ORDER BY CALIB_EXPIRATION ASC"""),
]

DASHBOARDS = [
    ("EGPC — Lab Operations",
     "Sample throughput, turnaround and status across the EGPC laboratory "
     "(live LabWare LIMS).",
     [
        dict(title="Total Samples", kind="kpi", ds="egpc_samples_by_product",
             config={"value_field": "samples", "rollup": "sum"}, x=0, y=0, w=3, h=3),
        dict(title="Products", kind="kpi", ds="egpc_samples_by_product",
             config={"value_field": "product", "rollup": "count"}, x=3, y=0, w=3, h=3),
        dict(title="Avg Turnaround (hrs)", kind="kpi", ds="egpc_tat_by_product",
             config={"value_field": "avg_tat_hours", "rollup": "avg"}, x=6, y=0, w=3, h=3),
        dict(title="Monthly Sample Volume", kind="line", ds="egpc_monthly_volume",
             config={"category_field": "month", "value_field": "samples", "smooth": True},
             x=0, y=3, w=8, h=6),
        dict(title="Sample Status", kind="pie", ds="egpc_status_breakdown",
             config={"category_field": "status", "value_field": "samples"}, x=8, y=3, w=4, h=6),
        dict(title="Samples by Product", kind="bar", ds="egpc_samples_by_product",
             config={"category_field": "product", "value_field": "samples"}, x=0, y=9, w=7, h=6),
        dict(title="Turnaround by Product", kind="bar", ds="egpc_tat_by_product",
             config={"category_field": "product", "value_field": "avg_tat_hours"}, x=7, y=9, w=5, h=6),
        dict(title="Sample Register", kind="table", ds="egpc_sample_register",
             config={}, x=0, y=15, w=12, h=6),
     ]),

    ("EGPC — Spec Compliance & Sulphur",
     "Result spec-compliance, off-spec trend and the sulphur release metric.",
     [
        dict(title="Results (sampled)", kind="kpi", ds="egpc_test_by_analysis",
             config={"value_field": "results", "rollup": "sum"}, x=0, y=0, w=3, h=3),
        dict(title="Avg Sulphur", kind="kpi", ds="egpc_sulphur_by_product",
             config={"value_field": "avg_sulphur", "rollup": "avg"}, x=3, y=0, w=3, h=3),
        dict(title="Off-Spec %", kind="kpi", ds="egpc_monthly_offspec",
             config={"value_field": "off_spec_pct", "rollup": "avg"}, x=6, y=0, w=3, h=3),
        dict(title="Spec Compliance by Product", kind="bar", ds="egpc_spec_by_product",
             config={"category_field": "product", "value_field": "in_spec_pct"}, x=0, y=3, w=7, h=6),
        dict(title="Test Volume by Analysis", kind="bar", ds="egpc_test_by_analysis",
             config={"category_field": "analysis", "value_field": "results"}, x=7, y=3, w=5, h=6),
        dict(title="Monthly Off-Spec Trend", kind="line", ds="egpc_monthly_offspec",
             config={"category_field": "month", "value_field": "off_spec_pct", "smooth": True},
             x=0, y=9, w=6, h=6),
        dict(title="Sulphur by Product", kind="bar", ds="egpc_sulphur_by_product",
             config={"category_field": "product", "value_field": "avg_sulphur"}, x=6, y=9, w=6, h=6),
        dict(title="Sulphur Results", kind="table", ds="egpc_sulphur_results",
             config={}, x=0, y=15, w=12, h=6),
     ]),

    ("EGPC — Out-of-Spec Root Cause",
     "Off-spec results by operator, product and analyte — the root-cause view.",
     [
        dict(title="Off-Spec by Operator", kind="bar", ds="egpc_offspec_by_operator",
             config={"category_field": "operator", "value_field": "off_spec_pct"}, x=0, y=0, w=7, h=6),
        dict(title="Off-Spec Results", kind="table", ds="egpc_offspec_results",
             config={}, x=0, y=6, w=12, h=8),
     ]),

    ("EGPC — Instruments & Calibration",
     "Instrument register with calibration and preventive-maintenance position.",
     [
        dict(title="Instruments", kind="kpi", ds="egpc_instruments",
             config={"value_field": "instrument", "rollup": "count"}, x=0, y=0, w=3, h=3),
        dict(title="Instruments & Calibration", kind="table", ds="egpc_instruments",
             config={}, x=0, y=3, w=12, h=8),
     ]),
]

ALERTS = [
    ("EGPC Off-Spec Rate High", "egpc_monthly_offspec", "off_spec_pct", "max", "gt", 5.0),
    ("EGPC Operator Off-Spec High", "egpc_offspec_by_operator", "off_spec_pct", "max", "gt", 10.0),
]


def main():
    admin = (User.objects.filter(role=User.Role.ADMIN).order_by("id").first()
             or User.objects.order_by("id").first())
    if not admin:
        raise SystemExit("No users — run seed_lab first.")
    print(f"Owner: {admin.username}")

    ds, _ = DataSource.objects.update_or_create(
        name=DS_NAME,
        defaults=dict(
            description="The live EGPC LabWare LIMS (SQL Server EGPC_DEV) — samples, "
                        "tests, results, specs, instruments. Primary DIS data source.",
            source_type="mssql",
            host=config("DB_HOST", default="127.0.0.1"),
            port=config("DB_PORT", default=1433, cast=int),
            database_name=config("DB_NAME", default="EGPC_DEV"),
            username=config("DB_USER", default="SA"),
            password_encrypted=encrypt(config("DB_PASSWORD", default="")),
            options={"driver": config("DB_DRIVER", default="ODBC Driver 18 for SQL Server"),
                     "extra_params": "TrustServerCertificate=yes;Encrypt=no"},
            owner=admin, visibility="shared",
            test_status="ok", last_tested_at=timezone.now(), last_test_error="",
        ),
    )
    print(f"Connection: {ds.name} -> {ds.host}:{ds.port}/{ds.database_name}")

    # Idempotency
    Dataset.objects.filter(query__datasource=ds).delete()
    QueryDefinition.objects.filter(datasource=ds).delete()
    Dashboard.objects.filter(name__in=[d[0] for d in DASHBOARDS], owner=admin).delete()
    DatasetAlert.objects.filter(name__in=[a[0] for a in ALERTS]).delete()

    print("\n=== Queries & Datasets ===")
    datasets = {}
    for key, name, desc, sql in QUERIES:
        query = QueryDefinition.objects.create(
            name=name, description=desc, datasource=ds,
            mode=QueryDefinition.Mode.RAW, raw_sql=sql, generated_sql=sql,
            owner=admin, visibility="shared",
        )
        dataset = Dataset.objects.create(
            name=name, description=desc, query=query, owner=admin,
            visibility="shared", refresh_interval=Dataset.RefreshInterval.DAILY,
        )
        try:
            result = refresh_dataset(dataset)
            query.last_run_at = timezone.now()
            query.last_row_count = result["row_count"]
            query.save(update_fields=["last_run_at", "last_row_count"])
            print(f"  {name:38s} {result['row_count']:>6,d} rows")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:38s} ERROR: {str(exc)[:80]}")
        datasets[key] = dataset

    print("\n=== Dashboards ===")
    for name, desc, widgets in DASHBOARDS:
        dash = Dashboard.objects.create(name=name, description=desc, owner=admin,
                                        visibility="shared")
        for order, w in enumerate(widgets):
            Widget.objects.create(
                dashboard=dash, title=w["title"], widget_type=w["kind"],
                dataset=datasets[w["ds"]], config=w["config"],
                x=w["x"], y=w["y"], w=w["w"], h=w["h"], order=order,
            )
        print(f"  {name:38s} {len(widgets)} widgets")

    print("\n=== Alerts ===")
    for name, key, col, agg, cond, thr in ALERTS:
        DatasetAlert.objects.create(
            name=name, dataset=datasets[key], column=col, aggregation=agg,
            condition=cond, threshold=thr, is_active=True, owner=admin,
        )
    print(f"  {len(ALERTS)} alerts")

    print(f"\n✓ EGPC LIMS seeded: {len(QUERIES)} datasets, {len(DASHBOARDS)} dashboards.")


if __name__ == "__main__":
    main()
