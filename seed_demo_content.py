"""
Seed demo queries, datasets, dashboards, alerts and reports
for the Water Quality DW data source.

Run: .venv/bin/python seed_demo_content.py
"""
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone

from connections.models import DataSource
from querybuilder.models import QueryDefinition
from datasets.models import Dataset, DatasetAlert, AlertEvent, DatasetReport
from datasets.services import refresh_dataset
from dashboards.models import Dashboard, Widget

User = get_user_model()

# ── helpers ──────────────────────────────────────────────────────────────────

def get_admin():
    return User.objects.filter(role="admin").first() or User.objects.filter(is_superuser=True).first() or User.objects.first()

def get_ds(name="Water Quality DW"):
    return DataSource.objects.filter(name__icontains="Water Quality").first()

def make_query(admin, ds, name, sql, description=""):
    q, _ = QueryDefinition.objects.update_or_create(
        name=name,
        defaults=dict(
            owner=admin, datasource=ds, mode="raw",
            raw_sql=sql, description=description, visibility="shared",
        ),
    )
    return q

def make_dataset(admin, query, name, description=""):
    ds, _ = Dataset.objects.update_or_create(
        name=name,
        defaults=dict(
            owner=admin, query=query, description=description,
            visibility="shared", refresh_interval="daily",
        ),
    )
    print(f"  Refreshing dataset: {name} …", end=" ", flush=True)
    try:
        r = refresh_dataset(ds)
        print(f"{r['row_count']} rows")
    except Exception as e:
        print(f"ERROR: {e}")
    return ds

# ── 1. QUERIES ────────────────────────────────────────────────────────────────

print("\n=== Queries ===")
admin = get_admin()
wq = get_ds()

if not wq:
    print("ERROR: 'Water Quality DW' data source not found. Run the app first and fix the connection.")
    raise SystemExit(1)

print(f"Using datasource: {wq.name} (id={wq.id})")

q_potability = make_query(admin, wq, "Potability Summary by Lab",
    """
SELECT
    lab,
    COUNT(*) AS total_samples,
    SUM(CASE WHEN potable = 1 THEN 1 ELSE 0 END) AS potable_count,
    SUM(CASE WHEN potable = 0 THEN 1 ELSE 0 END) AS not_potable_count,
    ROUND(100.0 * SUM(CASE WHEN potable = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) AS potability_pct,
    ROUND(AVG(quality_score), 1) AS avg_quality_score
FROM water_samples
GROUP BY lab
ORDER BY total_samples DESC
    """.strip(),
    "Potability rate and average quality score per laboratory.",
)

q_monthly = make_query(admin, wq, "Monthly Sample Volume Trend",
    """
SELECT
    collected_month,
    COUNT(*) AS samples,
    SUM(CASE WHEN potable = 1 THEN 1 ELSE 0 END) AS potable,
    SUM(CASE WHEN potable = 0 THEN 1 ELSE 0 END) AS not_potable,
    ROUND(AVG(quality_score), 1) AS avg_quality
FROM water_samples
WHERE collected_month >= '2024-01'
GROUP BY collected_month
ORDER BY collected_month
    """.strip(),
    "Monthly sample counts and average quality score since Jan 2024.",
)

q_region = make_query(admin, wq, "Quality by Region",
    """
SELECT
    region,
    COUNT(*) AS samples,
    ROUND(AVG(ph), 2) AS avg_ph,
    ROUND(AVG(turbidity), 2) AS avg_turbidity,
    ROUND(AVG(quality_score), 1) AS avg_quality_score,
    ROUND(100.0 * SUM(CASE WHEN potable = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) AS potability_pct
FROM water_samples
GROUP BY region
ORDER BY avg_quality_score DESC
    """.strip(),
    "Average water quality metrics broken down by geographic region.",
)

q_source = make_query(admin, wq, "Sample Volume by Source Type",
    """
SELECT
    source_type,
    COUNT(*) AS total,
    SUM(CASE WHEN status = 'Approved' THEN 1 ELSE 0 END) AS approved,
    SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected,
    ROUND(AVG(quality_score), 1) AS avg_quality
FROM water_samples
GROUP BY source_type
ORDER BY total DESC
    """.strip(),
    "Breakdown of sample volumes and approval rates by water source type.",
)

q_lab_perf = make_query(admin, wq, "Lab Performance Overview",
    """
SELECT
    w.lab,
    l.region,
    l.manager,
    COUNT(*) AS total_samples,
    ROUND(AVG(w.quality_score), 1) AS avg_quality,
    ROUND(AVG(w.ph), 2) AS avg_ph,
    ROUND(AVG(w.turbidity), 3) AS avg_turbidity,
    ROUND(AVG(w.conductivity), 1) AS avg_conductivity,
    ROUND(100.0 * SUM(CASE WHEN w.potable = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) AS potability_pct,
    SUM(CASE WHEN w.status = 'Rejected' THEN 1 ELSE 0 END) AS rejected_count
FROM water_samples w
JOIN labs l ON l.id = w.lab_id
GROUP BY w.lab, l.region, l.manager
ORDER BY avg_quality DESC
    """.strip(),
    "Full performance scorecard per lab including manager and region details.",
)

q_ph_scatter = make_query(admin, wq, "pH vs Conductivity Scatter",
    """
SELECT
    ph,
    conductivity,
    turbidity,
    quality_score,
    potability,
    lab,
    region
FROM water_samples
WHERE ph BETWEEN 3 AND 11
ORDER BY RANDOM()
LIMIT 500
    """.strip(),
    "Random 500-row sample for pH vs conductivity scatter analysis.",
)

q_rejected = make_query(admin, wq, "Rejected Samples Detail",
    """
SELECT
    sample_code,
    lab,
    site,
    region,
    collected_on,
    analyst,
    ROUND(ph, 2) AS ph,
    ROUND(quality_score, 1) AS quality_score,
    status
FROM water_samples
WHERE status = 'Rejected'
ORDER BY collected_on DESC
LIMIT 200
    """.strip(),
    "Last 200 rejected samples with analyst and site details.",
)

print(f"  Created {QueryDefinition.objects.count()} queries total")

# ── 2. DATASETS ───────────────────────────────────────────────────────────────

print("\n=== Datasets ===")

ds_potability = make_dataset(admin, q_potability, "Potability by Lab",
    "Potability rates and quality scores aggregated per laboratory.")

ds_monthly = make_dataset(admin, q_monthly, "Monthly Sample Trend",
    "Month-by-month sample volumes and quality trend since Jan 2024.")

ds_region = make_dataset(admin, q_region, "Regional Water Quality",
    "Average quality metrics by geographic region.")

ds_source = make_dataset(admin, q_source, "Source Type Analysis",
    "Volume and approval rates by water source type.")

ds_lab_perf = make_dataset(admin, q_lab_perf, "Lab Performance Scorecard",
    "Full per-lab performance metrics joined with lab master data.")

ds_rejected = make_dataset(admin, q_rejected, "Recent Rejected Samples",
    "Latest 200 rejected samples for quality review.")

print(f"  Created {Dataset.objects.count()} datasets total")

# ── 3. ALERTS ─────────────────────────────────────────────────────────────────

print("\n=== Alerts ===")

def make_alert(admin, dataset, name, column, agg, condition, threshold, email="admin@dse.local"):
    a, _ = DatasetAlert.objects.update_or_create(
        name=name,
        defaults=dict(
            owner=admin, dataset=dataset,
            column=column, aggregation=agg,
            condition=condition, threshold=threshold,
            is_active=True, notify_email=email,
        ),
    )
    return a

al1 = make_alert(admin, ds_potability, "Low Potability Rate — Any Lab",
    "potability_pct", "min", "lt", 30.0)

al2 = make_alert(admin, ds_monthly, "Monthly Sample Volume Drop",
    "samples", "last", "lt", 100.0)

al3 = make_alert(admin, ds_lab_perf, "Lab Quality Score Below Threshold",
    "avg_quality", "min", "lt", 55.0)

al4 = make_alert(admin, ds_region, "High Average Turbidity — Region",
    "avg_turbidity", "max", "gt", 3.5)

# Seed one past alert event for al1 and al3 so the Events tab isn't empty
from datetime import timedelta
now = timezone.now()

AlertEvent.objects.get_or_create(
    alert=al1, triggered_value=22.7,
    defaults=dict(
        message="Alert 'Low Potability Rate — Any Lab' triggered: min(potability_pct) < 30.0 (current value: 22.7)",
        acknowledged=False,
    ),
)
AlertEvent.objects.get_or_create(
    alert=al3, triggered_value=51.3,
    defaults=dict(
        message="Alert 'Lab Quality Score Below Threshold' triggered: min(avg_quality) < 55.0 (current value: 51.3)",
        acknowledged=False,
    ),
)
AlertEvent.objects.get_or_create(
    alert=al3, triggered_value=53.1,
    defaults=dict(
        message="Alert 'Lab Quality Score Below Threshold' triggered: min(avg_quality) < 55.0 (current value: 53.1)",
        acknowledged=True,
    ),
)

print(f"  Created {DatasetAlert.objects.count()} alerts, {AlertEvent.objects.count()} events")

# ── 4. REPORTS ────────────────────────────────────────────────────────────────

print("\n=== Reports ===")

def make_report(dataset, name, emails, schedule):
    r, _ = DatasetReport.objects.update_or_create(
        name=name,
        defaults=dict(
            dataset=dataset, recipient_emails=emails,
            schedule=schedule, is_active=True,
        ),
    )
    return r

make_report(ds_monthly, "Daily Quality Digest",
    "admin@dse.local,lab-managers@dse.local", "daily")
make_report(ds_lab_perf, "Weekly Lab Performance Report",
    "admin@dse.local,quality@dse.local", "weekly")
make_report(ds_rejected, "Monthly Rejected Samples Summary",
    "admin@dse.local", "monthly")

print(f"  Created {DatasetReport.objects.count()} reports")

# ── 5. DASHBOARDS ─────────────────────────────────────────────────────────────

print("\n=== Dashboards ===")

# Remove stale widgets to avoid duplicates on re-run
Dashboard.objects.filter(name__in=[
    "Water Quality Operations",
    "Lab Performance",
]).delete()

# --- Dashboard 1: Operations ---
d1 = Dashboard.objects.create(
    name="Water Quality Operations",
    description="Live overview of potability, sample volumes and quality trends.",
    owner=admin, visibility="shared",
)

widgets_d1 = [
    # Row 1 — KPI cards
    dict(title="Total Samples", widget_type="kpi", dataset=ds_monthly,
         config={"value_field": "samples", "rollup": "sum"},
         x=0, y=0, w=3, h=3),
    dict(title="Avg Quality Score", widget_type="kpi", dataset=ds_region,
         config={"value_field": "avg_quality_score", "rollup": "avg"},
         x=3, y=0, w=3, h=3),
    dict(title="Potability Rate (%)", widget_type="kpi", dataset=ds_potability,
         config={"value_field": "potability_pct", "rollup": "avg"},
         x=6, y=0, w=3, h=3),
    dict(title="Rejected Samples", widget_type="kpi", dataset=ds_rejected,
         config={"value_field": "quality_score", "rollup": "count"},
         x=9, y=0, w=3, h=3),

    # Row 2 — Monthly trend line
    dict(title="Monthly Sample Volume Trend", widget_type="line", dataset=ds_monthly,
         config={"category_field": "collected_month",
                 "value_field": "samples",
                 "series_field": "potability",
                 "smooth": True},
         x=0, y=3, w=8, h=6),

    # Row 2 — Source type pie
    dict(title="Samples by Source Type", widget_type="pie", dataset=ds_source,
         config={"category_field": "source_type", "value_field": "total"},
         x=8, y=3, w=4, h=6),

    # Row 3 — Potability bar by lab
    dict(title="Potability Rate by Lab (%)", widget_type="bar", dataset=ds_potability,
         config={"category_field": "lab",
                 "value_field": "potability_pct"},
         x=0, y=9, w=6, h=6),

    # Row 3 — Regional quality bar
    dict(title="Average Quality Score by Region", widget_type="bar", dataset=ds_region,
         config={"category_field": "region",
                 "value_field": "avg_quality_score"},
         x=6, y=9, w=6, h=6),

    # Row 4 — Approval status bar
    dict(title="Approved vs Rejected by Source", widget_type="bar", dataset=ds_source,
         config={"category_field": "source_type",
                 "value_field": "approved",
                 "series_field": "rejected"},
         x=0, y=15, w=7, h=6),

    # Row 4 — Recent rejected table
    dict(title="Recent Rejected Samples", widget_type="table", dataset=ds_rejected,
         config={},
         x=7, y=15, w=5, h=6),
]

for i, w in enumerate(widgets_d1):
    Widget.objects.create(dashboard=d1, order=i, **w)
print(f"  Dashboard '{d1.name}': {len(widgets_d1)} widgets")

# --- Dashboard 2: Lab Performance ---
d2 = Dashboard.objects.create(
    name="Lab Performance",
    description="Per-lab scorecards, rankings and parameter comparisons.",
    owner=admin, visibility="shared",
)

widgets_d2 = [
    # KPI row
    dict(title="Labs Monitored", widget_type="kpi", dataset=ds_lab_perf,
         config={"value_field": "total_samples", "rollup": "count"},
         x=0, y=0, w=3, h=3),
    dict(title="Best Avg Quality", widget_type="kpi", dataset=ds_lab_perf,
         config={"value_field": "avg_quality", "rollup": "max"},
         x=3, y=0, w=3, h=3),
    dict(title="Total Rejected", widget_type="kpi", dataset=ds_lab_perf,
         config={"value_field": "rejected_count", "rollup": "sum"},
         x=6, y=0, w=3, h=3),
    dict(title="Avg Potability %", widget_type="kpi", dataset=ds_lab_perf,
         config={"value_field": "potability_pct", "rollup": "avg"},
         x=9, y=0, w=3, h=3),

    # Bar — samples per lab
    dict(title="Total Samples per Lab", widget_type="bar", dataset=ds_lab_perf,
         config={"category_field": "lab", "value_field": "total_samples"},
         x=0, y=3, w=6, h=6),

    # Bar — quality per lab
    dict(title="Average Quality Score per Lab", widget_type="bar", dataset=ds_lab_perf,
         config={"category_field": "lab", "value_field": "avg_quality"},
         x=6, y=3, w=6, h=6),

    # Bar — rejection count
    dict(title="Rejected Samples per Lab", widget_type="bar", dataset=ds_lab_perf,
         config={"category_field": "lab", "value_field": "rejected_count"},
         x=0, y=9, w=5, h=6),

    # Pie — potability by region
    dict(title="Potability Rate by Region", widget_type="pie", dataset=ds_region,
         config={"category_field": "region", "value_field": "potability_pct"},
         x=5, y=9, w=4, h=6),

    # Area — monthly potable vs not
    dict(title="Monthly Potable vs Not Potable", widget_type="area", dataset=ds_monthly,
         config={"category_field": "collected_month",
                 "value_field": "potable"},
         x=9, y=9, w=3, h=6),

    # Full-width table
    dict(title="Lab Scorecard Table", widget_type="table", dataset=ds_lab_perf,
         config={},
         x=0, y=15, w=12, h=7),
]

for i, w in enumerate(widgets_d2):
    Widget.objects.create(dashboard=d2, order=i, **w)
print(f"  Dashboard '{d2.name}': {len(widgets_d2)} widgets")

# ── SUMMARY ───────────────────────────────────────────────────────────────────

print("\n✓ Seed complete")
print(f"  Queries:    {QueryDefinition.objects.count()}")
print(f"  Datasets:   {Dataset.objects.count()}")
print(f"  Dashboards: {Dashboard.objects.count()}")
print(f"  Widgets:    {Widget.objects.count()}")
print(f"  Alerts:     {DatasetAlert.objects.count()}")
print(f"  Events:     {AlertEvent.objects.count()}")
print(f"  Reports:    {DatasetReport.objects.count()}")
