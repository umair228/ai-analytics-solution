"""
Build the Refinery LIMS demo data warehouse (SQLite).

Generates an authentic LabWare-style operational warehouse for the Refinery Lab
(the refinery complex) covering the demo use cases:

  * Samples & QC test results (ASTM/IP methods, spec limits, pass/fail, TAT)
  * Instruments + ON/OFF event log (downtime/uptime) + calibration records
  * Inventory items + PULL/RECEIVE transactions (reagent consumption)
  * Computer-vision inspection results (colour/appearance, corrosion, OCR ...)
  * Document register (SOPs / ASTM methods / COA / MSDS) for the chatbot

The LIMS-shaped tables (SAMPLE, INSTRUMENTS1_LOG, INVENTORY_ITEM,
INVENTORY_TRANS) deliberately mirror the real MSSQL column names so the
forecasting endpoints can read this SQLite file directly (SQLite read-mode).

Run standalone:  .venv/bin/python -m seed_refinery.build_warehouse
"""
import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .personnel import technician_names

REF_PATH = Path(__file__).resolve().parent / "reference.json"
# Committed, in-image location (not shadowed by the media volume) so the built
# warehouse is the same file the app + server point at. Overridable for ad-hoc
# builds via the FORECAST_WAREHOUSE_OUT env var.
WAREHOUSE = Path(
    os.environ.get("FORECAST_WAREHOUSE_OUT")
    or (Path(__file__).resolve().parent.parent / "forecasting" / "warehouse" / "refinery_lims.sqlite3")
)

# --------------------------------------------------------------------------
# Section canonicalisation — the parallel research agents used a few variant
# section codes; fold them onto the 8 canonical sections.
# --------------------------------------------------------------------------
SECTION_CANON = {
    "AROM_BTX": "NAPH_AROM",
    "NAPHTHA_PHYS": "NAPH_AROM",
    "MIDDLE_DIST": "MIDDIST",
    "DIESEL_PHYS": "MIDDIST",
}
SECTION_NAME = {
    "CRUDE_DIST": "Crude & Distillation",
    "MOGAS_CHEM": "Gasoline/Mogas",
    "NAPH_AROM": "Naphtha & Aromatics",
    "MIDDIST": "Middle Distillates",
    "LUBE_ASPHALT": "Fuel Oil & Asphalt",
    "GAS_LPG": "Gas & LPG",
    "ENV_WATER": "Environmental & Water",
    "QC_CAL": "QA/QC & Calibration",
}
# Sections that get a per-section sample-volume forecast (exclude the QC unit).
FORECAST_SECTIONS = [
    "Crude & Distillation", "Gasoline/Mogas", "Naphtha & Aromatics",
    "Middle Distillates", "Fuel Oil & Asphalt", "Gas & LPG",
    "Environmental & Water",
]
# 2021-baseline monthly sample volume per canonical section.
SECTION_BASE_VOLUME = {
    "CRUDE_DIST": 45, "MOGAS_CHEM": 62, "NAPH_AROM": 34, "MIDDIST": 70,
    "LUBE_ASPHALT": 24, "GAS_LPG": 30, "ENV_WATER": 40, "QC_CAL": 16,
}

PRIORITIES = [("Routine", 0.62, 24), ("High", 0.28, 8), ("Urgent", 0.10, 4)]
DOWNTIME_REASONS = [
    "Preventive Maintenance", "Breakdown", "Calibration", "Power Trip",
    "Awaiting Spare Parts", "Method Setup", "Carrier Gas Change",
]
# Named support staff who operate instruments, handle inventory, run
# calibrations and review vision inspections. Sourced from the shared personnel
# roster so every ENTERED_BY / TRANSACTION_BY / performed_by / reviewed_by value
# also has a matching platform login account.
TECHNICIANS = technician_names()

START_YEAR, START_MONTH = 2021, 1
END_YEAR, END_MONTH = 2026, 5   # "today" is 2026-06-01


class Rng:
    """Tiny deterministic PRNG wrapper (stdlib random with a fixed seed)."""

    def __init__(self, seed=20260602):
        import random
        self._r = random.Random(seed)

    def uniform(self, a, b):
        return self._r.uniform(a, b)

    def randint(self, a, b):
        return self._r.randint(a, b)

    def gauss(self, mu, sigma):
        return self._r.gauss(mu, sigma)

    def choice(self, seq):
        return self._r.choice(seq)

    def random(self):
        return self._r.random()

    def weighted(self, items, weights):
        return self._r.choices(items, weights=weights, k=1)[0]

    def sample(self, seq, k):
        k = min(k, len(seq))
        return self._r.sample(seq, k)


def canon(code):
    return SECTION_CANON.get(code, code)


def months_range():
    y, m = START_YEAR, START_MONTH
    out = []
    while (y, m) <= (END_YEAR, END_MONTH):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def month_days(y, m):
    if m == 12:
        nxt = datetime(y + 1, 1, 1)
    else:
        nxt = datetime(y, m + 1, 1)
    return (nxt - datetime(y, m, 1)).days


def seasonal(m, amp=0.12, phase=3):
    """Mild yearly seasonality factor centred on 1.0."""
    return 1.0 + amp * math.sin(2 * math.pi * (m - phase) / 12)


# --------------------------------------------------------------------------
def load_reference():
    ref = json.loads(REF_PATH.read_text())
    # group tests by product
    tests_by_product = {}
    for t in ref["tests"]:
        tests_by_product.setdefault(t["product_code"], []).append(t)
    # build group_name -> friendly section name map
    group_to_section = {}
    for s in ref["sections"]:
        c = canon(s["code"])
        for g in s["group_names"]:
            group_to_section[g] = SECTION_NAME[c]
        group_to_section[s["code"]] = SECTION_NAME[c]
    for variant, target in SECTION_CANON.items():
        group_to_section[variant] = SECTION_NAME[target]
    # section_code -> list of group_names (for authentic GROUP_NAME variety)
    section_groups = {}
    for s in ref["sections"]:
        section_groups.setdefault(canon(s["code"]), []).extend(s["group_names"])
    return ref, tests_by_product, group_to_section, section_groups


# --------------------------------------------------------------------------
def create_schema(cur):
    cur.executescript(
        """
        DROP TABLE IF EXISTS sections;
        DROP TABLE IF EXISTS analysts;
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS SAMPLE;
        DROP TABLE IF EXISTS test_results;
        DROP TABLE IF EXISTS instruments;
        DROP TABLE IF EXISTS INSTRUMENTS1_LOG;
        DROP TABLE IF EXISTS calibrations;
        DROP TABLE IF EXISTS INVENTORY_ITEM;
        DROP TABLE IF EXISTS INVENTORY_TRANS;
        DROP TABLE IF EXISTS vision_inspections;
        DROP TABLE IF EXISTS documents;

        CREATE TABLE sections (
            id INTEGER PRIMARY KEY, code TEXT, name TEXT,
            group_names TEXT, description TEXT, head_analyst TEXT
        );
        CREATE TABLE analysts (
            id INTEGER PRIMARY KEY, name TEXT, role TEXT,
            section_code TEXT, section TEXT
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY, code TEXT, name TEXT,
            stream_type TEXT, section_code TEXT, section TEXT
        );
        CREATE TABLE SAMPLE (
            SAMPLE_NUMBER INTEGER PRIMARY KEY, TEXT_ID TEXT,
            PRODUCT TEXT, PRODUCT_NAME TEXT, STREAM_TYPE TEXT,
            GROUP_NAME TEXT, SECTION TEXT, SAMPLING_POINT TEXT,
            PRIORITY TEXT, STATUS TEXT, STATUS_LABEL TEXT,
            SAMPLED_DATE TEXT, LOGIN_DATE TEXT, DUE_DATE TEXT,
            DATE_COMPLETED TEXT, LOGIN_MONTH TEXT,
            ANALYST TEXT, TAT_HOURS REAL, ON_TIME INTEGER,
            RESULT TEXT, TESTS_TOTAL INTEGER, TESTS_FAILED INTEGER
        );
        CREATE TABLE test_results (
            id INTEGER PRIMARY KEY, SAMPLE_NUMBER INTEGER, TEXT_ID TEXT,
            PRODUCT TEXT, SECTION TEXT, test_name TEXT, astm_method TEXT,
            value REAL, unit TEXT, spec_min REAL, spec_max REAL,
            in_spec INTEGER, instrument TEXT, analyst TEXT,
            result_date TEXT, result_month TEXT
        );
        CREATE TABLE instruments (
            id INTEGER PRIMARY KEY, asset_code TEXT, name TEXT, type TEXT,
            section_code TEXT, section TEXT, manufacturer TEXT, model TEXT,
            uptime_pct REAL, mtbf_days INTEGER, calibration_interval_days INTEGER,
            install_year INTEGER, status TEXT, location TEXT,
            last_calibration TEXT, next_calibration TEXT, calibration_overdue INTEGER
        );
        CREATE TABLE INSTRUMENTS1_LOG (
            id INTEGER PRIMARY KEY, INSTRUMENT TEXT, EVENT_TYPE TEXT,
            ENTERED_BY TEXT, ENTERED_ON TEXT, REASON TEXT, SECTION TEXT
        );
        CREATE TABLE calibrations (
            id INTEGER PRIMARY KEY, instrument TEXT, instrument_name TEXT,
            section TEXT, calib_date TEXT, next_due TEXT, result TEXT,
            performed_by TEXT, cert_no TEXT, standard_used TEXT, overdue INTEGER
        );
        CREATE TABLE INVENTORY_ITEM (
            ITEM_NUMBER INTEGER PRIMARY KEY, STOCK TEXT, ITEM_CODE TEXT,
            DESCRIPTION TEXT, INVENTORY_TYPE TEXT, UNITS TEXT,
            REORDER_LEVEL REAL, UNIT_COST REAL, SUPPLIER TEXT,
            HAZARD_CLASS TEXT, CURRENT_STOCK REAL, LOCATION TEXT,
            BELOW_REORDER INTEGER
        );
        CREATE TABLE INVENTORY_TRANS (
            id INTEGER PRIMARY KEY, ENTRY_CODE TEXT, INVENTORY_ITEM INTEGER,
            STOCK TEXT, TRANSACTION_TYPE TEXT, TRANSACTION_STATUS TEXT,
            TRANSACTION_DATE TEXT, TRANS_MONTH TEXT, QUANTITY REAL, UNITS TEXT,
            TRANSACTION_BY TEXT, DESCRIPTION TEXT, ORDER_NUMBER TEXT
        );
        CREATE TABLE vision_inspections (
            id INTEGER PRIMARY KEY, image_code TEXT, use_case TEXT,
            sample_text_id TEXT, section TEXT, category_predicted TEXT,
            confidence REAL, status TEXT, inspected_on TEXT, inspect_month TEXT,
            model_name TEXT, reviewed_by TEXT, ground_truth_match INTEGER
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, doc_code TEXT, title TEXT, doc_type TEXT,
            section_code TEXT, section TEXT, standard_ref TEXT, version TEXT,
            summary TEXT, keywords TEXT, effective_date TEXT, review_due TEXT,
            status TEXT
        );
        """
    )


# --------------------------------------------------------------------------
def build(verbose=True):
    ref, tests_by_product, group_to_section, section_groups = load_reference()
    rng = Rng()

    WAREHOUSE.parent.mkdir(parents=True, exist_ok=True)
    if WAREHOUSE.exists():
        WAREHOUSE.unlink()
    conn = sqlite3.connect(WAREHOUSE)
    cur = conn.cursor()
    create_schema(cur)

    # ---- Dimensions: sections -------------------------------------------
    # Pick a head per section: prefer an explicit "Section Head", otherwise fall
    # back to the most senior bench analyst so every section is headed by a real
    # named person (and never a blank).
    _HEAD_PRIORITY = ["Section Head", "Senior Analyst", "QC Chemist", "Analyst"]
    by_sec_role = {}
    for a in ref["analysts"]:
        by_sec_role.setdefault(canon(a["section_code"]), {}).setdefault(
            a["role"], a["name"])

    def head_for(section_code):
        roles = by_sec_role.get(section_code, {})
        for r in _HEAD_PRIORITY:
            if r in roles:
                return roles[r]
        return ""

    sec_rows = []
    for i, s in enumerate(ref["sections"], 1):
        c = canon(s["code"])
        sec_rows.append((i, c, SECTION_NAME[c], ", ".join(s["group_names"]),
                         s["description"], head_for(c)))
    cur.executemany("INSERT INTO sections VALUES (?,?,?,?,?,?)", sec_rows)

    # ---- Dimensions: analysts -------------------------------------------
    analysts_by_section = {}
    an_rows = []
    for i, a in enumerate(ref["analysts"], 1):
        c = canon(a["section_code"])
        name = SECTION_NAME.get(c, "Other")
        an_rows.append((i, a["name"], a["role"], c, name))
        analysts_by_section.setdefault(c, []).append(a["name"])
    cur.executemany("INSERT INTO analysts VALUES (?,?,?,?,?)", an_rows)
    all_analysts = [a["name"] for a in ref["analysts"]]

    def analyst_for(section_code):
        pool = analysts_by_section.get(section_code) or all_analysts
        return rng.choice(pool)

    # ---- Dimensions: products -------------------------------------------
    prod_rows = []
    product_by_code = {}
    for i, p in enumerate(ref["products"], 1):
        c = canon(p["section_code"])
        prod_rows.append((i, p["code"], p["name"], p["stream_type"], c,
                          SECTION_NAME.get(c, "Other")))
        product_by_code[p["code"]] = {**p, "section_code": c,
                                      "section": SECTION_NAME.get(c, "Other")}
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", prod_rows)

    # products grouped by canonical section (only those with tests)
    products_by_section = {}
    for code, p in product_by_code.items():
        if code in tests_by_product:
            products_by_section.setdefault(p["section_code"], []).append(code)

    # ---- Dimensions: instruments ----------------------------------------
    instruments = ref["instruments"]
    instr_by_section = {}
    instr_rows = []
    today = datetime(2026, 6, 1)
    for i, ins in enumerate(instruments, 1):
        c = canon(ins["section_code"])
        sec = SECTION_NAME.get(c, "Other")
        interval = int(ins["calibration_interval_days"])
        # last calibration sometime in the interval window; next = last+interval
        days_since = rng.randint(5, max(6, interval))
        last_cal = today - timedelta(days=days_since)
        next_cal = last_cal + timedelta(days=interval)
        overdue = 1 if next_cal < today else 0
        status = "Calibration Due" if overdue else rng.weighted(
            ["In Service", "In Service", "In Service", "Under Maintenance"],
            [0.8, 0.1, 0.05, 0.05])
        instr_rows.append((
            i, ins["asset_code"], ins["name"], ins["type"], c, sec,
            ins["manufacturer"], ins["model"], float(ins["typical_uptime_pct"]),
            int(ins["mtbf_days"]), interval, int(ins["install_year"]), status,
            f"{sec} Lab", last_cal.strftime("%Y-%m-%d"),
            next_cal.strftime("%Y-%m-%d"), overdue,
        ))
        instr_by_section.setdefault(c, []).append(ins["asset_code"])
    cur.executemany(
        "INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        instr_rows)

    def instrument_for(section_code, instrument_type=""):
        pool = instr_by_section.get(section_code)
        if not pool:
            pool = [ins["asset_code"] for ins in instruments]
        return rng.choice(pool)

    # ---- Fact: SAMPLE + test_results ------------------------------------
    months = months_range()
    n_months = len(months)
    sample_rows = []
    result_rows = []
    sample_no = 0
    result_id = 0
    text_seq = {}
    samples_index = []  # (text_id, section, login_dt) for vision linkage

    for mi, (y, m) in enumerate(months):
        trend = 1.0 + 0.004 * mi               # ~+5%/yr growth
        maxday = month_days(y, m)
        for sec_code, base in SECTION_BASE_VOLUME.items():
            sec_name = SECTION_NAME[sec_code]
            vol = base * trend * seasonal(m) * rng.uniform(0.85, 1.15)
            count = max(1, int(round(vol)))
            prods = products_by_section.get(sec_code)
            if not prods:
                continue
            groups = section_groups.get(sec_code, [sec_code])
            for _ in range(count):
                sample_no += 1
                pcode = rng.choice(prods)
                p = product_by_code[pcode]
                day = rng.randint(1, maxday)
                hour = rng.randint(6, 20)
                login_dt = datetime(y, m, day, hour, rng.randint(0, 59))
                sampled_dt = login_dt - timedelta(hours=rng.randint(1, 10))
                pr_name, _, sla = rng.weighted(
                    PRIORITIES, [p[1] for p in PRIORITIES])
                due_dt = login_dt + timedelta(hours=sla)
                # turnaround: usually within SLA, sometimes late (~88% on-time)
                tat = max(0.5, rng.gauss(sla * 0.6, sla * 0.16))
                if rng.random() < 0.10:           # ~10% breach the SLA
                    tat = sla * rng.uniform(1.05, 1.9)
                completed_dt = login_dt + timedelta(hours=tat)
                on_time = 1 if completed_dt <= due_dt else 0
                # recent samples may still be open (live WIP backlog)
                age_days = (today - login_dt).days
                if age_days < 6 and rng.random() < 0.6:
                    status, status_label, result = "P", "In Progress", "Pending"
                    completed_dt = None
                elif rng.random() < 0.02:
                    status, status_label, result = "X", "Cancelled", "Cancelled"
                    completed_dt = None
                else:
                    status, status_label, result = "C", "Authorized", "Pass"
                group_name = rng.choice(groups)
                analyst = analyst_for(sec_code)
                text_seq[y] = text_seq.get(y, 0) + 1
                text_id = f"RFL-{y}-{text_seq[y]:06d}"
                sampling_point = f"{p['name'].split('(')[0].strip()[:24]} / {rng.choice(['Tank','Rundown','Blender','Jetty','Unit Outlet','Lab Spot'])}"

                # ---- test results for completed samples ----
                tests_total = 0
                tests_failed = 0
                if status == "C":
                    ptests = tests_by_product.get(pcode, [])
                    chosen = ptests if len(ptests) <= 6 else rng.sample(ptests, rng.randint(4, 6))
                    for t in chosen:
                        smin = t["spec_min"]
                        smax = t["spec_max"]
                        # Spec-aware centring: keep the authentic typical value but
                        # guarantee it sits inside the spec band, and scale the
                        # spread to the band, so natural product is in-spec and the
                        # deliberate excursions below drive a realistic ~5-8% fail.
                        mu = float(t["typical_mean"])
                        # sensible spread floor FIRST, then clamp it to the spec
                        # band so the band cap always has the final say.
                        base_sd = abs(float(t["typical_std"]) or abs(mu) * 0.05)
                        base_sd = max(base_sd, abs(mu) * 0.004, 1e-6)
                        sd = base_sd
                        if smin is not None and smax is not None and smax > smin:
                            band = smax - smin
                            mu = min(max(mu, smin + 0.2 * band), smax - 0.2 * band)
                            sd = max(min(base_sd, band / 6.0), band * 0.02)
                        elif smax is not None:               # one-sided upper limit
                            if mu > smax:
                                mu = smax - base_sd * 2
                            sd = min(base_sd, max(abs(smax - mu) / 3.0, 1e-6))
                        elif smin is not None:               # one-sided lower limit
                            if mu < smin:
                                mu = smin + base_sd * 2
                            sd = min(base_sd, max(abs(mu - smin) / 3.0, 1e-6))
                        sd = max(sd, 1e-9)
                        dev = rng.gauss(0, sd)
                        if rng.random() < 0.055:        # deliberate excursion
                            dev = (abs(dev) + sd) * rng.choice([-1, 1]) * rng.uniform(3.0, 5.0)
                        val = mu + dev
                        if smin is not None and smin >= 0:
                            val = max(0.0, val)
                        in_spec = 1
                        if smin is not None and val < smin:
                            in_spec = 0
                        if smax is not None and val > smax:
                            in_spec = 0
                        tests_total += 1
                        tests_failed += (1 - in_spec)
                        result_id += 1
                        rdate = completed_dt or login_dt
                        result_rows.append((
                            result_id, sample_no, text_id, pcode, sec_name,
                            t["test_name"], t["astm_method"], round(val, 4),
                            t["unit"], smin, smax, in_spec,
                            instrument_for(sec_code, t.get("instrument_type", "")),
                            analyst, rdate.strftime("%Y-%m-%d %H:%M:%S"),
                            rdate.strftime("%Y-%m"),
                        ))
                    if tests_failed > 0:
                        result = "Fail"

                sample_rows.append((
                    sample_no, text_id, pcode, p["name"], p["stream_type"],
                    group_name, sec_name, sampling_point, pr_name, status,
                    status_label, sampled_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    login_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    due_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    completed_dt.strftime("%Y-%m-%d %H:%M:%S") if completed_dt else None,
                    login_dt.strftime("%Y-%m"), analyst, round(tat, 1),
                    on_time if status == "C" else None, result,
                    tests_total, tests_failed,
                ))
                samples_index.append((text_id, sec_name, login_dt))

    cur.executemany(
        "INSERT INTO SAMPLE VALUES (%s)" % ",".join(["?"] * 22), sample_rows)
    cur.executemany(
        "INSERT INTO test_results VALUES (%s)" % ",".join(["?"] * 16), result_rows)

    # ---- Fact: INSTRUMENTS1_LOG (ON/OFF events) -------------------------
    log_rows = []
    log_id = 0
    for ins in instruments:
        asset = ins["asset_code"]
        sec = SECTION_NAME.get(canon(ins["section_code"]), "Other")
        uptime = float(ins["typical_uptime_pct"])
        downtime_fraction = max(0.01, (100.0 - uptime) / 100.0)
        # opening ON event
        cursor_dt = datetime(START_YEAR, START_MONTH, 1, 6, 0)
        log_id += 1
        log_rows.append((log_id, asset, "ON", rng.choice(TECHNICIANS),
                         cursor_dt.strftime("%Y-%m-%d %H:%M:%S"), "Startup", sec))
        for mi, (y, m) in enumerate(months):
            mdays = month_days(y, m)
            hours_in_month = mdays * 24
            base_dt = downtime_fraction * hours_in_month * seasonal(m, amp=0.18, phase=7)
            monthly_downtime = max(2.0, base_dt * rng.uniform(0.6, 1.5))
            monthly_downtime = min(monthly_downtime, 140.0)
            episodes = rng.randint(1, 4)
            # random episode day-times, sorted
            starts = sorted(rng.sample(range(1, max(2, mdays - 1)), episodes)) \
                if mdays > episodes + 1 else list(range(1, episodes + 1))
            portions = [rng.uniform(0.5, 1.5) for _ in range(episodes)]
            psum = sum(portions)
            for ei, day in enumerate(starts):
                dur_h = monthly_downtime * portions[ei] / psum
                off_dt = datetime(y, m, day, rng.randint(7, 18), rng.randint(0, 59))
                if off_dt <= cursor_dt:
                    off_dt = cursor_dt + timedelta(hours=rng.randint(2, 24))
                on_dt = off_dt + timedelta(hours=dur_h)
                reason = rng.weighted(
                    DOWNTIME_REASONS,
                    [0.30, 0.22, 0.20, 0.08, 0.08, 0.07, 0.05])
                tech = rng.choice(TECHNICIANS)
                log_id += 1
                log_rows.append((log_id, asset, "OFF", tech,
                                 off_dt.strftime("%Y-%m-%d %H:%M:%S"), reason, sec))
                log_id += 1
                log_rows.append((log_id, asset, "ON", tech,
                                 on_dt.strftime("%Y-%m-%d %H:%M:%S"), "Returned to service", sec))
                cursor_dt = on_dt
    cur.executemany(
        "INSERT INTO INSTRUMENTS1_LOG VALUES (?,?,?,?,?,?,?)", log_rows)

    # ---- Fact: calibrations ---------------------------------------------
    cal_rows = []
    cal_id = 0
    for ins in instruments:
        asset = ins["asset_code"]
        sec = SECTION_NAME.get(canon(ins["section_code"]), "Other")
        interval = int(ins["calibration_interval_days"])
        d = datetime(max(START_YEAR, int(ins["install_year"])), 1, 15)
        while d < today:
            nxt = d + timedelta(days=interval)
            overdue = 1 if nxt < today and (today - nxt).days < interval else 0
            cal_id += 1
            cal_rows.append((
                cal_id, asset, ins["name"], sec, d.strftime("%Y-%m-%d"),
                nxt.strftime("%Y-%m-%d"),
                rng.weighted(["Pass", "Pass", "Conditional"], [0.85, 0.1, 0.05]),
                rng.choice(TECHNICIANS),
                f"CAL-{asset}-{d.strftime('%Y%m')}",
                rng.choice(["CRM Std", "NIST-traceable", "ASTM RM", "Working Std"]),
                overdue if nxt < today else 0,
            ))
            d = nxt
    cur.executemany(
        "INSERT INTO calibrations VALUES (?,?,?,?,?,?,?,?,?,?,?)", cal_rows)

    # ---- Dimensions: INVENTORY_ITEM -------------------------------------
    inv_items = ref["inventory"]
    item_rows = []
    item_by_number = {}
    for i, it in enumerate(inv_items, 1):
        name = it["name"]
        code_up = it["item_code"].upper()
        # Match the bulk solvent items by code (MEOH/ETOH) so they normalise to
        # METHANOL / ETHANOL for the inventory forecast — without catching the
        # "Karl Fischer Methanol" *reagent*, which keeps its own stock code.
        if "MEOH" in code_up:
            stock = "METHANOL"
        elif "ETOH" in code_up:
            stock = "ETHANOL"
        else:
            stock = it["item_code"]
        reorder = float(it["reorder_level"])
        current = round(reorder * rng.uniform(0.4, 3.2), 1)
        below = 1 if current < reorder else 0
        item_rows.append((
            i, stock, it["item_code"], name, it["category"], it["unit"],
            reorder, float(it["unit_cost"]), it["supplier"], it["hazard_class"],
            current, rng.choice(["Solvent Store", "Reagent Cabinet",
                                 "Gas Bank", "Cold Store", "Main Store"]), below,
        ))
        item_by_number[it["item_code"]] = (i, stock, it)
    cur.executemany(
        "INSERT INTO INVENTORY_ITEM VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", item_rows)

    # ---- Fact: INVENTORY_TRANS ------------------------------------------
    trans_rows = []
    trans_id = 0
    # heavy-consumption items get full PULL history (incl. methanol/ethanol for
    # the inventory forecast); a sampling of others get lighter history.
    inv_months = [(y, m) for (y, m) in
                  [(yy, mm) for yy in range(2020, END_YEAR + 1) for mm in range(1, 13)]
                  if (2020, 1) <= (y, m) <= (END_YEAR, END_MONTH)]
    for idx, it in enumerate(inv_items, 1):
        i, stock, meta = item_by_number[it["item_code"]]
        monthly = float(it["typical_monthly_consumption"])
        is_key = stock in ("METHANOL", "ETHANOL")
        # only generate rich history for key stocks + ~16 other busy items
        if not is_key and idx % 3 != 0:
            continue
        per_month = 4 if is_key else rng.randint(1, 3)
        for mi, (y, m) in enumerate(inv_months):
            trend = 1.0 + 0.0035 * mi
            month_total = max(monthly * 0.3, monthly * trend * seasonal(m, amp=0.15) * rng.uniform(0.8, 1.2))
            mdays = month_days(y, m)
            days = sorted(rng.sample(range(1, min(27, mdays)), min(per_month, mdays - 1)))
            for day in days:
                trans_id += 1
                qty = max(0.5, round(month_total / per_month * rng.uniform(0.7, 1.3), 2))
                ts = datetime(y, m, day, rng.randint(8, 16), rng.randint(0, 59))
                trans_rows.append((
                    trans_id, "{" + str(uuid.uuid5(uuid.NAMESPACE_OID, f"RFL-TRANS-{trans_id}")).upper() + "}", i, stock,
                    "PULL", "C", ts.strftime("%Y-%m-%d %H:%M:%S"),
                    ts.strftime("%Y-%m"), qty, it["unit"], rng.choice(TECHNICIANS),
                    f"Issued to {rng.choice(FORECAST_SECTIONS)}", "",
                ))
            # occasional RECEIVE (restock)
            if rng.random() < 0.4:
                trans_id += 1
                ts = datetime(y, m, rng.randint(1, max(2, mdays - 1)), 10, 0)
                trans_rows.append((
                    trans_id, "{" + str(uuid.uuid5(uuid.NAMESPACE_OID, f"RFL-TRANS-{trans_id}")).upper() + "}", i, stock,
                    "RECEIVE", "C", ts.strftime("%Y-%m-%d %H:%M:%S"),
                    ts.strftime("%Y-%m"), round(month_total * rng.uniform(1.5, 3.0), 1),
                    it["unit"], "Stores", f"PO restock {meta['supplier']}",
                    f"PO-{y}{m:02d}-{rng.randint(1000,9999)}",
                ))
    cur.executemany(
        "INSERT INTO INVENTORY_TRANS VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", trans_rows)

    # ---- Fact: vision_inspections ---------------------------------------
    vis_rows = []
    vis_id = 0
    recent_samples = [s for s in samples_index if s[2] >= datetime(2025, 1, 1)]
    for uc in ref["vision_use_cases"]:
        cats = uc["categories"] or ["pass", "fail"]
        n = rng.randint(160, 360)
        pass_rate = rng.uniform(0.82, 0.95)
        model = f"refinery-vision-{uc['name'].split()[0].lower()}-v{rng.randint(1,3)}.{rng.randint(0,9)}"
        for _ in range(n):
            vis_id += 1
            link = rng.choice(recent_samples) if recent_samples else (None, "QA/QC & Calibration", today)
            ok = rng.random() < pass_rate
            conf = rng.uniform(0.7, 0.99) if ok else rng.uniform(0.45, 0.85)
            status = "Pass" if ok else rng.weighted(["Fail", "Review"], [0.6, 0.4])
            insp_dt = link[2] + timedelta(hours=rng.randint(1, 48))
            if insp_dt > today:
                insp_dt = today - timedelta(hours=rng.randint(1, 72))
            vis_rows.append((
                vis_id, f"IMG-{vis_id:06d}", uc["name"], link[0], link[1],
                rng.choice(cats), round(conf, 3), status,
                insp_dt.strftime("%Y-%m-%d %H:%M:%S"), insp_dt.strftime("%Y-%m"),
                model, rng.choice(TECHNICIANS),
                1 if (ok and conf > 0.8) else (0 if not ok else 1),
            ))
    cur.executemany(
        "INSERT INTO vision_inspections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", vis_rows)

    # ---- Dimensions: documents ------------------------------------------
    doc_rows = []
    for i, d in enumerate(ref["documents"], 1):
        c = canon(d["section_code"])
        eff = today - timedelta(days=rng.randint(60, 1400))
        review = eff + timedelta(days=730)
        doc_rows.append((
            i, d["doc_code"], d["title"], d["doc_type"], c,
            SECTION_NAME.get(c, "Cross-Section"), d["standard_ref"], d["version"],
            d["summary"], ", ".join(d["keywords"]), eff.strftime("%Y-%m-%d"),
            review.strftime("%Y-%m-%d"),
            "Active" if review > today else "Review Due",
        ))
    cur.executemany(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", doc_rows)

    conn.commit()

    counts = {}
    for t in ["sections", "analysts", "products", "SAMPLE", "test_results",
              "instruments", "INSTRUMENTS1_LOG", "calibrations",
              "INVENTORY_ITEM", "INVENTORY_TRANS", "vision_inspections",
              "documents"]:
        counts[t] = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()

    if verbose:
        print(f"Warehouse built: {WAREHOUSE}")
        for t, n in counts.items():
            print(f"  {t:20s} {n:>8,d}")
    return counts


if __name__ == "__main__":
    build()
