"""Build the fabrication demonstration warehouse (SQLite), deterministically.

A mid-size engineer-to-order fabrication shop (pressure vessels, heat exchangers,
skids, tanks, piping, columns), 2025-01 to the data date. The figures are invented
but behave like a real shop, so the boards show real-looking problems:

* two projects run over cost and late (CPI ~0.85, SPI ~0.85), and their NCRs,
  late operations and late shipments are visibly higher;
* the welding bay is the bottleneck (loaded over 100 % in peak weeks);
* the weld repair rate improves after a quality push in April 2026;
* two suppliers are chronically late (about 4 lines in 5); plates and forgings run late most often;
* recordable injuries fall over the period while man-hours rise;
* problems concentrate as they do in a real shop (Pareto): welding and dimensional
  NCRs, and workmanship and procedure causes, are most of them; material defects cost
  the most per NCR; hand and eye injuries are most incidents; each material's first two
  (strategic) suppliers take the large orders, the rest are spot buys.

Weights and multipliers only reshape what each draw picks: every value still takes one
random draw, so the other stories (and every count) stay as they were.

The business around the shop (sales and the order book, invoices and cash, stock,
maintenance, people and their certificates) is built from these tables afterwards, each
area with its own random stream: see seed_fabrication/business.py for its stories.

People appear as codes only (welder W-012, PM-03); clients are "Client A".."L".

Run:  .venv/bin/python -m seed_fabrication.build_warehouse [path]
"""
from __future__ import annotations

import math
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from seed_fabrication.business import TABLES as BUSINESS_TABLES, build_business

DATA_DATE = date(2026, 11, 22)
START = date(2025, 1, 6)                     # a Monday
SEED = 20261122

TYPES = {  # product type: (code, items range, welds per item range, PWHT?)
    "Pressure vessel": ("PV", (6, 18), (40, 110), True),
    "Heat exchanger": ("HX", (8, 20), (30, 80), True),
    "Process skid": ("SK", (10, 30), (20, 60), False),
    "Storage tank": ("TK", (4, 10), (60, 150), False),
    "Piping spools": ("PS", (90, 220), (4, 12), False),
    "Column / tower": ("CL", (3, 8), (80, 160), True),
}
CLIENTS = [("Client A", "Refining"), ("Client B", "Petrochemicals"), ("Client C", "Gas processing"),
           ("Client D", "Utilities"), ("Client E", "Refining"), ("Client F", "Mining"),
           ("Client G", "Petrochemicals"), ("Client H", "Water")]
SITES = ["Riyadh", "Jubail", "Yanbu", "Dammam", "Ras Tanura", "Jazan"]
# Route: operation, work centre, base hours per item, fraction of the item window.
ROUTE = [
    ("Cutting", "Cutting bay", 6), ("Forming", "Rolling & forming", 10), ("Fit-up", "Fit-up", 14),
    ("Welding", "Welding bay", 34), ("NDT", "NDT", 6), ("PWHT", "Heat treatment", 8),
    ("Hydrotest", "Testing", 5), ("Painting", "Blast & paint", 9), ("Final inspection", "QC inspection", 3),
]
CAPACITY = {  # weekly hours available per work centre (welding is the constraint)
    "Cutting bay": 260, "Rolling & forming": 380, "Fit-up": 560, "Welding bay": 740, "NDT": 300,
    "Heat treatment": 260, "Testing": 340, "Blast & paint": 450, "QC inspection": 250,
}
PROCESSES = [("SMAW", 0.30), ("GTAW", 0.25), ("SAW", 0.25), ("FCAW", 0.20)]
PROCESS_RISK = {"SMAW": 1.15, "GTAW": 0.8, "SAW": 0.75, "FCAW": 1.3}
NDT = [("RT", 0.45), ("UT", 0.30), ("MT", 0.15), ("PT", 0.10)]
NCR_CATEGORY = [("Welding", 0.48), ("Dimensional", 0.25), ("Material", 0.11), ("Coating", 0.07),
                ("Supplier", 0.05), ("Documentation", 0.04)]
ROOT_CAUSE = [("Workmanship", 0.50), ("Procedure not followed", 0.22), ("Material defect", 0.13),
              ("Drawing / design", 0.06), ("Equipment", 0.05), ("Inadequate supervision", 0.04)]
# A material defect means scrap and re-ordering; a drawing error, rework of several items.
CAUSE_COST = {"Material defect": 2.4, "Drawing / design": 1.6, "Equipment": 0.8,
              "Inadequate supervision": 0.7}
# Order value by supplier rank within its material: strategic suppliers get the packages.
SUPPLIER_VALUE = {"01": 2.0, "02": 1.25}
TAIL_SUPPLIER_VALUE = 0.25
PO_CATEGORY = {  # category: (lead days, value range SAR, lines per project range)
    "Plates": (42, (40_000, 650_000), (6, 18)), "Pipes & fittings": (35, (8_000, 180_000), (10, 40)),
    "Forgings & flanges": (56, (12_000, 260_000), (6, 20)), "Valves": (63, (10_000, 340_000), (4, 16)),
    "Bought-out equipment": (84, (60_000, 1_400_000), (1, 6)), "Consumables": (10, (2_000, 40_000), (8, 24)),
    "Paint & coating": (14, (5_000, 90_000), (2, 8)),
}
INCIDENT_TYPES = [("Hand injury", 0.42), ("Eye injury", 0.22), ("Slip / trip", 0.17),
                  ("Dropped object", 0.08), ("Burn / arc flash", 0.05), ("Lifting / rigging", 0.03),
                  ("Strain", 0.03)]


def weighted(rng, pairs):
    x, acc = rng.random(), 0.0
    for value, w in pairs:
        acc += w
        if x <= acc:
            return value
    return pairs[-1][0]


def s_curve(f):
    """Cumulative share of work done at fraction f of the duration (logistic)."""
    f = max(0.0, min(1.0, f))
    lo, hi = 1 / (1 + math.exp(6)), 1 / (1 + math.exp(-6))
    return (1 / (1 + math.exp(-12 * (f - 0.5))) - lo) / (hi - lo)


def monday(d):
    return d - timedelta(days=d.weekday())


def iso(d):
    return d.isoformat() if d else None


SCHEMA = """
CREATE TABLE projects (
  project_no TEXT PRIMARY KEY, project_name TEXT, client TEXT, client_sector TEXT,
  product_type TEXT, site TEXT, project_manager TEXT, contract_value_sar REAL,
  budget_cost_sar REAL, budget_hours REAL, start_date TEXT, planned_finish TEXT,
  forecast_finish TEXT, status TEXT, cpi REAL, spi REAL);
CREATE TABLE evm_weekly (
  project_no TEXT, week_start TEXT, pv_sar REAL, ev_sar REAL, ac_sar REAL,
  PRIMARY KEY (project_no, week_start));
CREATE TABLE items (
  item_no TEXT PRIMARY KEY, project_no TEXT, description TEXT, weight_t REAL, welds INTEGER);
CREATE TABLE operations (
  op_id INTEGER PRIMARY KEY, item_no TEXT, project_no TEXT, seq INTEGER, operation TEXT,
  work_center TEXT, planned_start TEXT, planned_finish TEXT, actual_start TEXT,
  actual_finish TEXT, planned_hours REAL, actual_hours REAL, status TEXT, days_late INTEGER);
CREATE TABLE wc_weekly (
  work_center TEXT, week_start TEXT, capacity_hours REAL, load_hours REAL,
  PRIMARY KEY (work_center, week_start));
CREATE TABLE welds (
  weld_no TEXT PRIMARY KEY, project_no TEXT, item_no TEXT, welder TEXT, process TEXT,
  ndt_method TEXT, welded_on TEXT, result TEXT, repairs INTEGER);
CREATE TABLE ncrs (
  ncr_no TEXT PRIMARY KEY, project_no TEXT, raised_on TEXT, closed_on TEXT, status TEXT,
  category TEXT, severity TEXT, detected_at TEXT, root_cause TEXT, cost_sar REAL,
  days_open INTEGER);
CREATE TABLE purchase_orders (
  po_line TEXT PRIMARY KEY, project_no TEXT, supplier TEXT, category TEXT, ordered_on TEXT,
  promised_on TEXT, received_on TEXT, value_sar REAL, status TEXT, days_late INTEGER);
CREATE TABLE shipments (
  shipment_no TEXT PRIMARY KEY, project_no TEXT, items INTEGER, weight_t REAL,
  planned_ship TEXT, actual_ship TEXT, destination TEXT, status TEXT, days_late INTEGER);
CREATE TABLE change_orders (
  co_no TEXT PRIMARY KEY, project_no TEXT, raised_on TEXT, decided_on TEXT, status TEXT,
  reason TEXT, value_sar REAL, days_to_decide INTEGER);
CREATE TABLE hse_monthly (
  month TEXT PRIMARY KEY, man_hours REAL, near_misses INTEGER, first_aid INTEGER,
  recordables INTEGER, lost_time INTEGER);
CREATE TABLE hse_incidents (
  incident_no TEXT PRIMARY KEY, occurred_on TEXT, incident_type TEXT, classification TEXT,
  work_center TEXT, project_no TEXT, days_lost INTEGER);
CREATE TABLE data_info (key TEXT PRIMARY KEY, value TEXT);
"""


def build(path: Path, verbose: bool = True) -> dict:
    rng = random.Random(SEED)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rows = {k: [] for k in ("projects", "evm", "items", "ops", "welds", "ncrs", "pos",
                            "ships", "cos", "incidents")}
    load = {}                                      # (work centre, week) -> planned hours

    welder_skill = {f"W-{i:03d}": rng.uniform(0.006, 0.05) for i in range(1, 41)}
    for w in ("W-007", "W-023"):                    # two welders who need retraining
        welder_skill[w] = rng.uniform(0.07, 0.09)
    welders = list(welder_skill)
    suppliers = {}
    for cat in PO_CATEGORY:
        for k in range(1, rng.randint(4, 7) + 1):
            suppliers.setdefault(cat, []).append(f"{cat.split(' ')[0]} supplier {k:02d}")
    late_suppliers = {suppliers["Plates"][1], suppliers["Forgings & flanges"][0]}

    names = {"PV": ["Amine absorber", "Separator", "KO drum", "Reflux drum", "Flash drum"],
             "HX": ["Shell & tube exchanger", "Reboiler", "Condenser", "Feed preheater"],
             "SK": ["Chemical injection skid", "Metering skid", "Pump skid", "Filter skid"],
             "TK": ["Crude storage tank", "Water tank", "Diesel tank"],
             "PS": ["Piping spools, unit", "Process piping package, area"],
             "CL": ["Stripper column", "Fractionation column", "Contactor tower"]}
    type_names = list(TYPES)
    n_projects = 24
    for p in range(n_projects):
        ptype = type_names[p % len(type_names)] if p < 18 else rng.choice(type_names)
        code, items_rng, welds_rng, pwht = TYPES[ptype]
        start = START + timedelta(days=int(p * 26 + rng.randint(-10, 10)))
        dur = rng.randint(190, 400)
        planned_finish = start + timedelta(days=dur)
        troubled = p in (15, 19)                       # two current projects in trouble
        cpi = rng.uniform(0.82, 0.88) if troubled else rng.uniform(0.93, 1.08)
        spi = rng.uniform(0.82, 0.87) if troubled else rng.uniform(0.92, 1.05)
        contract = round(rng.uniform(4e6, 55e6), -3)
        bac = round(contract * rng.uniform(0.74, 0.82), -3)
        project_no = f"F-{25 if start.year == 2025 else 26}{p + 1:02d}"
        client, sector = CLIENTS[(p * 3) % len(CLIENTS)]
        name = f"{rng.choice(names[code])} {'package ' + str(rng.randint(1, 9)) if code == 'PS' else ''}".strip()
        forecast_finish = start + timedelta(days=int(dur / spi))
        on_hold = p == 20
        n_items = rng.randint(*items_rng)
        budget_hours = 0.0
        # ---- items and their routes ------------------------------------------------
        for i in range(1, n_items + 1):
            item_no = f"{project_no}-{code}{i:03d}"
            size = rng.uniform(0.6, 1.6) * (6 if code in ("PV", "HX", "CL", "TK") else 1)
            welds = rng.randint(*welds_rng)
            rows["items"].append((item_no, project_no, f"{name} item {i}", round(size * 3.2, 1), welds))
            # items are spread over the first 80 % of the project; each takes ~25 %.
            offset = (i - 1) / max(1, n_items - 1) * 0.75 * dur
            window = dur * rng.uniform(0.18, 0.3)
            route = [r for r in ROUTE if pwht or r[0] != "PWHT"]
            t_plan = start + timedelta(days=offset)
            delay_factor = 1 / spi
            weld_done = None
            for seq, (op, wc, base) in enumerate(route, start=1):
                plan_days = max(1, round(window * base / sum(b for _, _, b in route)))
                p_start, p_finish = t_plan, t_plan + timedelta(days=plan_days)
                t_plan = p_finish
                hours = round(base * size * rng.uniform(0.8, 1.2), 1)
                budget_hours += hours
                a_start = start + (p_start - start) * delay_factor + timedelta(days=rng.randint(-2, 4))
                a_finish = a_start + timedelta(days=max(1, round(plan_days * delay_factor * rng.uniform(0.85, 1.3))))
                if on_hold and a_start > date(2026, 6, 1):
                    a_start = a_finish = None
                if a_start and a_start > DATA_DATE:
                    status, a_s, a_f, actual = "Not started", None, None, None
                elif a_finish and a_finish > DATA_DATE:
                    status, a_s, a_f = "In progress", a_start, None
                    actual = round(hours / cpi * (DATA_DATE - a_start).days / max(1, (a_finish - a_start).days), 1)
                elif a_start:
                    status, a_s, a_f, actual = "Done", a_start, a_finish, round(hours / cpi * rng.uniform(0.85, 1.2), 1)
                else:
                    status, a_s, a_f, actual = "On hold", None, None, None
                ref = a_f or (DATA_DATE if status == "In progress" else None)
                days_late = max(0, (ref - p_finish).days) if ref else (max(0, (DATA_DATE - p_finish).days) if p_finish < DATA_DATE else 0)
                rows["ops"].append((None, item_no, project_no, seq, op, wc, iso(p_start), iso(p_finish),
                                    iso(a_s), iso(a_f), hours, actual, status, days_late))
                # load: planned hours spread over the weeks the work is (or will be) done
                ls, lf = (a_s or p_start), (a_f or (a_finish if a_finish else p_finish))
                weeks = max(1, ((monday(lf) - monday(ls)).days // 7) + 1)
                for wk in range(weeks):
                    key = (wc, monday(ls) + timedelta(weeks=wk))
                    load[key] = load.get(key, 0.0) + hours / weeks
                if op == "Welding" and status == "Done":
                    weld_done = a_f
            # ---- welds and NDT for this item (when welded) ---------------------------
            if weld_done:
                for w in range(1, welds + 1):
                    welder = rng.choice(welders)
                    process = weighted(rng, PROCESSES)
                    p_rep = welder_skill[welder] * PROCESS_RISK[process] * (0.62 if weld_done >= date(2026, 4, 1) else 1.0)
                    p_rep *= 1.4 if troubled else 1.0
                    rejected = rng.random() < p_rep
                    welded_on = weld_done - timedelta(days=rng.randint(0, 12))
                    rows["welds"].append((f"{item_no}-W{w:03d}", project_no, item_no, welder, process,
                                          weighted(rng, NDT), iso(welded_on),
                                          "Rejected" if rejected else "Accepted",
                                          (1 + (rng.random() < 0.15)) if rejected else 0))
        # ---- earned value, weekly ---------------------------------------------------------
        wk = monday(start)
        while wk <= monday(min(DATA_DATE, forecast_finish + timedelta(days=21))):
            f = (wk - start).days / dur
            pv = bac * s_curve(f)
            ev = bac * s_curve(f * spi)
            ac = ev / cpi * rng.uniform(0.98, 1.02)
            if on_hold and wk > date(2026, 6, 1):
                ev = rows["evm"][-1][3] if rows["evm"] and rows["evm"][-1][0] == project_no else ev
                ac = rows["evm"][-1][4] if rows["evm"] and rows["evm"][-1][0] == project_no else ac
            rows["evm"].append((project_no, iso(wk), round(pv), round(ev), round(ac)))
            wk += timedelta(weeks=1)
        done = forecast_finish <= DATA_DATE
        status = "On hold" if on_hold else "Completed" if done else "Active"
        rows["projects"].append((project_no, name, client, sector, ptype, rng.choice(SITES),
                                 f"PM-{(p % 9) + 1:02d}", contract, bac, round(budget_hours),
                                 iso(start), iso(planned_finish), iso(forecast_finish), status,
                                 round(cpi, 3), round(spi, 3)))
        # ---- NCRs ---------------------------------------------------------------------------
        n_ncr = int(rng.uniform(8, 22) * (2.4 if troubled else 1.0))
        for k in range(1, n_ncr + 1):
            raised = start + timedelta(days=rng.randint(10, max(11, min(dur + 60, (DATA_DATE - start).days))))
            if raised > DATA_DATE:
                continue
            category = weighted(rng, NCR_CATEGORY)
            severity = weighted(rng, [("Minor", 0.62), ("Major", 0.32), ("Critical", 0.06)])
            days_to_close = int(rng.lognormvariate(2.6 if severity == "Minor" else 3.1, 0.6))
            closed = raised + timedelta(days=days_to_close)
            is_open = closed > DATA_DATE
            base_cost = rng.lognormvariate(8.6, 0.9) * {"Minor": 1, "Major": 3.2, "Critical": 9}[severity]
            detected_at = weighted(rng, [("In-process inspection", 0.55), ("Final inspection", 0.2),
                                         ("Client inspection", 0.12), ("Receiving inspection", 0.13)])
            cause = weighted(rng, ROOT_CAUSE)
            cost = round(base_cost * CAUSE_COST.get(cause, 1.0), -1)
            rows["ncrs"].append((f"NCR-{project_no[2:]}-{k:03d}", project_no, iso(raised),
                                 None if is_open else iso(closed), "Open" if is_open else "Closed",
                                 category, severity, detected_at, cause, cost,
                                 ((DATA_DATE if is_open else closed) - raised).days))
        # ---- purchase orders ------------------------------------------------------------
        line = 0
        for cat, (lead, val, lines_rng) in PO_CATEGORY.items():
            for _ in range(rng.randint(*lines_rng)):
                line += 1
                ordered = start + timedelta(days=rng.randint(0, max(1, int(dur * 0.55))))
                if ordered > DATA_DATE:
                    continue
                supplier = rng.choice(suppliers[cat])
                promised = ordered + timedelta(days=int(lead * rng.uniform(0.8, 1.2)))
                slip = rng.lognormvariate(2.4, 0.7) - 6 if supplier in late_suppliers else (
                    rng.lognormvariate(1.6, 0.9) if rng.random() < 0.28 else -rng.randint(0, 6))
                received = promised + timedelta(days=int(slip))
                if received > DATA_DATE:
                    status = "Overdue" if promised < DATA_DATE else "Open"
                    days_late = max(0, (DATA_DATE - promised).days)
                    received = None
                else:
                    status, days_late = "Received", max(0, (received - promised).days)
                value = rng.uniform(*val) * SUPPLIER_VALUE.get(supplier[-2:], TAIL_SUPPLIER_VALUE)
                rows["pos"].append((f"PO-{project_no[2:]}-{line:03d}", project_no, supplier, cat, iso(ordered),
                                    iso(promised), iso(received), round(value, -2), status, days_late))
        # ---- shipments ---------------------------------------------------------------------
        n_ship = rng.randint(2, 9) if code != "PS" else rng.randint(6, 14)
        for k in range(1, n_ship + 1):
            planned = start + timedelta(days=int(dur * (0.55 + 0.45 * k / n_ship)))
            # Late by the schedule slip (most projects barely slip), give or take a week.
            slip_days = (planned - start).days * max(0.0, 1 / spi - 1) * 0.35
            actual = planned + timedelta(days=int(slip_days) + rng.randint(-10, 3))
            if on_hold and planned > date(2026, 6, 1):
                actual = None
            if actual and actual > DATA_DATE:
                actual = None
            status = "Shipped" if actual else ("Late, not shipped" if planned < DATA_DATE else "Planned")
            late = (max(0, (actual - planned).days) if actual else
                    max(0, (DATA_DATE - planned).days) if planned < DATA_DATE else 0)
            rows["ships"].append((f"SH-{project_no[2:]}-{k:02d}", project_no, rng.randint(1, 12),
                                  round(rng.uniform(4, 90), 1), iso(planned), iso(actual),
                                  rng.choice(SITES), status, late))
        # ---- change orders -------------------------------------------------------------
        for k in range(1, rng.randint(1, 9 if not troubled else 16) + 1):
            raised = start + timedelta(days=rng.randint(15, dur))
            if raised > DATA_DATE:
                continue
            days = int(rng.lognormvariate(2.8, 0.6))
            decided = raised + timedelta(days=days)
            st = "Pending" if decided > DATA_DATE else weighted(rng, [("Approved", 0.78), ("Rejected", 0.22)])
            rows["cos"].append((f"CO-{project_no[2:]}-{k:02d}", project_no, iso(raised),
                                None if st == "Pending" else iso(decided), st,
                                weighted(rng, [("Client scope change", 0.45), ("Design development", 0.25),
                                               ("Site condition", 0.12), ("Specification change", 0.18)]),
                                round(contract * rng.uniform(0.004, 0.04), -3),
                                (min(decided, DATA_DATE) - raised).days))

    con.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows["projects"])
    con.executemany("INSERT INTO evm_weekly VALUES (?,?,?,?,?)", rows["evm"])
    con.executemany("INSERT INTO items VALUES (?,?,?,?,?)", rows["items"])
    con.executemany("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows["ops"])
    con.executemany("INSERT INTO welds VALUES (?,?,?,?,?,?,?,?,?)", rows["welds"])
    con.executemany("INSERT INTO ncrs VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows["ncrs"])
    con.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?)", rows["pos"])
    con.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?)", rows["ships"])
    con.executemany("INSERT INTO change_orders VALUES (?,?,?,?,?,?,?,?)", rows["cos"])

    # ---- work centre capacity vs load, weekly (incl. 8 weeks ahead) ------------------------
    wc_rows = []
    wk = START
    while wk <= monday(DATA_DATE) + timedelta(weeks=8):
        for wc, cap in CAPACITY.items():
            holiday = 0.6 if (wk.month == 4 and wk.day < 8) or (wk.month == 6 and 10 <= wk.day < 20) else 1.0
            wc_rows.append((wc, iso(wk), round(cap * holiday), round(load.get((wc, wk), 0.0), 1)))
        wk += timedelta(weeks=1)
    con.executemany("INSERT INTO wc_weekly VALUES (?,?,?,?)", wc_rows)

    # ---- safety: man-hours from the work, incidents falling over time ----------------------
    hours_by_month = {}
    for op in rows["ops"]:
        if op[11] and op[9]:
            m = op[9][:7]
            hours_by_month[m] = hours_by_month.get(m, 0.0) + op[11] * 1.7   # direct + indirect
    hse, incidents, inc_no = [], [], 0
    month = date(2025, 1, 1)
    while month <= DATA_DATE.replace(day=1):
        key = month.strftime("%Y-%m")
        mh = round(hours_by_month.get(key, 0.0) * 4 + 52_000)   # direct work + the rest of the site
        trend = 1.0 - 0.45 * ((month - date(2025, 1, 1)).days / (DATA_DATE - date(2025, 1, 1)).days)
        counts = {}
        for cls, rate in (("Near miss", 9.0), ("First aid", 3.2), ("Recordable", 0.6), ("Lost time", 0.2)):
            lam = rate * trend * mh / 60_000
            n = sum(1 for _ in range(60) if rng.random() < lam / 60)
            counts[cls] = n
            for _ in range(n):
                inc_no += 1
                day = month + timedelta(days=rng.randint(0, 27))
                if day > DATA_DATE:
                    continue
                incidents.append((f"HSE-{inc_no:04d}", iso(day), weighted(rng, INCIDENT_TYPES), cls,
                                  rng.choice(list(CAPACITY)), rng.choice(rows["projects"])[0],
                                  rng.randint(1, 21) if cls == "Lost time" else 0))
        # A lost-time injury is also a recordable one (TRIR counts both).
        hse.append((key, mh, counts["Near miss"], counts["First aid"],
                    counts["Recordable"] + counts["Lost time"], counts["Lost time"]))
        month = (month + timedelta(days=32)).replace(day=1)
    con.executemany("INSERT INTO hse_monthly VALUES (?,?,?,?,?,?)", hse)
    con.executemany("INSERT INTO hse_incidents VALUES (?,?,?,?,?,?,?)", incidents)
    con.executemany("INSERT INTO data_info VALUES (?,?)", [
        ("data_date", DATA_DATE.isoformat()), ("generator", "seed_fabrication.build_warehouse"),
        ("seed", str(SEED)), ("note", "Demonstration data: invented, not from any client system."),
    ])
    # the business around the shop (sales, cash, stock, maintenance, people): its own random
    # streams, so nothing above changes
    build_business(con, DATA_DATE, SEED)
    for idx in ("operations(project_no)", "operations(work_center)", "welds(project_no)", "welds(welder)",
                "ncrs(project_no)", "purchase_orders(supplier)", "evm_weekly(week_start)"):
        con.execute(f"CREATE INDEX IF NOT EXISTS ix_{idx.replace('(', '_').replace(')', '')} ON {idx}")
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
              ("projects", "evm_weekly", "items", "operations", "wc_weekly", "welds", "ncrs",
               "purchase_orders", "shipments", "change_orders", "hse_monthly", "hse_incidents",
               *BUSINESS_TABLES)}
    con.close()
    if verbose:
        for t, n in counts.items():
            print(f"  {t:16s} {n:>7,}")
    return counts


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data" / "fabrication_demo.sqlite3"
    build(out)
    print(f"Built {out}")
