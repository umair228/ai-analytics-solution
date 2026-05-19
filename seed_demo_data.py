"""Seed the DSE platform with a realistic water-quality LIMS demo.

Builds a SQLite "data warehouse" from the water_potability dataset, enriched
with lab / site / analyst / date metadata, then registers it as a connection
and creates saved queries, cached datasets, and a full dashboard.

Run:  .venv/bin/python seed_demo_data.py
"""
import calendar
import os
import random
import sqlite3
from datetime import date
from pathlib import Path

import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.utils import timezone  # noqa: E402

from accounts.models import User  # noqa: E402
from connections.models import DataSource  # noqa: E402
from dashboards.models import Dashboard, Widget  # noqa: E402
from datasets.models import Dataset  # noqa: E402
from datasets.services import refresh_dataset  # noqa: E402
from querybuilder.models import QueryDefinition  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = Path("/Users/umair/Labware/OMICS/water_potability 1.csv")
WAREHOUSE = BASE_DIR / "media" / "lab_warehouse.sqlite3"

RNG = random.Random(20260519)

LABS = [
    ("LAB-RUH", "Riyadh Central Water Lab", "Central", "Khalid Al-Otaibi", 2009),
    ("LAB-JED", "Jeddah Coastal Analytics", "Western", "Sara Mansour", 2012),
    ("LAB-DMM", "Dammam Industrial Lab", "Eastern", "Faisal Al-Harbi", 2007),
    ("LAB-MAK", "Makkah Quality Unit", "Western", "Huda Al-Ghamdi", 2015),
    ("LAB-TBK", "Tabuk Regional Lab", "Northern", "Yousef Nasser", 2018),
    ("LAB-ABH", "Abha Highlands Lab", "Southern", "Layla Al-Qahtani", 2016),
]
SITES = {
    "Central": "Riyadh Treatment Plant",
    "Western": "Jeddah Desalination Complex",
    "Eastern": "Dammam Network Hub",
    "Northern": "Tabuk Wellfield Station",
    "Southern": "Abha Reservoir Station",
}
SOURCE_TYPES = ["Borehole", "Municipal Supply", "River Intake",
                "Reservoir", "Treated Effluent", "Desalination"]
ANALYSTS = ["A. Rahman", "M. Saleh", "N. Idris", "R. Costa",
            "T. Okafor", "L. Petrova", "J. Mwangi", "C. Delacroix"]
STATUSES_HIGH = ["Approved", "Approved", "Approved", "Approved", "Pending"]
STATUSES_MID = ["Approved", "Pending", "Pending", "In Review", "Approved"]
STATUSES_LOW = ["In Review", "Rejected", "Rejected", "Pending", "In Review"]

# Each lab runs a slightly different operation — gives the scorecard real
# spread instead of every lab averaging out to the same number (keyed by lab_id).
LAB_QUALITY_BIAS = {1: 6.0, 2: 1.5, 3: -3.0, 4: 3.5, 5: -7.0, 6: -1.5}


def clamp(value, low, high):
    return max(low, min(high, value))


def quality_score(row, bias=0.0):
    """Derive a 5-99 quality score from how in-range the measurements are."""
    score = 100.0
    score -= abs(row["ph"] - 7.2) * 6.5
    score -= max(0.0, row["Turbidity"] - 3.2) * 7.5
    score -= max(0.0, row["Organic_carbon"] - 11.0) * 1.8
    score -= max(0.0, row["Trihalomethanes"] - 70.0) * 0.30
    score -= max(0.0, row["Hardness"] - 250.0) * 0.06
    score -= max(0.0, row["Chloramines"] - 7.5) * 2.6
    return round(clamp(score + bias + RNG.uniform(-7, 7), 5.0, 99.0), 1)


def build_warehouse():
    """Read the CSV, enrich it, and write the SQLite warehouse."""
    print(f"Reading {CSV_PATH} ...")
    df = pd.read_csv(CSV_PATH)
    # Fill the gaps the raw dataset ships with, using column means.
    for col in ("ph", "Sulfate", "Trihalomethanes"):
        df[col] = df[col].fillna(round(df[col].mean(), 4))
    df = df.where(pd.notnull(df), None)

    WAREHOUSE.parent.mkdir(parents=True, exist_ok=True)
    if WAREHOUSE.exists():
        WAREHOUSE.unlink()

    conn = sqlite3.connect(WAREHOUSE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE labs (
            id INTEGER PRIMARY KEY,
            lab_code TEXT, name TEXT, region TEXT,
            manager TEXT, established_year INTEGER
        )""")
    cur.executemany(
        "INSERT INTO labs VALUES (?,?,?,?,?,?)",
        [(i + 1, *lab) for i, lab in enumerate(LABS)],
    )

    cur.execute("""
        CREATE TABLE water_samples (
            id INTEGER PRIMARY KEY,
            sample_code TEXT, lab_id INTEGER, lab TEXT, site TEXT,
            region TEXT, source_type TEXT, collected_on TEXT,
            collected_month TEXT, analyst TEXT,
            ph REAL, hardness REAL, solids REAL, chloramines REAL,
            sulfate REAL, conductivity REAL, organic_carbon REAL,
            trihalomethanes REAL, turbidity REAL,
            potable INTEGER, potability TEXT,
            quality_score REAL, status TEXT
        )""")

    # 24 months of history with a gentle upward volume trend, so the
    # monthly chart reads like a growing operation rather than noise.
    months = []
    y, m = 2024, 6
    for _ in range(24):
        months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    month_weights = [1.0 + 0.055 * idx for idx in range(len(months))]
    last_year, last_month = months[-1]

    rows = []
    for i, rec in enumerate(df.itertuples(index=False), start=1):
        rec = rec._asdict()
        lab_id = RNG.randint(1, len(LABS))
        lab_code, lab_name, region, _mgr, _yr = LABS[lab_id - 1]
        my, mm = RNG.choices(months, weights=month_weights, k=1)[0]
        max_day = 18 if (my, mm) == (last_year, last_month) \
            else calendar.monthrange(my, mm)[1]
        collected = date(my, mm, RNG.randint(1, max_day))
        score = quality_score(rec, LAB_QUALITY_BIAS[lab_id])
        if score >= 80:
            status = RNG.choice(STATUSES_HIGH)
        elif score >= 62:
            status = RNG.choice(STATUSES_MID)
        else:
            status = RNG.choice(STATUSES_LOW)
        potable = int(rec["Potability"])
        rows.append((
            i,
            f"WS-{collected.year}-{i:05d}",
            lab_id, lab_name, SITES[region], region,
            RNG.choice(SOURCE_TYPES),
            collected.isoformat(),
            collected.strftime("%Y-%m"),
            RNG.choice(ANALYSTS),
            round(rec["ph"], 3), round(rec["Hardness"], 3),
            round(rec["Solids"], 2), round(rec["Chloramines"], 3),
            round(rec["Sulfate"], 3), round(rec["Conductivity"], 3),
            round(rec["Organic_carbon"], 3), round(rec["Trihalomethanes"], 3),
            round(rec["Turbidity"], 3),
            potable, "Potable" if potable else "Not Potable",
            score, status,
        ))
    cur.executemany(
        "INSERT INTO water_samples VALUES (%s)" % ",".join(["?"] * 23), rows
    )
    conn.commit()
    conn.close()
    print(f"Warehouse built: {WAREHOUSE}  ({len(rows)} samples, {len(LABS)} labs)")


# ----------------------------------------------------------------------------
QUERIES = [
    ("All Water Samples",
     "Row-level sample register — every potability test with lab, site and "
     "analyst metadata. Powers the Data Explorer.",
     """SELECT sample_code, lab, site, region, source_type, collected_on,
       analyst, ph, hardness, solids, chloramines, sulfate, conductivity,
       organic_carbon, trihalomethanes, turbidity, potability,
       quality_score, status
FROM water_samples
ORDER BY collected_on DESC"""),

    ("Samples by Lab",
     "Total water samples processed by each laboratory.",
     """SELECT lab, COUNT(*) AS sample_count
FROM water_samples
GROUP BY lab
ORDER BY sample_count DESC"""),

    ("Potability by Source Type",
     "Potable-water pass rate broken down by the type of water source.",
     """SELECT source_type,
       SUM(potable) AS potable_samples,
       COUNT(*) AS total_samples,
       ROUND(100.0 * SUM(potable) / COUNT(*), 1) AS potable_pct
FROM water_samples
GROUP BY source_type
ORDER BY potable_pct DESC"""),

    ("Sample Status Breakdown",
     "How many samples sit in each review status.",
     """SELECT status, COUNT(*) AS sample_count
FROM water_samples
GROUP BY status
ORDER BY sample_count DESC"""),

    ("Monthly Sample Volume",
     "Samples logged per month with the average quality score for the month.",
     """SELECT collected_month AS month,
       COUNT(*) AS sample_count,
       ROUND(AVG(quality_score), 1) AS avg_quality
FROM water_samples
GROUP BY collected_month
ORDER BY month"""),

    ("Average Turbidity by Lab",
     "Mean turbidity and pH of the samples each lab handled.",
     """SELECT lab,
       ROUND(AVG(turbidity), 2) AS avg_turbidity,
       ROUND(AVG(ph), 2) AS avg_ph
FROM water_samples
GROUP BY lab
ORDER BY avg_turbidity DESC"""),

    ("Lab Quality Scorecard",
     "Per-lab scorecard joining the labs dimension — volume, average quality "
     "and potable pass rate.",
     """SELECT l.name AS lab, l.region, l.manager,
       COUNT(s.id) AS samples,
       ROUND(AVG(s.quality_score), 1) AS avg_quality,
       ROUND(100.0 * SUM(s.potable) / COUNT(s.id), 1) AS potable_pct
FROM water_samples s
JOIN labs l ON s.lab_id = l.id
GROUP BY l.name, l.region, l.manager
ORDER BY avg_quality DESC"""),
]

# title, type, dataset name, config, x, y, w, h
WIDGETS = [
    ("Total Samples Logged", "kpi", "Samples by Lab",
     {"value_field": "sample_count", "rollup": "sum"}, 0, 0, 3, 4),
    ("Potable Samples", "kpi", "Potability by Source Type",
     {"value_field": "potable_samples", "rollup": "sum"}, 3, 0, 3, 4),
    ("Avg Quality Score", "kpi", "Monthly Sample Volume",
     {"value_field": "avg_quality", "rollup": "avg"}, 6, 0, 3, 4),
    ("Active Labs", "kpi", "Samples by Lab",
     {"value_field": "sample_count", "rollup": "count"}, 9, 0, 3, 4),
    ("Samples by Lab", "bar", "Samples by Lab",
     {"category_field": "lab", "value_field": "sample_count"}, 0, 4, 7, 8),
    ("Sample Status Breakdown", "pie", "Sample Status Breakdown",
     {"category_field": "status", "value_field": "sample_count"}, 7, 4, 5, 8),
    ("Monthly Sample Volume", "line", "Monthly Sample Volume",
     {"category_field": "month", "value_field": "sample_count"}, 0, 12, 7, 8),
    ("Potability % by Source Type", "bar", "Potability by Source Type",
     {"category_field": "source_type", "value_field": "potable_pct"}, 7, 12, 5, 8),
    ("Average Turbidity by Lab", "area", "Average Turbidity by Lab",
     {"category_field": "lab", "value_field": "avg_turbidity"}, 0, 20, 7, 8),
    ("Lab Quality Scorecard", "table", "Lab Quality Scorecard",
     {}, 7, 20, 5, 8),
]


def seed_platform():
    admin = User.objects.filter(role=User.Role.ADMIN).order_by("id").first()
    if admin is None:
        admin = User.objects.order_by("id").first()
    print(f"Owner: {admin.username}")

    # --- Connection -------------------------------------------------------
    ds, _ = DataSource.objects.update_or_create(
        name="Water Quality DW",
        defaults=dict(
            description="Water-quality LIMS warehouse (potability tests, "
                        "labs, sites). Built from the water_potability dataset.",
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

    # --- Saved queries + datasets ----------------------------------------
    # Clear any previous run so the script is idempotent.
    Dataset.objects.filter(query__datasource=ds).delete()
    QueryDefinition.objects.filter(datasource=ds).delete()

    datasets = {}
    for name, desc, sql in QUERIES:
        query = QueryDefinition.objects.create(
            name=name, description=desc, datasource=ds,
            mode=QueryDefinition.Mode.RAW, raw_sql=sql, generated_sql=sql,
            owner=admin, visibility="shared",
        )
        dataset = Dataset.objects.create(
            name=name, description=desc, query=query,
            owner=admin, visibility="shared",
        )
        result = refresh_dataset(dataset)
        query.last_run_at = timezone.now()
        query.last_row_count = result["row_count"]
        query.save(update_fields=["last_run_at", "last_row_count"])
        datasets[name] = dataset
        print(f"  Dataset '{name}': {result['row_count']} rows")

    # --- Dashboard --------------------------------------------------------
    Dashboard.objects.filter(name="Water Quality Operations",
                             owner=admin).delete()
    dash = Dashboard.objects.create(
        name="Water Quality Operations",
        description="Live operational view of water-potability testing "
                    "across all regional labs.",
        owner=admin, visibility="shared",
    )
    for order, (title, kind, ds_name, config, x, y, w, h) in enumerate(WIDGETS):
        Widget.objects.create(
            dashboard=dash, title=title, widget_type=kind,
            dataset=datasets[ds_name], config=config,
            x=x, y=y, w=w, h=h, order=order,
        )
    print(f"Dashboard: {dash.name}  ({len(WIDGETS)} widgets)")


if __name__ == "__main__":
    build_warehouse()
    seed_platform()
    print("\nDone. Refresh the app — Connections, Saved Queries, Datasets, "
          "Dashboards and Data Explorer are now populated.")
