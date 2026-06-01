"""
Builds rich, DB-grounded context for forecast AI insights.

The frontend already holds the forecast payload, so it is passed back in; this
module summarises it and augments it with *fresh* aggregate facts queried
straight from the LIMS source tables (counts, per-lab / per-year breakdowns,
consumption totals) so Claude grounds its answer in real data, not just the
chart.
"""
from .db import get_connection, get_inventory_connection
from .views.inventory import _normalize_stock, ALLOWED_STOCKS


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:  # never let DB grounding break the insight
        print(f"[forecast-insights] grounding query failed: {e}")
        return default


def _series_lines(labels, values, limit=24):
    pairs = list(zip(labels or [], values or []))[:limit]
    return "; ".join(f"{l}={v}" for l, v in pairs) or "(none)"


# --------------------------------------------------------------------------
# Per-feature DB grounding (fast aggregates only — no model training)
# --------------------------------------------------------------------------
def _downtime_facts(params):
    inst = params.get("instrument", "")

    def q():
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    SUM(CASE WHEN EVENT_TYPE='OFF' THEN 1 ELSE 0 END) AS off_events,
                    SUM(CASE WHEN EVENT_TYPE='ON'  THEN 1 ELSE 0 END) AS on_events,
                    MIN(ENTERED_ON), MAX(ENTERED_ON)
                FROM dbo.INSTRUMENTS1_LOG
                WHERE INSTRUMENT = ? AND EVENT_TYPE IN ('ON','OFF') AND ENTERED_ON IS NOT NULL
                """,
                (inst,),
            )
            r = cur.fetchone()
            return (
                f"DB facts for instrument {inst}: {r[0]} OFF events, {r[1]} ON events, "
                f"log spans {r[2]:%Y-%m-%d} to {r[3]:%Y-%m-%d}."
            ) if r and r[2] else None

    return _safe(q)


def _sample_facts(params):
    lab = params.get("lab")  # None for all-labs

    def q():
        with get_connection() as conn:
            cur = conn.cursor()
            lines = []
            # Year-over-year totals
            cur.execute(
                "SELECT YEAR(LOGIN_DATE) y, COUNT(*) FROM dbo.SAMPLE "
                "WHERE LOGIN_DATE IS NOT NULL GROUP BY YEAR(LOGIN_DATE) ORDER BY y"
            )
            yoy = "; ".join(f"{int(r[0])}={r[1]}" for r in cur.fetchall())
            lines.append(f"DB year-over-year total samples (all groups): {yoy}.")
            # Top groups (overall)
            cur.execute(
                "SELECT TOP 8 GROUP_NAME, COUNT(*) c FROM dbo.SAMPLE "
                "WHERE GROUP_NAME IS NOT NULL GROUP BY GROUP_NAME ORDER BY c DESC"
            )
            top = "; ".join(f"{r[0]}={r[1]}" for r in cur.fetchall())
            lines.append(f"DB busiest sample groups: {top}.")
            return "\n".join(lines)

    return _safe(q)


def _inventory_facts(params):
    stock = (params.get("stock") or "").upper().strip()

    def q():
        with get_inventory_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT YEAR(tr.TRANSACTION_DATE) y, COUNT(*) n, SUM(tr.QUANTITY) total
                FROM dbo.INVENTORY_TRANS tr
                JOIN dbo.INVENTORY_ITEM it ON tr.INVENTORY_ITEM = it.ITEM_NUMBER
                WHERE tr.TRANSACTION_TYPE='PULL' AND tr.TRANSACTION_STATUS='C'
                  AND tr.QUANTITY > 0 AND tr.TRANSACTION_DATE IS NOT NULL
                  AND (UPPER(LTRIM(RTRIM(it.STOCK))) = ? OR UPPER(LTRIM(RTRIM(it.STOCK))) LIKE ?)
                GROUP BY YEAR(tr.TRANSACTION_DATE) ORDER BY y
                """,
                (stock, f"%{stock}"),
            )
            rows = cur.fetchall()
            if not rows:
                return None
            yoy = "; ".join(f"{int(r[0])}: {r[1]} pulls / {round(r[2],1)} units" for r in rows)
            return f"DB year-over-year {stock} consumption (PULL): {yoy}."

    return _safe(q)


_GROUNDERS = {
    "downtime": _downtime_facts,
    "sample": _sample_facts,
    "section": _sample_facts,
    "inventory": _inventory_facts,
}


def build_context(feature, params, forecast):
    """Assemble a textual context block for Claude from the forecast payload
    plus fresh DB aggregates."""
    feature = (feature or "").lower()
    forecast = forecast or {}
    lines = [f"FORECAST TYPE: {feature}", f"PARAMETERS: {params}"]

    if feature == "downtime":
        hist = forecast.get("historical", {})
        fc = forecast.get("forecast", {})
        stats = forecast.get("stats", {})
        trend = forecast.get("trend", {})
        cl = forecast.get("control_limit", {})
        lines.append(f"Historical monthly downtime hours: {_series_lines(hist.get('labels'), hist.get('values'))}")
        lines.append(f"Forecast monthly downtime hours: {_series_lines(fc.get('labels'), fc.get('values'))}")
        lines.append(f"Acceptable control limit: {(cl.get('values') or [None])[0]} hours/month.")
        lines.append(f"Trend: historical={trend.get('historical')}, forecast={trend.get('forecast')}.")
        lines.append(f"Stats: {stats}.")
    else:
        labels = forecast.get("labels")
        values = forecast.get("values")
        past = forecast.get("PastData") or forecast.get("pastData") or []
        past_str = "; ".join(f"{p.get('date')}={p.get('samples')}" for p in past[:36])
        lines.append(f"Historical monthly values: {past_str or '(none)'}")
        lines.append(f"Forecast monthly values: {_series_lines(labels, values)}")
        lines.append(f"Forecast average: {forecast.get('orangeDashedLineAverage(Y-Axis)')}")
        lines.append(f"Highest: {forecast.get('highestDay(Stats)') or forecast.get('highestMonth(Stats)')}")
        lines.append(f"Lowest: {forecast.get('lowestDay(Stats)') or forecast.get('lowestMonth(Stats)')}")
        lines.append(
            "Quartiles (forecast): Q1="
            f"{forecast.get('LowerQuartile-Forecasted', forecast.get('forecastLowerQuartile'))}, "
            f"Q3={forecast.get('UpperQuartile-Forecasted', forecast.get('forecastUpperQuartile'))}"
        )
        if forecast.get("storyboard"):
            lines.append(f"Storyboard: {forecast['storyboard']}")

    grounder = _GROUNDERS.get(feature)
    if grounder:
        facts = grounder(params or {})
        if facts:
            lines.append("\n--- LIVE DATABASE FACTS ---\n" + facts)

    return "\n".join(str(x) for x in lines)


FEATURE_LABEL = {
    "downtime": "instrument downtime",
    "sample": "lab sample volume",
    "section": "lab sample volume",
    "inventory": "inventory consumption",
}


def suggested_questions(feature):
    feature = (feature or "").lower()
    if feature == "downtime":
        return [
            "Why is downtime trending this way and what should maintenance prioritise?",
            "Is the forecast at risk of breaching the acceptable control limit?",
            "Which months need preventive maintenance scheduled?",
        ]
    if feature == "inventory":
        return [
            "When should we reorder to avoid stock-outs?",
            "How much should we budget for the next two quarters?",
            "Is consumption seasonal, and which months peak?",
        ]
    return [
        "Which months will be busiest and need more staff?",
        "What seasonal patterns drive sample volume?",
        "How does the forecast compare to last year?",
    ]
