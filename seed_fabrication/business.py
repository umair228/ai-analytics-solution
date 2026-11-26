"""The business around the shop: sales, invoicing and cash, stock, maintenance, people.

Generated after the shop (build_warehouse) from the tables it wrote, each area with its
own random stream (seeded "<seed>:<area>"), so adding or changing an area never changes a
figure of the shop, and every number here agrees with the shop's: revenue is each
project's earned value at its contract price, invoices bill that revenue, stock is used as
the work centres are loaded, plate cut is the plate issued from stock, the welders are the
weld log's welders and their qualifications are checked against the welds they made.

The stories (invented, like everything here):

* sales: order intake has fallen behind revenue since the summer, so the order book is
  shrinking; we lose mostly on price, and bids priced above a 24% margin rarely win;
* cash: Client D (a utility) pays two months late as a rule and Client G has paid later
  every quarter of 2026; invoices on the two troubled projects are disputed;
* margin: the two troubled projects were bid at about 22% and will earn single figures;
* stock: stainless plate and stainless flanges run out when their two late suppliers slip;
  duplex plate bought for the project on hold has not moved since June; thick carbon
  plate was over-bought; plate yield improved after nesting software in September 2025;
* maintenance: the old plate rolls (FRM-02) break down more and more; planned maintenance
  slipped in the summer of 2026 and breakdowns followed; the mobile crane's lifting
  certificate has expired, and a crane, a compressor receiver and the test-pump gauges
  fall due within a month;
* people: welders work heavy overtime when the welding bay is overloaded; the yearly H2S
  course lapsed for many agency staff in the autumn; three welder qualifications lapsed
  and welds were made on them after they did.
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE calendar_months (month TEXT PRIMARY KEY, month_start TEXT, month_end TEXT);
CREATE TABLE revenue_monthly (
  project_no TEXT, month TEXT, revenue_sar REAL, cost_sar REAL, PRIMARY KEY (project_no, month));
CREATE TABLE business_plan (
  month TEXT PRIMARY KEY, revenue_plan_sar REAL, intake_plan_sar REAL, margin_plan_pct REAL,
  overhead_sar REAL);
CREATE TABLE invoices (
  invoice_no TEXT PRIMARY KEY, project_no TEXT, client TEXT, invoice_type TEXT, period TEXT,
  issued_on TEXT, due_on TEXT, paid_on TEXT, terms_days INTEGER, gross_sar REAL,
  retention_sar REAL, advance_recovery_sar REAL, amount_sar REAL, status TEXT);
CREATE TABLE cash_monthly (
  month TEXT PRIMARY KEY, cash_in_sar REAL, project_spend_sar REAL, overhead_sar REAL,
  closing_cash_sar REAL);
CREATE TABLE tenders (
  tender_no TEXT PRIMARY KEY, title TEXT, client TEXT, client_sector TEXT, product_type TEXT,
  estimator TEXT, received_on TEXT, submitted_on TEXT, decided_on TEXT, status TEXT,
  stage_reached INTEGER, value_sar REAL, margin_pct REAL, win_probability_pct INTEGER,
  loss_reason TEXT, price_gap_pct REAL, competitor TEXT, project_no TEXT, planned_start TEXT);
CREATE TABLE client_feedback (
  project_no TEXT PRIMARY KEY, client TEXT, surveyed_on TEXT, overall INTEGER, delivery INTEGER,
  quality INTEGER, communication INTEGER, would_recommend INTEGER);
CREATE TABLE stock_items (
  sku TEXT PRIMARY KEY, description TEXT, category TEXT, uom TEXT, unit_cost_sar REAL,
  main_supplier TEXT, lead_days INTEGER, weekly_use REAL, reorder_point REAL, safety_stock REAL,
  order_up_to REAL,
  on_hand_qty REAL, value_sar REAL, last_issued_on TEXT, last_received_on TEXT, reserved_for TEXT);
CREATE TABLE stock_monthly (
  sku TEXT, month TEXT, received_qty REAL, issued_qty REAL, closing_qty REAL,
  closing_value_sar REAL, issued_value_sar REAL, short_days INTEGER, PRIMARY KEY (sku, month));
CREATE TABLE stockouts (
  stockout_no TEXT PRIMARY KEY, sku TEXT, started_on TEXT, ended_on TEXT, days INTEGER,
  short_qty REAL);
CREATE TABLE plate_yield_monthly (
  month TEXT PRIMARY KEY, plate_cut_t REAL, scrap_t REAL, offcut_reused_t REAL, yield_pct REAL);
CREATE TABLE assets (
  asset_id TEXT PRIMARY KEY, asset TEXT, asset_class TEXT, work_center TEXT, criticality TEXT,
  installed_year INTEGER, certificate TEXT, last_certified_on TEXT, certificate_expires_on TEXT);
CREATE TABLE maintenance_orders (
  wo_no TEXT PRIMARY KEY, asset_id TEXT, wo_type TEXT, raised_on TEXT, due_on TEXT,
  completed_on TEXT, status TEXT, downtime_hours REAL, cost_sar REAL, failure_mode TEXT);
CREATE TABLE asset_monthly (
  asset_id TEXT, month TEXT, planned_hours REAL, downtime_hours REAL, performance_pct REAL,
  quality_pct REAL, PRIMARY KEY (asset_id, month));
CREATE TABLE people (
  person_id TEXT PRIMARY KEY, trade TEXT, employment TEXT, joined_on TEXT, left_on TEXT);
CREATE TABLE labour_monthly (
  month TEXT, trade TEXT, employment TEXT, headcount INTEGER, normal_hours REAL,
  overtime_hours REAL, absence_days REAL, joiners INTEGER, leavers INTEGER,
  PRIMARY KEY (month, trade, employment));
CREATE TABLE qualifications (
  qual_id TEXT PRIMARY KEY, person_id TEXT, trade TEXT, qualification TEXT, process TEXT,
  issued_on TEXT, expires_on TEXT);
CREATE TABLE training_records (
  record_id TEXT PRIMARY KEY, person_id TEXT, trade TEXT, employment TEXT, course TEXT,
  completed_on TEXT, expires_on TEXT);
"""

# The business rules the boards share, written once in the warehouse (as a client's
# reporting views would be), so a figure and the list behind it read the same definition.
# "Now" is data_info.data_date.
DD = "(SELECT value FROM data_info WHERE key = 'data_date')"
VIEWS = f"""
CREATE VIEW v_business_month AS
SELECT month, month_end, revenue_sar, cost_sar, plan_sar, margin_plan_pct, intake_sar,
       ROUND(100.0 * rev_ytd / plan_ytd, 1) AS revenue_vs_plan_pct,
       ROUND(100.0 * (rev_12m - cost_12m) / rev_12m, 1) AS margin_12m_pct,
       rev_12m AS revenue_12m_sar, receivables_sar, overdue_sar,
       ROUND(100.0 * overdue_sar / receivables_sar, 1) AS overdue_share_pct,
       CAST(ROUND(receivables_sar * 91.0 / rev_3m) AS INTEGER) AS dso_days,
       cash_sar, intake_12m_sar, ROUND(1.0 * intake_12m_sar / rev_12m, 2) AS book_to_bill,
       won_to_date - rev_to_date AS order_book_sar,
       ROUND((won_to_date - rev_to_date) / (rev_3m / 3.0), 1) AS months_of_work,
       ROUND(100.0 * won_12m / (won_12m + lost_12m), 1) AS win_rate_12m
FROM (SELECT c.month, c.month_end, b.revenue_plan_sar AS plan_sar, b.margin_plan_pct,
             (SELECT SUM(revenue_sar) FROM revenue_monthly r WHERE r.month = c.month) AS revenue_sar,
             (SELECT SUM(cost_sar) FROM revenue_monthly r WHERE r.month = c.month) AS cost_sar,
             (SELECT SUM(revenue_sar) FROM revenue_monthly r WHERE r.month BETWEEN substr(c.month, 1, 4) || '-01' AND c.month) AS rev_ytd,
             (SELECT SUM(revenue_plan_sar) FROM business_plan p WHERE p.month BETWEEN substr(c.month, 1, 4) || '-01' AND c.month) AS plan_ytd,
             (SELECT SUM(revenue_sar) FROM revenue_monthly r WHERE r.month > c.m12 AND r.month <= c.month) AS rev_12m,
             (SELECT SUM(cost_sar) FROM revenue_monthly r WHERE r.month > c.m12 AND r.month <= c.month) AS cost_12m,
             (SELECT SUM(revenue_sar) FROM revenue_monthly r WHERE r.month > c.m3 AND r.month <= c.month) AS rev_3m,
             (SELECT SUM(revenue_sar) FROM revenue_monthly r WHERE r.month <= c.month) AS rev_to_date,
             (SELECT ROUND(SUM(amount_sar)) FROM invoices i
               WHERE i.issued_on <= c.month_end AND (i.paid_on IS NULL OR i.paid_on > c.month_end)) AS receivables_sar,
             (SELECT COALESCE(ROUND(SUM(amount_sar)), 0) FROM invoices i
               WHERE i.issued_on <= c.month_end AND (i.paid_on IS NULL OR i.paid_on > c.month_end)
                 AND i.due_on < c.month_end) AS overdue_sar,
             (SELECT closing_cash_sar FROM cash_monthly k WHERE k.month = c.month) AS cash_sar,
             (SELECT COALESCE(SUM(value_sar), 0) FROM tenders t
               WHERE t.status = 'Won' AND substr(t.decided_on, 1, 7) = c.month) AS intake_sar,
             (SELECT COALESCE(SUM(value_sar), 0) FROM tenders t WHERE t.status = 'Won'
                 AND t.decided_on > date(c.month_end, '-12 months') AND t.decided_on <= c.month_end) AS intake_12m_sar,
             (SELECT SUM(value_sar) FROM tenders t WHERE t.status = 'Won' AND t.decided_on <= c.month_end) AS won_to_date,
             (SELECT COUNT(*) FROM tenders t WHERE t.status = 'Won'
                 AND t.decided_on > date(c.month_end, '-12 months') AND t.decided_on <= c.month_end) AS won_12m,
             (SELECT COUNT(*) FROM tenders t WHERE t.status = 'Lost'
                 AND t.decided_on > date(c.month_end, '-12 months') AND t.decided_on <= c.month_end) AS lost_12m
      FROM (SELECT month, month_end, substr(date(month || '-01', '-12 months'), 1, 7) AS m12,
                   substr(date(month || '-01', '-3 months'), 1, 7) AS m3
            FROM calendar_months) c
      JOIN business_plan b ON b.month = c.month);

CREATE VIEW v_invoices AS
SELECT x.*, CASE age_band WHEN 'Not due yet' THEN 1 WHEN '1-30 days overdue' THEN 2 WHEN '31-60 days overdue' THEN 3
                 WHEN '61-90 days overdue' THEN 4 WHEN 'over 90 days overdue' THEN 5 WHEN 'Disputed' THEN 6
                 ELSE 7 END AS band_order
FROM (SELECT i.*,
             CASE WHEN i.paid_on IS NOT NULL THEN CAST(julianday(i.paid_on) - julianday(i.due_on) AS INTEGER) END AS days_late_paid,
             CASE WHEN i.paid_on IS NULL AND i.due_on < d.dd THEN CAST(julianday(d.dd) - julianday(i.due_on) AS INTEGER) END AS days_overdue,
             CASE WHEN i.paid_on IS NOT NULL THEN 'Paid'
                  WHEN i.status = 'Disputed' THEN 'Disputed'
                  WHEN i.due_on >= d.dd THEN 'Not due yet'
                  WHEN julianday(d.dd) - julianday(i.due_on) <= 30 THEN '1-30 days overdue'
                  WHEN julianday(d.dd) - julianday(i.due_on) <= 60 THEN '31-60 days overdue'
                  WHEN julianday(d.dd) - julianday(i.due_on) <= 90 THEN '61-90 days overdue'
                  ELSE 'over 90 days overdue' END AS age_band,
             substr(i.issued_on, 1, 4) || ' Q' || ((CAST(substr(i.issued_on, 6, 2) AS INTEGER) + 2) / 3) AS issued_quarter,
             CASE WHEN i.paid_on IS NULL AND i.due_on < d.dd THEN 1 ELSE 0 END AS overdue_now
      FROM invoices i CROSS JOIN (SELECT value AS dd FROM data_info WHERE key = 'data_date') d) x;

CREATE VIEW v_tenders AS
SELECT t.*,
       CASE WHEN t.margin_pct < 20 THEN 'Below 20%' WHEN t.margin_pct < 24 THEN '20-24%' ELSE '24% and above' END AS margin_band,
       CASE WHEN t.margin_pct < 20 THEN 1 WHEN t.margin_pct < 24 THEN 2 ELSE 3 END AS margin_band_order,
       CASE WHEN t.status IN ('In preparation', 'Submitted', 'Negotiation') THEN 'Open'
            WHEN t.status IN ('Won', 'Lost') THEN t.status ELSE 'Not bid or withdrawn' END AS outcome,
       CASE WHEN t.status IN ('Submitted', 'Negotiation') THEN ROUND(t.value_sar * t.win_probability_pct / 100.0) END AS weighted_sar,
       CASE WHEN t.decided_on > date(d.dd, '-12 months') THEN 1 ELSE 0 END AS decided_last_12m,
       CASE WHEN t.status = 'Lost' AND t.decided_on > date(d.dd, '-12 months') THEN 1 ELSE 0 END AS lost_last_12m,
       CASE WHEN t.status IN ('Won', 'Lost') AND t.decided_on > date(d.dd, '-24 months') THEN 1 ELSE 0 END AS decided_bid_24m
FROM tenders t CROSS JOIN (SELECT value AS dd FROM data_info WHERE key = 'data_date') d;

CREATE VIEW v_stock_now AS
SELECT s.*,
       CASE stock_status WHEN 'Out of stock' THEN 1 WHEN 'Below safety stock' THEN 2 WHEN 'No movement 90+ days' THEN 3
            WHEN 'Excess (over 26 weeks)' THEN 4 ELSE 5 END AS status_order,
       CASE WHEN stock_status IN ('No movement 90+ days', 'Excess (over 26 weeks)') THEN 1 ELSE 0 END AS tied_up
FROM (SELECT u.*,
             CASE WHEN u.on_hand_qty < 0.01 THEN 'Out of stock'
                  WHEN u.on_hand_qty < u.safety_stock THEN 'Below safety stock'
                  WHEN u.days_since_issue IS NULL OR u.days_since_issue > 90 THEN 'No movement 90+ days'
                  WHEN u.weeks_of_cover > 26 THEN 'Excess (over 26 weeks)'
                  ELSE 'In range' END AS stock_status
      FROM (SELECT i.sku, i.description, i.category, i.uom, i.unit_cost_sar, i.main_supplier, i.lead_days,
                   i.reorder_point, i.safety_stock, i.on_hand_qty, i.value_sar, i.last_issued_on, i.last_received_on,
                   i.reserved_for,
                   ROUND(COALESCE(w.used, 0) / 13.0, 2) AS weekly_use_now,
                   CASE WHEN w.used > 0 THEN ROUND(i.on_hand_qty / (w.used / 13.0), 1) END AS weeks_of_cover,
                   CAST(julianday(d.dd) - julianday(i.last_issued_on) AS INTEGER) AS days_since_issue
            FROM stock_items i
            CROSS JOIN (SELECT value AS dd FROM data_info WHERE key = 'data_date') d
            LEFT JOIN (SELECT sku, SUM(issued_qty) AS used FROM stock_monthly
                       WHERE month >= substr(date({DD}, 'start of month', '-3 months'), 1, 7)
                         AND month < substr({DD}, 1, 7)
                       GROUP BY sku) w ON w.sku = i.sku) u) s;

CREATE VIEW v_maintenance AS
SELECT o.wo_no, o.asset_id, a.asset_id || ' · ' || a.asset AS asset, a.asset_class, a.work_center, a.criticality,
       o.wo_type, o.status, o.raised_on, o.due_on, o.completed_on, o.downtime_hours, o.cost_sar, o.failure_mode,
       CASE WHEN o.wo_type = 'Preventive' AND o.completed_on IS NOT NULL
            THEN CAST(julianday(o.completed_on) - julianday(o.due_on) AS INTEGER) END AS days_late,
       CASE WHEN julianday(o.raised_on) > julianday(d.dd) - 365 THEN 1 ELSE 0 END AS raised_last_12m,
       CASE WHEN substr(o.raised_on, 1, 7) >= substr(date(d.dd, 'start of month', '-3 months'), 1, 7)
             AND substr(o.raised_on, 1, 7) < substr(d.dd, 1, 7) THEN 1 ELSE 0 END AS raised_last_3_months
FROM maintenance_orders o JOIN assets a ON a.asset_id = o.asset_id
CROSS JOIN (SELECT value AS dd FROM data_info WHERE key = 'data_date') d;

CREATE VIEW v_maintenance_month AS
SELECT month, pm_due, pm_on_time, pm_done, breakdowns, downtime_hours,
       preventive_cost_sar, breakdown_cost_sar, inspection_cost_sar,
       ROUND(100.0 * pm_on_time / pm_due, 1) AS pm_on_time_pct,
       ROUND(100.0 * SUM(pm_on_time) OVER w / SUM(pm_due) OVER w, 1) AS pm_on_time_3m,
       ROUND(100.0 * pm_done / (pm_done + breakdowns), 1) AS planned_share_pct
FROM (SELECT c.month,
             (SELECT COUNT(*) FROM maintenance_orders o WHERE o.wo_type = 'Preventive' AND substr(o.due_on, 1, 7) = c.month) AS pm_due,
             (SELECT COUNT(*) FROM maintenance_orders o WHERE o.wo_type = 'Preventive' AND substr(o.due_on, 1, 7) = c.month
                 AND o.completed_on IS NOT NULL AND julianday(o.completed_on) - julianday(o.due_on) <= 7) AS pm_on_time,
             (SELECT COUNT(*) FROM maintenance_orders o WHERE o.wo_type = 'Preventive' AND substr(o.completed_on, 1, 7) = c.month) AS pm_done,
             (SELECT COUNT(*) FROM maintenance_orders o WHERE o.wo_type = 'Breakdown' AND substr(o.raised_on, 1, 7) = c.month) AS breakdowns,
             (SELECT COALESCE(SUM(downtime_hours), 0) FROM maintenance_orders o WHERE o.wo_type = 'Breakdown' AND substr(o.raised_on, 1, 7) = c.month) AS downtime_hours,
             (SELECT COALESCE(SUM(cost_sar), 0) FROM maintenance_orders o WHERE o.wo_type = 'Preventive' AND substr(o.completed_on, 1, 7) = c.month) AS preventive_cost_sar,
             (SELECT COALESCE(SUM(cost_sar), 0) FROM maintenance_orders o WHERE o.wo_type = 'Breakdown' AND substr(o.raised_on, 1, 7) = c.month) AS breakdown_cost_sar,
             (SELECT COALESCE(SUM(cost_sar), 0) FROM maintenance_orders o WHERE o.wo_type = 'Statutory inspection' AND substr(o.completed_on, 1, 7) = c.month) AS inspection_cost_sar
      FROM calendar_months c
      WHERE c.month >= '2025-01' AND c.month < substr({DD}, 1, 7))
WINDOW w AS (ORDER BY month ROWS 2 PRECEDING);

CREATE VIEW v_labour_month AS
SELECT month, headcount, own_staff, agency, overtime_pct, welder_overtime_pct, absence_pct, leavers, own_leavers,
       ROUND(100.0 * SUM(own_leavers) OVER w / AVG(own_staff) OVER w, 1) AS attrition_12m_pct
FROM (SELECT month, SUM(headcount) AS headcount,
             SUM(CASE WHEN employment = 'Own staff' THEN headcount ELSE 0 END) AS own_staff,
             SUM(CASE WHEN employment = 'Agency' THEN headcount ELSE 0 END) AS agency,
             ROUND(100.0 * SUM(overtime_hours) / SUM(normal_hours), 1) AS overtime_pct,
             ROUND(100.0 * SUM(CASE WHEN trade = 'Welder' THEN overtime_hours END)
                   / SUM(CASE WHEN trade = 'Welder' THEN normal_hours END), 1) AS welder_overtime_pct,
             ROUND(100.0 * SUM(absence_days) / (SUM(headcount) * 22.0), 1) AS absence_pct,
             SUM(leavers) AS leavers, SUM(CASE WHEN employment = 'Own staff' THEN leavers ELSE 0 END) AS own_leavers
      FROM labour_monthly WHERE month < substr({DD}, 1, 7) GROUP BY month)
WINDOW w AS (ORDER BY month ROWS 11 PRECEDING);

CREATE VIEW v_certificates AS
SELECT x.*,
       CAST(julianday(x.expires_on) - julianday(d.dd) AS INTEGER) AS days_left,
       CASE WHEN x.expires_on IS NULL THEN 'Not done'
            WHEN x.expires_on < d.dd THEN 'Lapsed'
            WHEN julianday(x.expires_on) - julianday(d.dd) <= 30 THEN 'Within 30 days'
            WHEN julianday(x.expires_on) - julianday(d.dd) <= 90 THEN '31-90 days'
            ELSE 'Later' END AS expiry_band,
       CASE WHEN x.expires_on IS NULL THEN 1 WHEN x.expires_on < d.dd THEN 2
            WHEN julianday(x.expires_on) - julianday(d.dd) <= 30 THEN 3
            WHEN julianday(x.expires_on) - julianday(d.dd) <= 90 THEN 4 ELSE 5 END AS band_order,
       CASE WHEN x.expires_on >= d.dd THEN 1 ELSE 0 END AS up_to_date
FROM (SELECT q.qual_id AS record_id, 'Qualification' AS kind, q.person_id, q.trade, p.employment,
             q.qualification AS name, q.process, q.issued_on AS done_on, q.expires_on
      FROM qualifications q JOIN people p ON p.person_id = q.person_id
      UNION ALL
      SELECT t.record_id, 'Training', t.person_id, t.trade, t.employment, t.course, NULL, t.completed_on, t.expires_on
      FROM training_records t) x
CROSS JOIN (SELECT value AS dd FROM data_info WHERE key = 'data_date') d;
"""

TABLES = ("calendar_months", "revenue_monthly", "business_plan", "invoices", "cash_monthly", "tenders",
          "client_feedback", "stock_items", "stock_monthly", "stockouts", "plate_yield_monthly",
          "assets", "maintenance_orders", "asset_monthly", "people", "labour_monthly",
          "qualifications", "training_records")


# ──────────────────────────────────────────────────────────────────────────────
#  Dates
# ──────────────────────────────────────────────────────────────────────────────

def _d(s):
    return date.fromisoformat(s) if s else None


def iso(d):
    return d.isoformat() if d else None


def ym(d):
    return d.strftime("%Y-%m")


def next_month(d):
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1)


def month_end(d):
    return next_month(d) - timedelta(days=1)


def months(a, b):
    m = a.replace(day=1)
    while m <= b:
        yield m
        m = next_month(m)


def monday(d):
    return d - timedelta(days=d.weekday())


def weighted(rng, pairs):
    x, acc = rng.random(), 0.0
    for value, w in pairs:
        acc += w
        if x <= acc:
            return value
    return pairs[-1][0]


# Ramadan (shorter working days) and the summer holidays, for hours and absence.
RAMADAN = {"2025-03", "2026-03"}
SUMMER = {"2025-07", "2025-08", "2026-07", "2026-08"}


def _load_ratio(con):
    """(work centre, 'YYYY-MM') -> load as a share of capacity, averaged over the weeks."""
    out = {}
    for wc, m, load, cap in con.execute(
            "SELECT work_center, substr(week_start, 1, 7), SUM(load_hours), SUM(capacity_hours) "
            "FROM wc_weekly GROUP BY 1, 2"):
        out[(wc, m)] = (load or 0) / cap if cap else 0
    return out


def _projects(con):
    cols = ("project_no", "project_name", "client", "client_sector", "product_type",
            "contract_value_sar", "budget_cost_sar", "start_date", "forecast_finish", "status",
            "cpi", "spi")
    out = {}
    for r in con.execute(f"SELECT {', '.join(cols)} FROM projects ORDER BY project_no"):
        p = dict(zip(cols, r))
        p["start"], p["finish"] = _d(p["start_date"]), _d(p["forecast_finish"])
        out[p["project_no"]] = p
    return out


# ──────────────────────────────────────────────────────────────────────────────
#  Finance: revenue, plan, invoices, cash
# ──────────────────────────────────────────────────────────────────────────────

TERMS = {"Client A": 45, "Client B": 60, "Client C": 30, "Client D": 90, "Client E": 45,
         "Client F": 60, "Client G": 60, "Client H": 60, "Client I": 60}
SEASON = {1: 1.0, 2: 1.0, 3: 0.85, 4: 1.0, 5: 1.05, 6: 0.92, 7: 0.85, 8: 0.9, 9: 1.05,
          10: 1.1, 11: 1.1, 12: 1.15}


def _pay_delay(rng, client, issued):
    """Days after the due date the client pays (negative: early)."""
    if client == "Client C":
        return rng.gauss(-4, 3)
    if client == "Client D":                                       # a utility: two months late
        return 38 + rng.lognormvariate(3.0, 0.45)
    if client == "Client G":                                       # later every quarter of 2026
        return 3 + max(0, (issued - date(2026, 1, 1)).days) / 300 * 55 + rng.gauss(0, 6)
    mu, sd = {"Client A": (4, 6), "Client B": (9, 9), "Client E": (5, 6), "Client F": (14, 10),
              "Client H": (16, 12)}.get(client, (8, 8))
    return rng.gauss(mu, sd)


def _finance(con, data_date, seed):
    rng = random.Random(f"{seed}:finance")
    projects = _projects(con)
    # earned value and actual cost at each month end (the last week of the month)
    last = {}
    for p, wk, ev, ac in con.execute("SELECT project_no, week_start, ev_sar, ac_sar FROM evm_weekly "
                                     "ORDER BY project_no, week_start"):
        last[(p, wk[:7])] = (ev, ac)
    revenue = {}                                                     # (project, month) -> (rev, cost)
    for pno, p in projects.items():
        prev_ev = prev_ac = 0.0
        for m in sorted(k[1] for k in last if k[0] == pno):
            ev, ac = last[(pno, m)]
            ev, ac = max(ev, prev_ev), max(ac, prev_ac)              # cumulative: never falls
            rev = (ev - prev_ev) * p["contract_value_sar"] / p["budget_cost_sar"]
            cost = ac - prev_ac
            prev_ev, prev_ac = ev, ac
            if rev >= 1 or cost >= 1:
                revenue[(pno, m)] = (round(rev), round(cost))
    con.executemany("INSERT INTO revenue_monthly VALUES (?,?,?,?)",
                    [(p, m, r, c) for (p, m), (r, c) in sorted(revenue.items())])

    # ---- the business plan: 2025 was planned a little above what it delivered; 2026 was
    # planned for growth the year has not reached (about 94% so far)
    total = {}
    for (_, m), (r, _) in revenue.items():
        total[m] = total.get(m, 0) + r
    cur = ym(data_date)
    a2025 = sum(v for m, v in total.items() if m.startswith("2025"))
    w = {m: SEASON[int(m[5:])] for m in (f"{y}-{k:02d}" for y in (2025, 2026) for k in range(1, 13))}
    closed_2026 = [m for m in w if m.startswith("2026") and m < cur]
    a2026 = sum(total.get(m, 0) for m in closed_2026)
    plan2026 = a2026 / 0.94 / sum(w[m] for m in closed_2026) * sum(w[m] for m in w if m.startswith("2026"))
    plan_rows = []
    for m, wt in w.items():
        yearly = a2025 * 1.03 if m.startswith("2025") else plan2026
        rev_plan = round(yearly * wt / sum(v for k, v in w.items() if k[:4] == m[:4]), -5)
        plan_rows.append((m, rev_plan, round(rev_plan * (1.1 if m.startswith("2025") else 1.05), -5),
                          21.0 if m.startswith("2025") else 22.0,
                          round((2.1e6 if m.startswith("2025") else 2.35e6) * rng.uniform(0.96, 1.04), -3)))
    con.executemany("INSERT INTO business_plan VALUES (?,?,?,?,?)", plan_rows)
    overhead = {r[0]: r[4] for r in plan_rows}

    # ---- invoices: an advance, a progress invoice each month for the revenue earned, the
    # retention released after completion
    invoices = []
    for pno, p in projects.items():
        client, terms = p["client"], TERMS.get(p["client"], 60)
        troubled = p["cpi"] < 0.9
        advance = round(p["contract_value_sar"] * 0.10, -2)
        left, retained = advance, 0.0
        drafts = []
        issued = p["start"] + timedelta(days=rng.randint(7, 14))
        drafts.append(("Advance payment", ym(p["start"]), issued, advance, 0.0, 0.0, False, 0.5))
        for (pp, m), (rev, _) in sorted(revenue.items()):
            if pp != pno or rev <= 0 or m >= cur:
                continue
            issued = next_month(date.fromisoformat(m + "-01")) + timedelta(days=rng.randint(4, 11))
            ret = round(rev * 0.05, -1)
            rec = round(min(rev * 0.10, left), -1)
            left -= rec
            retained += ret
            disputed = troubled and m >= "2026-08" and rng.random() < 0.45
            drafts.append(("Progress", m, issued, float(rev), ret, rec, disputed, 1.0))
        if p["status"] == "Completed" and retained:
            drafts.append(("Retention release", ym(p["finish"]), p["finish"] + timedelta(days=60),
                           retained, 0.0, 0.0, False, 1.4))
        for kind, period, issued, gross, ret, rec, disputed, slow in drafts:
            if issued > data_date:
                continue
            due = issued + timedelta(days=terms)
            paid = None
            if not disputed:
                paid = due + timedelta(days=round(_pay_delay(rng, client, issued) * slow))
                paid = max(paid, issued + timedelta(days=5))
                if paid > data_date:
                    paid = None
            status = ("Paid" if paid else "Disputed" if disputed
                      else "Overdue" if due < data_date else "Not due")
            invoices.append([pno, client, kind, period, issued, due, paid, terms, gross, ret, rec,
                             round(gross - ret - rec, 2), status])
    invoices.sort(key=lambda r: (r[4], r[0], r[2]))
    seq, out = {}, []
    for r in invoices:
        yy = r[4].strftime("%y")
        seq[yy] = seq.get(yy, 0) + 1
        out.append((f"INV-{yy}-{seq[yy]:04d}", r[0], r[1], r[2], r[3], iso(r[4]), iso(r[5]), iso(r[6]),
                    *r[7:]))
    con.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)

    # ---- cash: money in when invoices are paid; out as the work costs (labour in the
    # month, materials the month after) plus the overheads
    cost_by_month = {}
    for (_, m), (_, c) in revenue.items():
        cost_by_month[m] = cost_by_month.get(m, 0) + c
    paid_in = {}
    for r in out:
        if r[7]:
            paid_in[r[7][:7]] = paid_in.get(r[7][:7], 0) + r[12]
    # opening balance: the company did not start with the shop; it carried cash and a
    # facility into 2025 for the working capital a growing order book needs
    cash, rows, prev_m = 110e6, [], None
    for m in months(date(2025, 1, 1), data_date):
        k = ym(m)
        spend = 0.4 * cost_by_month.get(k, 0) + 0.6 * cost_by_month.get(prev_m, 0)
        oh = overhead.get(k, 2.35e6) * (data_date.day / month_end(m).day if k == cur else 1)
        cash += paid_in.get(k, 0) - spend - oh
        rows.append((k, round(paid_in.get(k, 0)), round(spend), round(oh), round(cash)))
        prev_m = k
    con.executemany("INSERT INTO cash_monthly VALUES (?,?,?,?,?)", rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Sales: tenders and client feedback
# ──────────────────────────────────────────────────────────────────────────────

SECTORS = {"Client A": "Refining", "Client B": "Petrochemicals", "Client C": "Gas processing",
           "Client D": "Utilities", "Client E": "Refining", "Client F": "Mining",
           "Client G": "Petrochemicals", "Client H": "Water", "Client I": "Petrochemicals",
           "Client J": "Gas processing", "Client K": "Refining", "Client L": "Utilities"}
PRODUCTS = [("Pressure vessel", 0.22), ("Heat exchanger", 0.18), ("Process skid", 0.18),
            ("Storage tank", 0.14), ("Piping spools", 0.16), ("Column / tower", 0.12)]
LOSS_REASONS = [("Price", 0.55), ("Delivery time", 0.17), ("Technical compliance", 0.11),
                ("Incumbent supplier", 0.09), ("Commercial terms", 0.08)]
COMPETITORS = [("Competitor 1", 0.42), ("Competitor 2", 0.24), ("Competitor 3", 0.13),
               ("Competitor 4", 0.10), ("Competitor 5", 0.07), ("Competitor 6", 0.04)]
ESTIMATORS = ["EST-01", "EST-02", "EST-03", "EST-04", "EST-05"]


def _sales(con, data_date, seed):
    rng = random.Random(f"{seed}:sales")
    projects = _projects(con)
    rows = []
    # every project was a tender we won
    for pno, p in projects.items():
        decided = p["start"] - timedelta(days=rng.randint(20, 45))
        submitted = decided - timedelta(days=rng.randint(25, 70))
        received = submitted - timedelta(days=rng.randint(20, 45))
        margin = round(100 * (p["contract_value_sar"] - p["budget_cost_sar"]) / p["contract_value_sar"], 1)
        rows.append(dict(title=f"{p['project_name']} ({p['product_type'].lower()})", client=p["client"],
                         product=p["product_type"], received=received, submitted=submitted,
                         decided=decided, status="Won", stage=4, value=p["contract_value_sar"],
                         margin=margin, project=pno, start=None))
    # won since the summer, not started yet: the order book's newest work
    for client, product, title, value, margin, decided, start in (
            ("Client A", "Pressure vessel", "Separator package", 18.4e6, 19.5, date(2026, 10, 14), date(2027, 1, 10)),
            ("Client I", "Process skid", "Chemical injection skids", 26.2e6, 20.8, date(2026, 11, 9), date(2027, 2, 14))):
        submitted = decided - timedelta(days=rng.randint(30, 60))
        rows.append(dict(title=f"{title} ({product.lower()})", client=client, product=product,
                         received=submitted - timedelta(days=rng.randint(20, 40)), submitted=submitted,
                         decided=decided, status="Won", stage=4, value=value, margin=margin,
                         project=None, start=start))
    clients = [(c, 0.7 / 8) for c in list(SECTORS)[:8]] + [(c, 0.3 / 4) for c in list(SECTORS)[8:]]
    names = {"Pressure vessel": ["Separator", "KO drum", "Flash drum", "Accumulator"],
             "Heat exchanger": ["Shell & tube exchangers", "Air coolers", "Reboilers"],
             "Process skid": ["Metering skid", "Pump skid", "Dosing skid"],
             "Storage tank": ["Storage tanks", "Fire-water tanks"],
             "Piping spools": ["Piping spools", "Process piping package"],
             "Column / tower": ["Stripper column", "Contactor tower"]}
    day = date(2024, 9, 2)
    while day <= data_date:
        received = day + timedelta(days=rng.randint(0, 6))
        day += timedelta(days=rng.choice((4, 5, 6, 7, 8)))
        if received > data_date:
            break
        client = weighted(rng, clients)
        product = weighted(rng, PRODUCTS)
        value = round(min(90e6, max(3e6, rng.lognormvariate(math.log(20e6), 0.7))), -4)
        # bids have been priced higher in 2026: margins of 21-31% instead of 18-27%
        margin = round(rng.uniform(18, 27) if received.year < 2026 else rng.uniform(21, 31), 1)
        submitted = received + timedelta(days=rng.randint(21, 50))
        decided = submitted + timedelta(days=int(rng.lognormvariate(4.1, 0.45)))
        r = dict(title=f"{rng.choice(names[product])} ({product.lower()})", client=client,
                 product=product, received=received, submitted=submitted, decided=decided,
                 value=value, margin=margin, project=None, start=None)
        x = rng.random()
        if x < 0.12:
            r.update(status="Declined to bid", stage=1, submitted=None, decided=received + timedelta(days=7),
                     reason="Out of scope or no capacity")
        elif submitted > data_date:
            r.update(status="In preparation", stage=1, submitted=None, decided=None)
        elif decided > data_date:
            negotiating = (decided - data_date).days < 25 and rng.random() < 0.45
            r.update(status="Negotiation" if negotiating else "Submitted", stage=3 if negotiating else 2,
                     decided=None, prob=rng.choice((50, 60, 70) if negotiating else (20, 30, 40, 50)))
        elif x < 0.20:
            r.update(status="Withdrawn by client", stage=2, reason="Project cancelled or postponed")
        else:
            reason = weighted(rng, LOSS_REASONS)
            r.update(status="Lost", stage=3 if rng.random() < 0.4 else 2, reason=reason,
                     competitor=weighted(rng, COMPETITORS),
                     gap=round(rng.lognormvariate(1.9, 0.5), 1) if reason == "Price" else None)
        rows.append(r)
    rows.sort(key=lambda r: (r["received"], r["title"]))
    out, seq = [], {}
    for r in rows:
        yy = r["received"].strftime("%y")
        seq[yy] = seq.get(yy, 0) + 1
        out.append((f"T-{yy}-{seq[yy]:03d}", r["title"], r["client"], SECTORS[r["client"]], r["product"],
                    rng.choice(ESTIMATORS), iso(r["received"]), iso(r["submitted"]), iso(r["decided"]),
                    r["status"], r["stage"], r["value"], r["margin"], r.get("prob"), r.get("reason"),
                    r.get("gap"), r.get("competitor"), r["project"], iso(r["start"])))
    con.executemany("INSERT INTO tenders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)

    # ---- what clients said at handover: late and troubled projects score lower
    ncr = dict(con.execute("SELECT project_no, COUNT(*) FROM ncrs GROUP BY project_no").fetchall())
    fb = []
    for pno, p in projects.items():
        if p["status"] != "Completed":
            continue
        surveyed = p["finish"] + timedelta(days=rng.randint(20, 50))
        if surveyed > data_date:
            continue
        clamp = lambda v: max(1, min(10, round(v)))                  # noqa: E731
        delivery = clamp(9.0 - 30 * max(0.0, 1 - p["spi"]) + rng.gauss(0, 0.6))
        quality = clamp(9.6 - 0.1 * ncr.get(pno, 0) + rng.gauss(0, 0.6))
        comms = clamp(rng.gauss(8.0, 0.8))
        overall = clamp((delivery + quality + comms) / 3 + rng.gauss(0, 0.4))
        fb.append((pno, p["client"], iso(surveyed), overall, delivery, quality, comms, int(overall >= 8)))
    con.executemany("INSERT INTO client_feedback VALUES (?,?,?,?,?,?,?,?)", fb)


# ──────────────────────────────────────────────────────────────────────────────
#  Stock and materials
# ──────────────────────────────────────────────────────────────────────────────

# sku, description, category, uom, unit cost, weekly use, lead days, work centre that uses
# it, supplier (named as in the purchase orders), weeks of stock ordered above the reorder point
CS = "Plate - carbon steel"
SS = "Plate - stainless & duplex"
STOCK = [
    *[(f"PL-CS-{t:02d}", f"Plate SA-516-70, {t} mm", CS, "t", 4300 + 12 * t, use, 42, "Cutting bay",
       "Plates supplier 01", over) for t, use, over in
      ((8, 7.0, 10), (10, 12.0, 10), (12, 15.0, 10), (16, 15.0, 10), (20, 12.0, 10), (25, 10.0, 10),
       (32, 7.0, 10), (40, 3.0, 40), (50, 2.0, 46))],
    *[(f"PL-SS-{t:02d}", f"Plate SS 316L, {t} mm", SS, "t", 19500, use, 70, "Cutting bay",
       "Plates supplier 02", 8) for t, use in ((6, 1.2), (10, 1.6), (16, 1.0))],
    *[(f"PL-DX-{t:02d}", f"Plate duplex 2205, {t} mm", SS, "t", 31000, 0.0, 84, "Cutting bay",
       "Plates supplier 03", 0) for t in (12, 20)],
    *[(f"PP-CS-{n:02d}", f'Pipe A106-B, {n}" sch 40', "Pipe & fittings", "m", cost, use, 35, "Fit-up",
       "Pipes supplier 01", 6) for n, cost, use in ((2, 55, 180), (4, 120, 140), (6, 210, 90), (8, 330, 50),
                                                     (12, 620, 25))],
    *[(f"PP-SS-{n:02d}", f'Pipe SS 316L, {n}" sch 10S', "Pipe & fittings", "m", cost, use, 56, "Fit-up",
       "Pipes supplier 02", 6) for n, cost, use in ((2, 260, 40), (4, 520, 25))],
    ("FT-CS-01", "Elbows and tees, carbon steel (assorted)", "Pipe & fittings", "each", 160, 220, 28,
     "Fit-up", "Pipes supplier 01", 6),
    ("FT-SS-01", "Elbows and tees, SS 316L (assorted)", "Pipe & fittings", "each", 640, 40, 49, "Fit-up",
     "Pipes supplier 02", 6),
    ("FL-CS-15", "Flanges WN, carbon steel, class 150", "Flanges & bolting", "each", 380, 70, 42, "Fit-up",
     "Forgings supplier 02", 6),
    ("FL-CS-30", "Flanges WN, carbon steel, class 300", "Flanges & bolting", "each", 610, 40, 49, "Fit-up",
     "Forgings supplier 02", 6),
    ("FL-SS-15", "Flanges WN, SS 316L, class 150", "Flanges & bolting", "each", 1450, 12, 63, "Fit-up",
     "Forgings supplier 01", 6),
    ("BT-B7-01", "Stud bolts B7/2H (sets)", "Flanges & bolting", "set", 95, 160, 21, "Testing",
     "Forgings supplier 02", 6),
    ("GK-SW-01", "Spiral-wound gaskets (assorted)", "Flanges & bolting", "each", 75, 140, 21, "Testing",
     "Forgings supplier 02", 6),
    ("WC-7018-32", "Electrodes E7018, 3.2 mm", "Welding consumables", "kg", 18, 650, 14, "Welding bay",
     "Consumables supplier 01", 5),
    ("WC-7018-40", "Electrodes E7018, 4.0 mm", "Welding consumables", "kg", 18, 480, 14, "Welding bay",
     "Consumables supplier 01", 5),
    ("WC-ER70-12", "Wire ER70S-6, 1.2 mm", "Welding consumables", "kg", 11, 900, 14, "Welding bay",
     "Consumables supplier 01", 5),
    ("WC-E71T-12", "Flux-cored wire E71T-1, 1.2 mm", "Welding consumables", "kg", 16, 700, 14,
     "Welding bay", "Consumables supplier 02", 5),
    ("WC-EM12-40", "SAW wire EM12K, 4.0 mm", "Welding consumables", "kg", 9, 1100, 21, "Welding bay",
     "Consumables supplier 02", 5),
    ("WC-FLUX-01", "SAW flux (agglomerated)", "Welding consumables", "kg", 7, 1200, 21, "Welding bay",
     "Consumables supplier 02", 5),
    ("WC-316L-24", "TIG rod ER316L, 2.4 mm", "Welding consumables", "kg", 95, 60, 35, "Welding bay",
     "Consumables supplier 03", 6),
    ("WC-E316-32", "Electrodes E316L-16, 3.2 mm", "Welding consumables", "kg", 85, 45, 35, "Welding bay",
     "Consumables supplier 03", 6),
    ("GS-AR-01", "Argon (cylinder)", "Gases", "cyl", 140, 120, 7, "Welding bay", "Consumables supplier 04", 3),
    ("GS-MIX-01", "Argon/CO2 mix (cylinder)", "Gases", "cyl", 160, 90, 7, "Welding bay",
     "Consumables supplier 04", 3),
    ("GS-O2-01", "Oxygen (cylinder)", "Gases", "cyl", 60, 70, 7, "Cutting bay", "Consumables supplier 04", 3),
    ("GS-LPG-01", "Propane (cylinder)", "Gases", "cyl", 90, 40, 7, "Heat treatment",
     "Consumables supplier 04", 3),
    ("PT-ZN-01", "Epoxy zinc-rich primer", "Paint & abrasive", "L", 62, 900, 21, "Blast & paint",
     "Paint supplier 01", 6),
    ("PT-MIO-01", "Epoxy MIO intermediate", "Paint & abrasive", "L", 48, 700, 21, "Blast & paint",
     "Paint supplier 01", 6),
    ("PT-PU-01", "Polyurethane topcoat", "Paint & abrasive", "L", 70, 650, 21, "Blast & paint",
     "Paint supplier 02", 6),
    ("PT-TH-01", "Thinner", "Paint & abrasive", "L", 14, 300, 14, "Blast & paint", "Paint supplier 02", 6),
    ("AB-G40-01", "Steel grit G40", "Paint & abrasive", "t", 3200, 3.5, 28, "Blast & paint",
     "Paint supplier 01", 6),
]
LATE_STOCK_SUPPLIERS = {"Plates supplier 02", "Forgings supplier 01"}   # the purchase orders' late two
DUPLEX_FOR = "F-2621"                                                   # the project on hold


def _stock(con, data_date, seed):
    rng = random.Random(f"{seed}:stock")
    weekly_load = {}
    for wc, wk, load in con.execute("SELECT work_center, week_start, load_hours FROM wc_weekly"):
        weekly_load[(wc, wk)] = load or 0
    mean = {}
    for (wc, wk), v in weekly_load.items():
        if wk <= iso(data_date):
            mean.setdefault(wc, []).append(v)
    mean = {wc: sum(v) / len(v) for wc, v in mean.items()}
    start, end = date(2025, 1, 6), monday(data_date)
    items, monthly, events = [], [], []
    for sku, desc, cat, uom, cost, use, lead, wc, supplier, over in STOCK:
        duplex = sku.startswith("PL-DX")
        # the store plans on the shop's loading over the lead time (as an MRP run does), so
        # stock follows the work: it runs out when a supplier is late, not because the shop
        # got busy
        rop = up_to = rate = 0.0
        ahead = math.ceil(lead / 7) + 1
        on_hand = round(use * (lead / 7 * 1.3 + over) * rng.uniform(0.35, 0.6), 2) if not duplex else rng.uniform(0.8, 1.6)
        orders = []                                                  # [arrival, qty]
        if duplex:   # bought for the project on hold, delivered just before it stopped
            orders.append([date(2026, 5, 25), 24.0 if sku.endswith("12") else 19.0])
        last_issue = last_receipt = None
        month = {}
        out_since, short_total = None, 0.0
        wk = start
        while wk <= end:
            days = min(7, (data_date - wk).days + 1)
            for o in [o for o in orders if o[0] < wk + timedelta(days=days)]:
                on_hand += o[1]
                last_receipt = o[0]
                orders.remove(o)
                m = month.setdefault(ym(o[0]), [0.0, 0.0, 0.0, 0])
                m[0] += o[1]
            if duplex:
                demand = rng.uniform(2.0, 3.5) if date(2026, 5, 25) <= wk <= date(2026, 6, 8) else 0.0
            else:
                idx = weekly_load.get((wc, iso(wk)), 0) / mean.get(wc, 1)
                demand = use * idx * rng.uniform(0.6, 1.4) * days / 7
            issue = min(on_hand, demand)
            on_hand -= issue
            if issue > 0:
                last_issue = wk + timedelta(days=rng.randint(0, max(0, days - 1)))
            m = month.setdefault(ym(wk), [0.0, 0.0, 0.0, 0])
            m[1] += issue
            short = demand - issue
            if short > 0.03 * use * days / 7 and use > 0:
                ran_out = wk + timedelta(days=int(days * issue / demand))
                out_since = out_since or ran_out
                short_total += short
                m[3] += (wk + timedelta(days=days) - max(ran_out, wk)).days
            elif out_since:
                events.append((sku, out_since, last_receipt, short_total))
                out_since, short_total = None, 0.0
            fwd = sum(weekly_load.get((wc, iso(wk + timedelta(weeks=j))), 0) for j in range(ahead)) / ahead
            rate = max(use * fwd / mean.get(wc, 1), 0.6 * use)
            rop, up_to = rate * lead / 7 * 1.3, rate * (lead / 7 * 1.3 + over)
            if not duplex and on_hand + sum(o[1] for o in orders) <= rop:
                qty = up_to - on_hand - sum(o[1] for o in orders)
                days_lead = lead * rng.uniform(0.85, 1.2)
                if supplier in LATE_STOCK_SUPPLIERS and wk >= date(2026, 1, 1) and rng.random() < 0.85:
                    days_lead += rng.lognormvariate(3.3, 0.35)                 # about four weeks late
                orders.append([wk + timedelta(days=round(days_lead)), round(qty, 2)])
            m[2] = on_hand
            wk += timedelta(days=7)
        if out_since:
            events.append((sku, out_since, None, short_total))
        for k in sorted(month):
            rec, iss, close, sd = month[k]
            monthly.append((sku, k, round(rec, 2), round(iss, 2), round(close, 2), round(close * cost),
                            round(iss * cost), sd))
        # safety stock: the part of the reorder point kept for a late delivery or a busy week
        items.append((sku, desc, cat, uom, cost, supplier, lead, use, round(rop, 2),
                      round(rate * lead / 7 * 0.3, 2), round(up_to, 2),
                      round(on_hand, 2), round(on_hand * cost), iso(last_issue), iso(last_receipt),
                      DUPLEX_FOR if duplex else None))
    con.executemany("INSERT INTO stock_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", items)
    con.executemany("INSERT INTO stock_monthly VALUES (?,?,?,?,?,?,?,?)", monthly)
    events.sort(key=lambda e: (e[1], e[0]))
    con.executemany("INSERT INTO stockouts VALUES (?,?,?,?,?,?)", [
        (f"SO-{i:03d}", sku, iso(a), iso(b), max(1, ((b or data_date) - a).days), round(q, 2))
        for i, (sku, a, b, q) in enumerate(events, start=1)])

    # ---- plate yield: the plate issued to cutting, and what of it became parts; nesting
    # software from September 2025 lifted the yield from about 76% to 84%
    cut = {}
    for sku, m, _, issued, *_ in monthly:
        if sku.startswith("PL-"):
            cut[m] = cut.get(m, 0) + issued
    rows = []
    for m in sorted(cut):
        if cut[m] < 1 or m >= ym(data_date):
            continue
        k = (date.fromisoformat(m + "-01") - date(2025, 9, 1)).days / 30
        y = 76 + 8 * min(1.0, max(0.0, k / 3)) + rng.gauss(0, 1.1)
        loss = cut[m] * (1 - y / 100)
        reused = loss * rng.uniform(0.3, 0.45)
        rows.append((m, round(cut[m], 1), round(loss - reused, 1), round(reused, 1), round(y, 1)))
    con.executemany("INSERT INTO plate_yield_monthly VALUES (?,?,?,?,?)", rows)


# ──────────────────────────────────────────────────────────────────────────────
#  Maintenance and equipment
# ──────────────────────────────────────────────────────────────────────────────

LIFT = "Lifting inspection (12 months)"
PRESSURE = "Pressure receiver (24 months)"
CALIB = "Calibration (6 months)"
# id, name, class, work centre, criticality, installed, certificate, last certified, OEE tracked
ASSETS = [
    ("CUT-01", "CNC plasma and oxy-fuel cutter", "Cutting", "Cutting bay", "A", 2016, None, None, True),
    ("CUT-02", "Plate bevelling machine", "Cutting", "Cutting bay", "B", 2019, None, None, False),
    ("FRM-01", "Plate rolls 3 m x 60 mm", "Forming", "Rolling & forming", "A", 2018, None, None, True),
    ("FRM-02", "Plate rolls 2.5 m x 40 mm", "Forming", "Rolling & forming", "A", 2004, None, None, True),
    ("FRM-03", "Press brake 400 t", "Forming", "Rolling & forming", "B", 2012, None, None, False),
    ("WLD-01", "SAW column and boom 6 x 6 m", "Welding", "Welding bay", "A", 2017, None, None, True),
    ("WLD-02", "SAW column and boom 4 x 4 m", "Welding", "Welding bay", "A", 2010, None, None, True),
    ("WLD-03", "Welding rotators 100 t", "Welding", "Welding bay", "B", 2015, None, None, False),
    ("WLD-04", "Welding positioner 20 t", "Welding", "Welding bay", "B", 2014, None, None, False),
    ("NDT-01", "X-ray pipeline crawler", "Testing", "NDT", "B", 2020, None, None, False),
    ("HT-01", "PWHT furnace 12 m", "Furnace", "Heat treatment", "A", 2011, CALIB, "2026-07-28", True),
    ("TST-01", "Hydrotest pump and gauges", "Testing", "Testing", "B", 2018, CALIB, "2026-06-02", False),
    ("BP-01", "Blast booth and grit recovery", "Surface", "Blast & paint", "A", 2013, None, None, True),
    ("BP-02", "Paint booth", "Surface", "Blast & paint", "B", 2013, None, None, False),
    ("LFT-01", "Overhead crane 50 t, bay 1", "Lifting", "Plant", "A", 2009, LIFT, "2026-03-10", False),
    ("LFT-02", "Overhead crane 20 t, bay 1", "Lifting", "Plant", "A", 2009, LIFT, "2026-06-22", False),
    ("LFT-03", "Overhead crane 20 t, bay 2", "Lifting", "Plant", "A", 2012, LIFT, "2025-12-04", False),
    ("LFT-04", "Overhead crane 10 t, bay 3", "Lifting", "Plant", "B", 2015, LIFT, "2026-02-18", False),
    ("LFT-05", "Mobile crane 80 t", "Lifting", "Yard", "A", 2014, LIFT, "2025-11-16", False),
    ("LFT-06", "Forklift 10 t", "Lifting", "Yard", "B", 2017, LIFT, "2026-01-25", False),
    ("LFT-07", "Forklift 5 t", "Lifting", "Yard", "C", 2020, LIFT, "2026-09-03", False),
    ("UTL-01", "Air compressor 1 and receiver", "Utilities", "Plant", "B", 2011, PRESSURE, "2024-12-17", False),
    ("UTL-02", "Air compressor 2 and receiver", "Utilities", "Plant", "B", 2019, PRESSURE, "2025-08-05", False),
]
FAILURES = {
    "Forming": [("Hydraulic", 0.45), ("Mechanical wear", 0.25), ("Electrical", 0.15), ("Controls", 0.10),
                ("Operator damage", 0.05)],
    "Welding": [("Electrical", 0.35), ("Mechanical wear", 0.25), ("Controls", 0.20), ("Wire feed", 0.15),
                ("Operator damage", 0.05)],
    "Furnace": [("Burner / thermocouple", 0.50), ("Controls", 0.25), ("Electrical", 0.15), ("Refractory", 0.10)],
    "Lifting": [("Electrical", 0.35), ("Brakes", 0.20), ("Wire rope / hook", 0.20), ("Mechanical wear", 0.15),
                ("Controls", 0.10)],
    "Utilities": [("Mechanical wear", 0.40), ("Electrical", 0.30), ("Air leak", 0.30)],
}
DEFAULT_FAILURES = [("Electrical", 0.35), ("Mechanical wear", 0.30), ("Controls", 0.20), ("Operator damage", 0.15)]
CERT_MONTHS = {LIFT: 12, PRESSURE: 24, CALIB: 6}


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


def _maintenance(con, data_date, seed):
    rng = random.Random(f"{seed}:maintenance")
    load = _load_ratio(con)
    start = date(2025, 1, 1)
    orders, monthly, asset_rows = [], [], []
    for aid, name, cls, wc, crit, installed, cert, last_cert, oee in ASSETS:
        expires = None
        if cert:
            lc = _d(last_cert)
            expires = _add_months(lc, CERT_MONTHS[cert])
            # the inspections that led here, one per period, done on time
            when = lc
            while when >= start:
                orders.append([aid, "Statutory inspection", when - timedelta(days=21), when, when, 2.0,
                               rng.randint(2500, 6500), None])
                when = _add_months(when, -CERT_MONTHS[cert])
            nxt = expires
            if nxt - timedelta(days=30) <= data_date:   # the next one is raised a month ahead
                orders.append([aid, "Statutory inspection", nxt - timedelta(days=30), nxt, None, 0.0, 0, None])
        asset_rows.append((aid, name, cls, wc, crit, installed, cert, last_cert, iso(expires)))
        # ---- planned maintenance on its interval; slipped in the summer of 2026
        interval = {"A": 30, "B": 90, "C": 180}[crit]
        due = start + timedelta(days=rng.randint(0, interval - 1))
        missed = []                                                  # dates a PM was done late
        while due <= data_date + timedelta(days=14):
            summer = date(2026, 6, 1) <= due <= date(2026, 8, 31)
            delay = rng.randint(10, 35) if summer and rng.random() < 0.5 else rng.randint(-3, 9)
            done = due + timedelta(days=delay)
            if delay > 7:
                missed.append(due)
            hours = rng.uniform(4, 8) if crit == "A" else rng.uniform(2, 4)
            orders.append([aid, "Preventive", due - timedelta(days=14), due, done if done <= data_date else None,
                           round(hours, 1) if done <= data_date else 0.0,
                           round(rng.uniform(1500, 6000), -1) if done <= data_date else 0, None])
            due += timedelta(days=interval)
        # ---- breakdowns: older machines and missed PMs fail more; FRM-02 more and more
        base = {"A": 0.13, "B": 0.06, "C": 0.03}[crit] * (1 + max(0, 2026 - installed) / 12)
        down_by_month = {}
        for m in months(start, data_date):
            lam = base
            if any(0 <= (m - d).days <= 60 for d in missed):
                lam *= 2.8
            if aid == "FRM-02":
                lam *= 1.5 + 5 * min(1.0, max(0.0, (m - date(2026, 2, 1)).days / 270))
            if aid == "HT-01":
                lam *= 1.5
            days_in = (min(month_end(m), data_date) - m).days + 1
            for _ in range(30):
                if rng.random() < lam / 30 * days_in / 30:
                    raised = m + timedelta(days=rng.randint(0, days_in - 1))
                    mu = math.log(16 if aid == "FRM-02" else 10 if crit == "A" else 6)
                    hours = round(min(160, rng.lognormvariate(mu, 0.8)), 1)
                    done = raised + timedelta(days=max(0, round(hours / 10)))
                    mode = weighted(rng, FAILURES.get(cls, DEFAULT_FAILURES))
                    orders.append([aid, "Breakdown", raised, raised, done if done <= data_date else None,
                                   hours, round(hours * 450 + rng.lognormvariate(math.log(4000), 0.9), -1), mode])
                    down_by_month[ym(raised)] = down_by_month.get(ym(raised), 0) + hours
        if oee:
            for m in months(start, data_date):
                if ym(m) >= ym(data_date):
                    continue
                util = min(1.0, 0.35 + 0.65 * load.get((wc, ym(m)), 0))
                planned = round(440 * util * (0.85 if ym(m) in RAMADAN else 1.0))
                perf = rng.uniform(72, 85) if aid == "FRM-02" else rng.uniform(80, 92)
                monthly.append((aid, ym(m), planned, round(min(planned, down_by_month.get(ym(m), 0)), 1),
                                round(perf, 1), round(rng.uniform(96.5, 99.5), 1)))
    orders.sort(key=lambda o: (o[2], o[0], o[1]))
    rows = []
    for i, (aid, kind, raised, due, done, hours, cost, mode) in enumerate(orders, start=1):
        late_after = due + timedelta(days=7 if kind == "Preventive" else 0)
        status = ("Done" if done else "Open" if kind == "Breakdown" or late_after >= data_date else "Overdue")
        rows.append((f"WO-{i:05d}", aid, kind, iso(raised), iso(due), iso(done), status, hours, cost, mode))
    con.executemany("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)", asset_rows)
    con.executemany("INSERT INTO maintenance_orders VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO asset_monthly VALUES (?,?,?,?,?,?)", monthly)


# ──────────────────────────────────────────────────────────────────────────────
#  People: roster, hours, qualifications, training
# ──────────────────────────────────────────────────────────────────────────────

# trade: (code, own staff, agency staff in 2025, extra agency staff in the 2026 peak, work centre)
TRADES = {
    "Welder": ("W", 40, 0, 0, "Welding bay"),
    "Fitter": ("F", 34, 2, 10, "Fit-up"),
    "Helper": ("H", 30, 10, 14, "Fit-up"),
    "Rigger": ("R", 10, 1, 3, None),
    "Painter / blaster": ("P", 10, 3, 5, "Blast & paint"),
    "Machine operator": ("O", 14, 0, 0, "Cutting bay"),
    "QC inspector": ("Q", 8, 0, 0, None),
    "NDT technician": ("N", 7, 0, 0, "NDT"),
    "Supervisor": ("S", 11, 0, 0, None),
}
COURSES = {"H2S awareness": 12, "Working at height": 24, "Confined space entry": 24,
           "Fire watch": 24, "Rigging and slinging": 24, "First aid": 24, "Radiation safety": 12}
REQUIRED = {
    "H2S awareness": set(TRADES),
    "Working at height": {"Welder", "Fitter", "Helper", "Rigger", "Painter / blaster"},
    "Confined space entry": {"Welder", "Fitter", "Helper", "NDT technician"},
    "Fire watch": {"Helper"},
    "Rigging and slinging": {"Rigger", "Supervisor"},
    "First aid": {"Supervisor"},
    "Radiation safety": {"NDT technician"},
}
PEAK = (date(2026, 3, 1), date(2026, 10, 15))           # extra agency staff for the 2026 peak


def _people(con, data_date, seed):
    rng = random.Random(f"{seed}:people")
    load = _load_ratio(con)
    start = date(2025, 1, 1)
    people = []                                                       # [id, trade, employment, joined, left]
    counters = {}

    def new_id(code, agency):
        n = counters.get((code, agency), 100 if agency else 0) + 1
        counters[(code, agency)] = n
        return f"{code}-{n:03d}"

    for trade, (code, own, agency, peak, _) in TRADES.items():
        for _ in range(own):
            joined = date(2025, 1, 1) - timedelta(days=rng.randint(60, 3650))
            pid = f"W-{len([p for p in people if p[1] == trade]) + 1:03d}" if trade == "Welder" else new_id(code, False)
            people.append([pid, trade, "Own staff", joined, None])
        for _ in range(agency):
            people.append([new_id(code, True), trade, "Agency", date(2025, 1, 1) - timedelta(days=rng.randint(30, 700)), None])
        for _ in range(peak):
            joined = PEAK[0] + timedelta(days=rng.randint(0, 40))
            left = PEAK[1] + timedelta(days=rng.randint(-20, 25)) if rng.random() < 0.8 else None
            people.append([new_id(code, True), trade, "Agency", joined, left if left and left <= data_date else None])
    # attrition: own staff (not welders, the weld log's welders stay) leave about 1 in 9 a
    # year and are replaced a month or two later; agency staff turn over faster
    for p in list(people):
        if p[0].startswith("W-"):
            continue
        rate = 0.009 if p[2] == "Own staff" else 0.025
        day = max(p[3], start)
        while day <= data_date and p[4] is None:
            if rng.random() < rate:
                p[4] = day + timedelta(days=rng.randint(0, 27))
                if p[4] > data_date:
                    p[4] = None
                    break
                repl = p[4] + timedelta(days=rng.randint(25, 75))
                if repl <= data_date and (p[2] == "Own staff" or repl < PEAK[1]):
                    people.append([new_id(TRADES[p[1]][0], p[2] == "Agency"), p[1], p[2], repl, None])
            day = next_month(day)
    for p in people:
        if p[4] and p[4] <= p[3]:
            p[4] = p[3] + timedelta(days=30)
        if p[4] and p[4] > data_date:
            p[4] = None
    people.sort(key=lambda p: p[0])
    con.executemany("INSERT INTO people VALUES (?,?,?,?,?)", [(p[0], p[1], p[2], iso(p[3]), iso(p[4])) for p in people])

    # ---- hours by month: welders and fitters work overtime when their bay is overloaded
    rows = []
    for m in months(start, data_date):
        k, end = ym(m), month_end(m)
        for trade, (code, *_, wc) in TRADES.items():
            for emp in ("Own staff", "Agency"):
                grp = [p for p in people if p[1] == trade and p[2] == emp]
                head = sum(1 for p in grp if p[3] <= end and (p[4] is None or p[4] > end))
                if not head and not any(p[3] <= end for p in grp):
                    continue
                joiners = sum(1 for p in grp if p[3].strftime("%Y-%m") == k and p[3] >= start)
                leavers = sum(1 for p in grp if p[4] and ym(p[4]) == k)
                frac = (min(end, data_date) - m).days / (end - m).days if k == ym(data_date) else 1.0
                normal = head * 208 * (0.8 if k in RAMADAN else 1.0) * frac
                lr = load.get((wc, k), 0.7) if wc else 0.7
                ot = (min(0.34, max(0.03, 0.04 + 0.5 * (lr - 0.8))) if trade in ("Welder", "Fitter")
                      else rng.uniform(0.05, 0.09))
                ot = max(0.02, ot + rng.gauss(0, 0.012))
                absence = 0.025 + (0.015 if k in SUMMER else 0) + (0.01 if k in RAMADAN else 0)
                rows.append((k, trade, emp, head, round(normal), round(normal * ot),
                             round(head * 22 * frac * absence * rng.uniform(0.8, 1.2), 1), joiners, leavers))
    con.executemany("INSERT INTO labour_monthly VALUES (?,?,?,?,?,?,?,?,?)", rows)

    # ---- qualifications: every welder holds all four processes (the weld log has them
    # welding all four), renewed every two years; three lapsed a few weeks ago
    active = [p for p in people if p[4] is None]
    quals = []
    lapsed = {("W-007", "GTAW"), ("W-018", "SMAW"), ("W-031", "FCAW")}
    for p in active:
        if p[1] == "Welder":
            for proc in ("SMAW", "GTAW", "SAW", "FCAW"):
                if (p[0], proc) in lapsed:
                    issued = data_date - timedelta(days=730 + rng.randint(12, 40))
                elif rng.random() < 0.05:       # tested in a batch in December 2024: due again now
                    issued = date(2024, 12, 8) + timedelta(days=rng.randint(0, 14))
                else:
                    issued = data_date - timedelta(days=rng.randint(1, 729))
                quals.append((p[0], p[1], f"Welder qualification (WPQ) {proc}", proc, issued, issued + timedelta(days=730)))
        elif p[1] == "NDT technician":
            for q in ("ASNT level II RT", "ASNT level II UT"):
                issued = data_date - timedelta(days=rng.randint(30, 1800))
                quals.append((p[0], p[1], q, None, issued, issued + timedelta(days=1825)))
            issued = data_date - timedelta(days=rng.randint(380, 420) if p[0] in ("N-002", "N-005") else rng.randint(20, 360))
            quals.append((p[0], p[1], "Eye test (yearly)", None, issued, issued + timedelta(days=365)))
        elif p[1] == "QC inspector":
            issued = data_date - timedelta(days=1825 - 45 if p[0] == "Q-003" else rng.randint(60, 1700))
            quals.append((p[0], p[1], "CSWIP 3.1 welding inspector", None, issued, issued + timedelta(days=1825)))
            if p[0] in ("Q-001", "Q-004", "Q-006"):
                issued = data_date - timedelta(days=rng.randint(60, 1700))
                quals.append((p[0], p[1], "BGAS painting inspector", None, issued, issued + timedelta(days=1825)))
        elif p[1] == "Rigger":
            issued = data_date - timedelta(days=rng.randint(1, 1095))
            quals.append((p[0], p[1], "Rigger level II (third party)", None, issued, issued + timedelta(days=1095)))
        elif p[1] == "Machine operator" and int(p[0][2:]) % 2 == 0:
            issued = data_date - timedelta(days=rng.randint(1, 1095))
            quals.append((p[0], p[1], "Overhead crane operator", None, issued, issued + timedelta(days=1095)))
    quals.sort(key=lambda q: (q[0], q[2]))
    con.executemany("INSERT INTO qualifications VALUES (?,?,?,?,?,?,?)", [
        (f"Q-{i:04d}", pid, trade, q, proc, iso(a), iso(b)) for i, (pid, trade, q, proc, a, b) in enumerate(quals, 1)])

    # ---- training: each course a trade needs, the latest time each person did it; agency
    # staff lapse more, and the yearly H2S course run for them in October 2025 has lapsed
    recs = []
    for p in active:
        for course, months_valid in COURSES.items():
            if p[1] not in REQUIRED[course]:
                continue
            valid_days = round(months_valid * 30.4)
            agency = p[2] == "Agency"
            new = (data_date - p[3]).days < 30
            if new and rng.random() < 0.5:
                done = None
            elif course == "H2S awareness" and agency and p[3] < date(2025, 10, 1) and rng.random() < 0.6:
                done = date(2025, 10, 5) + timedelta(days=rng.randint(0, 30))
            elif rng.random() < (0.78 if agency else 0.94):
                done = data_date - timedelta(days=rng.randint(1, min(valid_days - 1, max(1, (data_date - p[3]).days))))
            else:
                done = data_date - timedelta(days=rng.randint(valid_days + 1, valid_days + 150))
            recs.append((p[0], p[1], p[2], course, iso(done),
                         iso(done + timedelta(days=valid_days)) if done else None))
    recs.sort(key=lambda r: (r[0], r[3]))
    con.executemany("INSERT INTO training_records VALUES (?,?,?,?,?,?,?)",
                    [(f"TR-{i:05d}", *r) for i, r in enumerate(recs, start=1)])


def build_business(con, data_date, seed) -> None:
    """Write every business table (the shop's tables must already be written)."""
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO calendar_months VALUES (?,?,?)",
                    [(ym(m), iso(m), iso(month_end(m))) for m in months(date(2024, 9, 1), date(2027, 12, 1))])
    _finance(con, data_date, seed)
    _sales(con, data_date, seed)
    _stock(con, data_date, seed)
    _maintenance(con, data_date, seed)
    _people(con, data_date, seed)
    for idx in ("invoices(project_no)", "invoices(client)", "tenders(status)", "stock_monthly(month)",
                "maintenance_orders(asset_id)", "training_records(person_id)", "qualifications(person_id)"):
        con.execute(f"CREATE INDEX IF NOT EXISTS ix_{idx.replace('(', '_').replace(')', '')} ON {idx}")
    con.executescript(VIEWS)
