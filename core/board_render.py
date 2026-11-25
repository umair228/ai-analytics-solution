"""A board as a management PDF, rendered on the server: the scheduled "board" report.

The browser's "Export PDF" photographs the board; a scheduled email has no browser,
and the reporting deployment has no internet to fetch one. So the server draws the
board itself with ReportLab, from the same rows the board shows: section headings, KPI
cards (value, change against the previous period, caption), ranked lists with their
bars, progress against a target, Pareto charts (core/management_figures.py: the same
"3 of 9 make up 78%" as the board), bullet rows (measure, target tick, grey zones, the
verdict in words), the first rows of each table, and bar and line charts. Numbers read as on the board (formatValue in ManagementWidgets.jsx: separators,
one decimal, "SAR 1.2M", "12.5 d"); colours are the board's colour-blind-safe palette.

A widget whose dataset the report's owner can no longer see, or whose query fails,
says so in its card: a broken figure must read as broken, never as zero.
"""
from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

# src/lib/chartTheme.js (light): categorical palette and tones.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
TONES = {"good": "#0ca30c", "watch": "#fab219", "serious": "#ec835a", "act": "#d03b3b",
         "info": "#2a78d6", "neutral": "#b4bac4"}
INK, MUTED, FAINT, RULE, TRACK = "#0b1220", "#5b6472", "#8a93a3", "#d9dde3", "#eef0f3"
# Bullet zones short of the target (management.css .mg-bullet-band--fair / --poor) and
# the verdict chips' words and soft backgrounds (tokens.css --mg-*-text / --mg-*-soft).
BAND_FAIR, BAND_POOR = "#e0e4ea", "#c9ced7"
TONE_TEXT = {"good": "#006300", "watch": "#7a5200", "act": "#a12a2a"}
TONE_SOFT = {"good": "#e7f6e7", "watch": "#fff5dc", "act": "#fdecec"}
LINE_INK = "#52514e"   # chartTheme ink2: the Pareto's running-total line
STALE = "#a12a2a"
UNIT_SUFFIX = {"%": "%", "percent": "%", "h": " h", "hours": " h", "d": " d", "days": " d"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DEFAULT_LIST_ROWS = 8
DEFAULT_TABLE_ROWS = 10
GRID_COLUMNS = 12


# ──────────────────────────────────────────────────────────────────────────────
#  Numbers and labels, as the board writes them
# ──────────────────────────────────────────────────────────────────────────────

def _num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _grouped(n: float, decimals: int = 1) -> str:
    rounded = round(n, decimals)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.{decimals}f}".rstrip("0").rstrip(".")


def format_value(value, unit="") -> str:
    """formatValue (ManagementWidgets.jsx): separators, at most one decimal, compact
    from a million, the unit attached; a currency code reads first."""
    if value is None or value == "":
        return "—"
    n = _num(value)
    if n is None:
        return str(value)
    u = str(unit or "").strip()
    if abs(n) >= 1e6:
        div, suffix = (1e9, "B") if abs(n) >= 1e9 else (1e6, "M")
        text = f"{_grouped(n / div)}{suffix}"
    elif u in ("ratio", "x"):
        text = f"{n:.2f}"
    else:
        text = _grouped(n)
    if not u or u in ("ratio", "count"):
        return text
    if u in UNIT_SUFFIX:
        return f"{text}{UNIT_SUFFIX[u]}"
    if re.fullmatch(r"[A-Z]{3}", u):
        return f"{u} {text}"
    return f"{text} {u}"


def category_label(value, fmt=None) -> str:
    """"Jun 2026" for a month category, "29 Jun 2026" for a date, else the text."""
    text = "" if value is None else str(value)
    if fmt == "month":
        m = re.match(r"^(\d{4})-(\d{2})", text)
        if m:
            return f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ]00:00(?::00)?)?", text)
    if m:
        return f"{int(m.group(3))} {MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return text


def format_stamp(iso) -> str:
    """"22 Nov 2026, 15:56" from an ISO time, as written (no time-zone shift): the words
    the board header uses (src/lib/freshness.js formatStamp)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", str(iso or ""))
    if not m:
        return ""
    return f"{int(m.group(3))} {MONTHS[int(m.group(2)) - 1]} {m.group(1)}, {m.group(4)}:{m.group(5)}"


def rollup(nums, kind="sum"):
    nums = [n for n in nums if n is not None]
    if not nums:
        return None
    kind = (kind or "sum").lower()
    if kind == "avg":
        return sum(nums) / len(nums)
    if kind == "count":
        return len(nums)
    if kind == "min":
        return min(nums)
    if kind == "max":
        return max(nums)
    return sum(nums)


def delta_for(series, direction="neutral"):
    """Change between the last two points, judged by the direction (deltaFor)."""
    pts = [p for p in (_num(v) for v in series or []) if p is not None]
    if len(pts) < 2:
        return None
    last, prev = pts[-1], pts[-2]
    diff = last - prev
    pct = (diff / abs(prev) * 100) if prev else None
    sense = "neutral" if diff == 0 or direction == "neutral" else (
        "good" if (diff > 0) == (direction == "higher_better") else "bad")
    return {"diff": diff, "pct": pct, "sense": sense}


def status_for(value, thresholds):
    tone = {"green": "good", "amber": "watch", "red": "act", "blue": "info"}
    words = {"good": "On track", "watch": "Watch", "act": "Needs action", "info": "Note"}
    v = _num(value)
    for t in thresholds or []:
        tv = _num(t.get("value"))
        if v is None or tv is None:
            continue
        op = t.get("op")
        if ((op == "lt" and v < tv) or (op == "lte" and v <= tv) or (op == "gt" and v > tv)
                or (op == "gte" and v >= tv) or (op == "eq" and v == tv)):
            k = tone.get(t.get("color"), "info")
            return {"tone": k, "label": t.get("label") or words[k]}
    return None


def humanize(name: str) -> str:
    acronyms = {"kpi": "KPI", "id": "ID", "rfq": "RFQ", "rfqs": "RFQs", "mto": "MTO",
                "wbs": "WBS", "bom": "BOM", "p6": "P6", "bd": "BD", "afc": "AFC"}
    words = str(name).replace("_", " ").split()
    return " ".join(acronyms.get(w.lower(), w.capitalize() if i == 0 else w)
                    for i, w in enumerate(words))


_RAW_COLUMN = re.compile(r"(year|^id$|_id$|number|code|_no$|seq)", re.I)


def table_cell(value, field_name, fmt=None) -> str:
    """Board-table numbers: separators except identifiers, years and codes."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool) or fmt == "raw" or _RAW_COLUMN.search(field_name or ""):
        return str(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return _grouped(float(value))
    return category_label(value)          # "7 Jun 2026" for a date, else the text


# ──────────────────────────────────────────────────────────────────────────────
#  The rows behind a widget
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class WidgetData:
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    error: str = ""


def widget_data(widget, owner=None) -> WidgetData:
    """The rows the board shows for ``widget``: the dataset's cached result (refreshed
    first if its query changed), or the full result when the cache is a capped preview.
    """
    from analytics.engine import build_dataframe, df_to_rows
    from datasets.services import query_changed_since_refresh, refresh_dataset

    ds = widget.dataset
    if ds is None:
        return WidgetData()
    if owner is not None and not ds.accessible_by(owner):
        return WidgetData(error="Not available to this report's owner.")
    try:
        if not ds.last_refreshed_at or query_changed_since_refresh(ds):
            refresh_dataset(ds)
        df = build_dataframe(ds)
        columns, rows = [str(c) for c in df.columns], df_to_rows(df)
        total = getattr(ds, "total_row_count", None) or 0
        if total > len(rows) and not (ds.calculated_fields or []):
            from datasets.explore import run_explore
            from querybuilder.executor import EXPLORE_MAX_PAGE
            full = run_explore(ds, {"mode": "rows", "limit": EXPLORE_MAX_PAGE,
                                    "with_count": False})
            columns, rows = full.get("columns") or columns, full.get("rows") or rows
        return WidgetData(columns, rows)
    except Exception as exc:  # noqa: BLE001 — one widget must not sink the report
        logger.warning("Board report: widget %s (%s) failed: %s", widget.pk, widget.title, exc)
        return WidgetData(error="This figure could not be computed.")


def _col(data: WidgetData, name):
    try:
        return data.columns.index(name)
    except ValueError:
        return -1


# ──────────────────────────────────────────────────────────────────────────────
#  Layout: sections, and rows of cards across the 12-column grid
# ──────────────────────────────────────────────────────────────────────────────

def plan(widgets) -> list:
    """[("section", widget) | ("row", [widget, ...])] in reading order. A text widget of
    height 2 or less is a section heading; other widgets fill rows of 12 columns."""
    out, row, used = [], [], 0
    for w in sorted(widgets, key=lambda w: (w.y, w.x, w.order, w.pk or 0)):
        if w.widget_type == "text" and w.h <= 2:
            if row:
                out.append(("row", row))
            row, used = [], 0
            out.append(("section", w))
            continue
        width = max(1, min(GRID_COLUMNS, int(w.w or GRID_COLUMNS)))
        if row and used + width > GRID_COLUMNS:
            out.append(("row", row))
            row, used = [], 0
        row.append(w)
        used += width
    if row:
        out.append(("row", row))
    return out


@dataclass
class BoardPdf:
    content: bytes
    pages: int
    widgets: int
    unavailable: int
    data_as_of: str
    stale: bool


def render_board_pdf(dashboard, *, owner=None, product="", footer_text="",
                     freshness=None, generated_at="", generated_by="") -> BoardPdf:
    """Landscape A4: header and footer on every page, the board in reading order,
    a row of cards never split across pages, a section heading kept with its row."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (
        KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    page_w, page_h = landscape(A4)
    margin = 12 * mm
    avail = page_w - 2 * margin
    gap = 4 * mm
    c = colors.HexColor

    styles = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, leading=19,
                                textColor=c(INK)),
        "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=9, leading=12,
                              textColor=c(MUTED)),
        "section": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=12, leading=15,
                                  textColor=c(INK), spaceBefore=4),
        "section_q": ParagraphStyle("q", fontName="Helvetica", fontSize=9, leading=12,
                                    textColor=c(MUTED)),
        "card_title": ParagraphStyle("ct", fontName="Helvetica-Bold", fontSize=9,
                                     leading=11.5, textColor=c(INK)),
        "label": ParagraphStyle("l", fontName="Helvetica", fontSize=8.5, leading=11,
                                textColor=c(MUTED)),
        "value": ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=20, leading=24,
                                textColor=c(INK)),
        "small": ParagraphStyle("sm", fontName="Helvetica", fontSize=7.5, leading=9.5,
                                textColor=c(MUTED)),
        "row": ParagraphStyle("r", fontName="Helvetica", fontSize=8, leading=10,
                              textColor=c(INK)),
        "row_r": ParagraphStyle("rr", fontName="Helvetica-Bold", fontSize=8, leading=10,
                                textColor=c(INK), alignment=2),
        "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=7, leading=8.5,
                               textColor=c(INK)),
        "head": ParagraphStyle("hd", fontName="Helvetica-Bold", fontSize=7, leading=8.5,
                               textColor=c(MUTED)),
        "warn": ParagraphStyle("w", fontName="Helvetica-Bold", fontSize=8.5, leading=11,
                               textColor=c(STALE)),
        "row_b": ParagraphStyle("rb", fontName="Helvetica-Bold", fontSize=8, leading=10,
                                textColor=c(INK)),
        **{f"verdict_{tone}": ParagraphStyle(f"vd{tone}", fontName="Helvetica-Bold", fontSize=6.5,
                                             leading=8, textColor=c(colour), leftIndent=4)
           for tone, colour in TONE_TEXT.items()},
    }

    def P(text, style, limit=None):
        text = "" if text is None else str(text)
        if limit and len(text) > limit:
            text = text[: limit - 1] + "…"
        return Paragraph(escape(text).replace("\n", "<br/>"), styles[style])

    widgets = list(dashboard.widgets.select_related("dataset", "dataset__query").all())
    counted = {"rendered": 0, "unavailable": 0}

    # ---- cards -----------------------------------------------------------------
    # A card is its cell's full width; its contents are drawn 14 pt narrower (padding).
    def card(inner, width):
        t = Table([[x] for x in inner], colWidths=[width])
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, c(RULE)),
            ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (0, 0), 6), ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
            ("TOPPADDING", (0, 1), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -2), 1),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    def unavailable(w, message, width):
        counted["unavailable"] += 1
        return card([P(w.title or "", "card_title"), P(message, "warn")], width)

    def kpi(w, data, width):
        cfg = w.config or {}
        vi = _col(data, cfg.get("value_field") or "value")
        if vi < 0 and data.columns:
            vi = len(data.columns) - 1
        nums = [_num(r[vi]) for r in data.rows] if vi >= 0 else []
        value = rollup(nums, cfg.get("rollup"))
        ti = _col(data, cfg.get("trend_field")) if cfg.get("trend_field") else -1
        trend = [n for n in ((_num(r[ti]) for r in data.rows) if ti >= 0 else []) if n is not None]
        series = trend[:-1] if cfg.get("value_from") == "last_complete" else trend
        if cfg.get("value_from") in ("last", "last_complete") and series:
            value = series[-1]
        unit = cfg.get("unit") or ""
        status = status_for(value, cfg.get("thresholds"))
        colour = {"good": TONES["good"], "act": TONES["act"], "watch": "#b27c00"}.get(
            (status or {}).get("tone"), INK)
        inner = [P(cfg.get("label") or w.title, "label"),
                 Paragraph(f'<font color="{colour}">{escape(format_value(value, unit))}</font>',
                           styles["value"])]
        delta = delta_for(series, cfg.get("direction") or "neutral")
        if delta:
            sign = "+" if delta["diff"] > 0 else ""
            pct = "" if delta["pct"] is None else f" ({'+' if delta['pct'] > 0 else ''}{round(delta['pct'])}%)"
            tone = {"good": TONES["good"], "bad": TONES["act"]}.get(delta["sense"], MUTED)
            inner.append(Paragraph(
                f'<font color="{tone}">{escape(sign + format_value(delta["diff"], unit) + pct)}</font>'
                f' {escape(cfg.get("delta_label") or "vs previous")}', styles["small"]))
        if status:
            inner.append(P(status["label"], "small"))
        if cfg.get("caption"):
            inner.append(P(cfg["caption"], "small", limit=220))
        return card(inner, width)

    def bar(width, ratio, colour, target=None):
        from reportlab.graphics.shapes import Drawing, Line, Rect
        d = Drawing(width, 5)
        d.add(Rect(0, 0, width, 4, fillColor=c(TRACK), strokeColor=None))
        d.add(Rect(0, 0, max(1.5, min(1.0, max(0.0, ratio)) * width), 4,
                   fillColor=c(colour), strokeColor=None))
        if target is not None:
            x = min(1.0, max(0.0, target)) * width
            d.add(Line(x, -1, x, 5, strokeColor=c(INK), strokeWidth=0.8))
        return d

    def ranked(w, data, width, *, keep_order=False):
        cfg = w.config or {}
        ci = _col(data, cfg.get("category_field")) if cfg.get("category_field") else 0
        vi = _col(data, cfg.get("value_field")) if cfg.get("value_field") else len(data.columns) - 1
        if ci < 0 or vi < 0:
            return unavailable(w, "This view's fields are not in its data.", width)
        ni = _col(data, cfg.get("name_field")) if cfg.get("name_field") else -1
        di = _col(data, cfg.get("detail_field")) if cfg.get("detail_field") else -1
        tones = cfg.get("tones") if isinstance(cfg.get("tones"), dict) else {}
        rows = []
        for r in data.rows:
            v = _num(r[vi])
            name = category_label(r[ci], cfg.get("category_format"))
            if v is None or name == "":
                continue
            sub = r[ni] if ni >= 0 and r[ni] not in (None, "") and str(r[ni]) != name else None
            detail = None
            if di >= 0 and r[di] is not None:
                fmt = cfg.get("detail_format") or "{value}"
                detail = fmt.replace("{value}", format_value(r[di]))
            rows.append((name, sub, v, detail, tones.get(name)))
        if not keep_order and cfg.get("sort") is not False:
            rows.sort(key=lambda t: -t[2])
        limit = int(cfg.get("limit") or DEFAULT_LIST_ROWS)
        shown, rest = rows[:limit], max(0, len(rows) - limit)
        top = max([t[2] for t in shown] + [0]) or 1
        inner_w = width - 14
        inner = [P(w.title, "card_title")]
        if cfg.get("caption"):
            inner.append(P(cfg["caption"], "small", limit=200))
        first = rows[0][2] if rows else None
        for i, (name, sub, v, detail, tone) in enumerate(shown):
            label = name + (f" · {sub}" if sub else "")
            right = format_value(v, cfg.get("unit"))
            if keep_order and first and i:
                right += f"  ({round(v / first * 100)}%)"
            line = Table([[P(label, "row", limit=80), P(right, "row_r")]],
                         colWidths=[inner_w * 0.74, inner_w * 0.26])
            line.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                      ("TOPPADDING", (0, 0), (-1, -1), 2),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            inner.append(line)
            if detail:
                inner.append(P(detail, "small", limit=90))
            inner.append(bar(inner_w, v / top, TONES.get(tone, PALETTE[0])))
        if rest:
            inner.append(P(f"and {rest} more", "small"))
        if not shown:
            inner.append(P("Nothing to show.", "small"))
        return card(inner, width)

    def progress(w, data, width):
        cfg = w.config or {}
        vi = _col(data, cfg.get("value_field") or "value")
        if vi < 0:
            return unavailable(w, "This view's fields are not in its data.", width)
        ci = _col(data, cfg.get("category_field")) if cfg.get("category_field") else -1
        ni = _col(data, cfg.get("name_field")) if cfg.get("name_field") else -1
        ti = _col(data, cfg.get("target_field")) if cfg.get("target_field") else -1
        unit = cfg.get("unit") or ""
        direction = cfg.get("direction") or "higher_better"
        rows = data.rows if ci >= 0 else data.rows[:1]
        limit = int(cfg.get("limit") or 12)
        inner_w = width - 14
        inner = [P(w.title, "card_title")]
        if cfg.get("caption"):
            inner.append(P(cfg["caption"], "small", limit=200))
        for r in rows[:limit]:
            v = _num(r[vi]) or 0
            target = _num(r[ti]) if ti >= 0 else _num(cfg.get("target"))
            ratio = (v / target) if target else None
            name = str(r[ci]) if ci >= 0 else (cfg.get("label") or w.title)
            if ni >= 0 and r[ni] and str(r[ni]) != name:
                name += f" · {r[ni]}"
            self_evident = unit in ("%", "percent") and target == 100
            right = format_value(v, unit)
            if target and not self_evident:
                right += f" of {format_value(target, unit)} ({round(ratio * 100)}%)"
            sense = "neutral" if ratio is None else (
                ("bad" if ratio > 1 else "neutral") if direction == "lower_better"
                else ("good" if ratio >= 1 else "neutral"))
            colour = {"bad": TONES["act"], "good": TONES["good"]}.get(sense, PALETTE[0])
            line = Table([[P(name, "row", limit=80), P(right, "row_r")]],
                         colWidths=[inner_w * 0.62, inner_w * 0.38])
            line.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                      ("TOPPADDING", (0, 0), (-1, -1), 2),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            inner.append(line)
            inner.append(bar(inner_w, ratio or 0, colour))
        if len(rows) > limit:
            inner.append(P(f"and {len(rows) - limit} more", "small"))
        return card(inner, width)

    def table(w, data, width):
        cfg = w.config or {}
        spec = [(x if isinstance(x, dict) else {"field": x}) for x in (cfg.get("columns") or [])]
        spec = [s for s in spec if s.get("field") in data.columns] or \
            [{"field": col} for col in data.columns]
        idx = [data.columns.index(s["field"]) for s in spec]
        limit = int(cfg.get("page_size") or DEFAULT_TABLE_ROWS)
        limit = min(limit, 14)
        inner_w = width - 14
        head = [P(s.get("label") or humanize(s["field"]), "head") for s in spec]
        body = [[P(table_cell(r[i], s["field"], s.get("format")), "cell", limit=60)
                 for i, s in zip(idx, spec)] for r in data.rows[:limit]]
        grid = Table([head] + body, colWidths=[inner_w / max(1, len(spec))] * len(spec),
                     repeatRows=1)
        grid.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, c(RULE)),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, c(TRACK)),
            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        inner = [P(w.title, "card_title")]
        if cfg.get("caption"):
            inner.append(P(cfg["caption"], "small", limit=200))
        inner.append(grid)
        if len(data.rows) > limit:
            inner.append(P(f"Showing {limit} of {len(data.rows):,}. The full list is on the board.",
                           "small"))
        elif not data.rows:
            inner.append(P("Nothing to show.", "small"))
        return card(inner, width)

    def chart(w, data, width):
        from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
        from reportlab.graphics.charts.legends import Legend
        from reportlab.graphics.charts.lineplots import LinePlot
        from reportlab.graphics.shapes import Drawing

        cfg = w.config or {}
        ci = _col(data, cfg.get("category_field")) if cfg.get("category_field") else 0
        fields = list(cfg.get("value_fields") or [])
        if not fields and cfg.get("value_field"):
            fields = [cfg["value_field"]]
        fields = [f for f in fields if f in data.columns]
        if ci < 0 or not fields:
            return unavailable(w, "This chart's fields are not in its data.", width)
        rows = data.rows
        roll = (cfg.get("rollup") or "sum").lower()
        if roll != "none" and len(fields) == 1:
            # The board aggregates per category on the server; the same, over the rows.
            agg, order = {}, []
            vi = data.columns.index(fields[0])
            for r in rows:
                key = r[ci]
                if key not in agg:
                    agg[key] = []
                    order.append(key)
                agg[key].append(_num(r[vi]))
            rows = [[k, rollup(agg[k], roll)] for k in order]
            ci, idx = 0, [1]
        else:
            idx = [data.columns.index(f) for f in fields]
        labels = [category_label(r[ci], cfg.get("category_format")) for r in rows]
        series = [[_num(r[i]) for r in rows] for i in idx]
        tones = cfg.get("tones") if isinstance(cfg.get("tones"), dict) else {}
        colours = [c(TONES.get(tones.get(f), PALETTE[k % len(PALETTE)])) for k, f in enumerate(fields)]
        inner_w = width - 14
        height = max(110.0, min(220.0, (w.h or 6) * 16.0))
        legend_h = 14 if len(fields) > 1 else 0
        d = Drawing(inner_w, height + legend_h)
        longest = max([len(x) for x in labels] + [1])
        step = max(1, math.ceil(len(labels) / max(1, int((inner_w - 50) // (longest * 3.3 + 10)))))
        shown_labels = [lab if i % step == 0 else "" for i, lab in enumerate(labels)]
        if w.widget_type in ("line", "area"):
            lp = LinePlot()
            lp.x, lp.y, lp.width, lp.height = 34, 18, inner_w - 44, height - 26
            lp.data = [[(i, v) for i, v in enumerate(s) if v is not None] or [(0, 0)] for s in series]
            for k, col in enumerate(colours):
                lp.lines[k].strokeColor = col
                lp.lines[k].strokeWidth = 1.6
            lp.xValueAxis.valueMin, lp.xValueAxis.valueMax = 0, max(1, len(labels) - 1)
            lp.xValueAxis.valueSteps = list(range(0, len(labels), step))
            lp.xValueAxis.labelTextFormat = lambda v: labels[int(v)] if 0 <= int(v) < len(labels) else ""
            lp.xValueAxis.labels.fontSize = 6
            lp.yValueAxis.labels.fontSize = 6
            lp.yValueAxis.labelTextFormat = lambda v: _grouped(v, 0)
            lp.yValueAxis.valueMin = 0
            lp.yValueAxis.visibleGrid = True
            lp.yValueAxis.gridStrokeColor = c(TRACK)
            d.add(lp)
        else:
            horizontal = cfg.get("orientation") == "horizontal"
            bc = HorizontalBarChart() if horizontal else VerticalBarChart()
            left = 70 if horizontal else 34
            bc.x, bc.y, bc.width, bc.height = left, 18, inner_w - left - 8, height - 26
            bc.data = [[v or 0 for v in s] for s in series]
            bc.categoryAxis.categoryNames = labels if horizontal else shown_labels
            bc.categoryAxis.labels.fontSize = 6
            bc.valueAxis.labels.fontSize = 6
            bc.valueAxis.labelTextFormat = lambda v: _grouped(v, 0)
            bc.valueAxis.valueMin = 0
            bc.valueAxis.visibleGrid = True
            bc.valueAxis.gridStrokeColor = c(TRACK)
            if cfg.get("stacked"):
                bc.categoryAxis.style = "stacked"
            for k, col in enumerate(colours):
                bc.bars[k].fillColor = col
                bc.bars[k].strokeColor = None
            if len(fields) == 1 and tones:
                for j, lab in enumerate(labels):
                    if tones.get(lab):
                        bc.bars[(0, j)].fillColor = c(TONES.get(tones[lab], PALETTE[0]))
            d.add(bc)
        if legend_h:
            # One line above the plot, inside the drawing: swatch, name, next.
            lg = Legend()
            lg.boxAnchor = "nw"
            lg.x, lg.y = 34, height + legend_h - 2
            lg.alignment = "right"
            lg.columnMaximum = 1
            lg.fontSize = 6.5
            lg.dx, lg.dy, lg.dxTextSpace = 6, 6, 3
            lg.deltax = max(len(f) for f in fields) * 3.6 + 22
            lg.strokeColor = None
            lg.colorNamePairs = list(zip(colours, fields))
            d.add(lg)
        inner = [P(w.title, "card_title")]
        if cfg.get("caption"):
            inner.append(P(cfg["caption"], "small", limit=200))
        inner.append(d)
        return card(inner, width)

    def pareto(w, data, width):
        from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Rect, String

        from core import management_figures as mf

        cfg = w.config or {}
        ci = _col(data, cfg.get("category_field")) if cfg.get("category_field") else 0
        vi = _col(data, cfg.get("value_field")) if cfg.get("value_field") else len(data.columns) - 1
        if ci < 0 or vi < 0:
            return unavailable(w, "This view's fields are not in its data.", width)
        ni = _col(data, cfg.get("name_field")) if cfg.get("name_field") else -1
        threshold = mf.num(cfg.get("threshold")) or 80
        what = cfg.get("what") or "the total"
        p = mf.pareto_bars(
            [{"name": category_label(r[ci], cfg.get("category_format")), "value": r[vi],
              "sub": r[ni] if ni >= 0 else None} for r in data.rows],
            limit=cfg.get("limit") or 10, threshold=threshold)
        inner = [P(w.title, "card_title")]
        if cfg.get("caption"):
            inner.append(P(cfg["caption"], "small", limit=200))
        summary = mf.pareto_summary(p, items=cfg.get("items") or "categories", what=what,
                                    threshold=threshold)
        if not summary:
            inner.append(P(cfg.get("empty") or "Nothing recorded for this selection.", "small"))
            return card(inner, width)
        inner.append(P(summary, "row_b"))
        inner_w = width - 14
        height = max(120.0, min(210.0, (w.h or 8) * 15.0))
        legend_h, label_h, left = 12, 26, 24
        d = Drawing(inner_w, height + legend_h)
        plot_w, plot_h, y0 = inner_w - left - 4, height - label_h - 6, label_h
        n = len(p["bars"])
        slot = plot_w / max(1, n)
        bar_w = min(30.0, slot * 0.62)
        chars = max(6, min(26, int(slot / 3.1)))     # 5.5 pt Helvetica: ~3 pt a character

        def y(pct):
            return y0 + plot_h * pct / 100

        for g in (0, 25, 50, 75, 100):
            d.add(Line(left, y(g), left + plot_w, y(g), strokeColor=c(TRACK), strokeWidth=0.5))
            d.add(String(left - 3, y(g) - 2, f"{g}%", fontName="Helvetica", fontSize=5.5,
                         fillColor=c(FAINT), textAnchor="end"))
        pts = []
        for i, b in enumerate(p["bars"]):
            cx = left + slot * (i + 0.5)
            d.add(Rect(cx - bar_w / 2, y0, bar_w, max(0.6, plot_h * b["share"] / 100),
                       fillColor=c(PALETTE[0] if b["vital"] else TONES["neutral"]), strokeColor=None))
            names = mf.wrap_name(b["name"], chars)
            for k, part in enumerate(names):
                d.add(String(cx, y0 - 8 - 6.5 * k, part, fontName="Helvetica", fontSize=5.5,
                             fillColor=c(MUTED), textAnchor="middle"))
            d.add(String(cx, y0 - 8 - 6.5 * len(names) - 1, format_value(b["value"], cfg.get("unit")),
                         fontName="Helvetica-Bold", fontSize=5.5, fillColor=c(INK), textAnchor="middle"))
            pts.append((cx, y(b["cumulative"])))
        d.add(Line(left, y(threshold), left + plot_w, y(threshold), strokeColor=c(FAINT),
                   strokeWidth=0.6, strokeDashArray=[2, 2]))
        d.add(String(left + plot_w, y(threshold) + 2, f"{mf.js_round(threshold)}%",
                     fontName="Helvetica", fontSize=5.5, fillColor=c(FAINT), textAnchor="end"))
        if len(pts) > 1:
            d.add(PolyLine([v for pt in pts for v in pt], strokeColor=c(LINE_INK), strokeWidth=1.2))
        for i, (px, py) in enumerate(pts):
            d.add(Circle(px, py, 1.7, fillColor=c(LINE_INK), strokeColor=c("#ffffff"), strokeWidth=0.6))
            if i == p["vital"] - 1:
                # Up and to the left of the point: the running line rises from the left
                # and leaves to the right, so that corner is free.
                d.add(String(px - 3, py + 3, f"{mf.js_round(p['bars'][i]['cumulative'])}%",
                             fontName="Helvetica-Bold", fontSize=6.5, fillColor=c(INK), textAnchor="end"))
        # Legend on top: the bars' colour, the line.
        ly = height + legend_h - 8
        share_name = "Share of " + (what[4:] if what.startswith("the ") else what)
        d.add(Rect(left, ly, 6, 6, fillColor=c(PALETTE[0]), strokeColor=None))
        d.add(String(left + 9, ly + 1, share_name, fontName="Helvetica", fontSize=6, fillColor=c(MUTED)))
        lx = left + 9 + len(share_name) * 3.1 + 12
        d.add(Line(lx, ly + 3, lx + 10, ly + 3, strokeColor=c(LINE_INK), strokeWidth=1.2))
        d.add(String(lx + 13, ly + 1, "Running total", fontName="Helvetica", fontSize=6, fillColor=c(MUTED)))
        inner.append(d)
        return card(inner, width)

    def bullet(w, data, width):
        from reportlab.graphics.shapes import Drawing, Line, Rect

        from core import management_figures as mf

        cfg = w.config or {}
        vi = _col(data, cfg.get("value_field")) if cfg.get("value_field") else (
            1 if len(data.columns) > 1 else 0)
        if vi < 0:
            return unavailable(w, "This view's fields are not in its data.", width)
        ci = _col(data, cfg.get("category_field")) if cfg.get("category_field") else -1

        def at(r, key, fallback=None):
            i = _col(data, cfg.get(key)) if cfg.get(key) else -1
            return r[i] if i >= 0 else fallback

        per_row = bool(cfg.get("unit_field") or cfg.get("direction_field"))
        shared = (cfg.get("scale") or ("row" if per_row else "shared")) != "row"
        rows = []
        for r in (data.rows if ci >= 0 else data.rows[:1]):
            name = category_label(r[ci], cfg.get("category_format")) if ci >= 0 else (
                cfg.get("label") or w.title)
            sub = at(r, "name_field") if ci >= 0 else None
            rows.append({
                "name": name, "sub": sub if sub not in (None, "") and str(sub) != name else None,
                "value": mf.num(r[vi]), "target": mf.num(at(r, "target_field", cfg.get("target"))),
                "floor": mf.num(at(r, "floor_field", cfg.get("floor"))),
                "max": mf.num(at(r, "max_field")), "unit": at(r, "unit_field", cfg.get("unit")) or "",
                "direction": at(r, "direction_field", cfg.get("direction")) or "higher_better",
                "previous": mf.num(at(r, "previous_field")),
                "reference": mf.num(at(r, "reference_field")),
            })
        unit0 = rows[0]["unit"] if rows else ""
        ref_value = mf.num((cfg.get("reference") or {}).get("value"))
        shared_max = None
        if shared:
            shared_max = mf.num(cfg.get("max")) or mf.scale_max(
                [x for r in rows for x in (r["value"], r["target"], r["floor"], r["reference"])] + [ref_value],
                unit0)
        limit = int(cfg.get("limit") or 12)
        inner_w = width - 14
        verdict_w = min(118.0, inner_w * 0.34)
        track_w = inner_w - verdict_w - 6
        inner = [P(w.title, "card_title")]
        if cfg.get("caption"):
            inner.append(P(cfg["caption"], "small", limit=200))
        value_label, target_label = cfg.get("value_label"), cfg.get("target_label") or "target"
        for r in rows[:limit]:
            mx = shared_max or r["max"] or mf.scale_max([r["value"], r["target"], r["floor"], r["reference"]],
                                                         r["unit"])
            verdict = mf.bullet_verdict(value=r["value"], target=r["target"], floor=r["floor"],
                                        direction=r["direction"], unit=r["unit"], gap=cfg.get("gap") or "difference",
                                        words=cfg.get("words") or {}, fmt=format_value)
            delta = mf.bullet_delta(r["value"], r["previous"], direction=r["direction"], unit=r["unit"],
                                    fmt=format_value)
            right = (f"{value_label} " if value_label else "") + format_value(r["value"], r["unit"])
            if r["target"] is not None:
                right += f" · {target_label} {format_value(r['target'], r['unit'])}"
            markup = escape(right)
            if delta:
                # Helvetica has no arrows: "up 1.9 pts" / "down 1.9 pts", coloured by sense.
                words_ = delta["text"].replace("▲ ", "up ").replace("▼ ", "down ")
                colour = {"good": TONE_TEXT["good"], "bad": TONE_TEXT["act"]}.get(delta["sense"], MUTED)
                markup += f'&nbsp;&nbsp;<font color="{colour}">{escape(words_)}</font>'
            label = r["name"] + (f" · {r['sub']}" if r["sub"] else "")
            head = Table([[P(label, "row", limit=80), Paragraph(markup, styles["row_r"])]],
                         colWidths=[inner_w * 0.5, inner_w * 0.5])
            head.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                      ("TOPPADDING", (0, 0), (-1, -1), 2),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            inner.append(head)
            d = Drawing(track_w, 9)
            d.add(Rect(0, 1, track_w, 7, fillColor=c(TRACK), strokeColor=None))
            for b in mf.bullet_bands(target=r["target"], floor=r["floor"], direction=r["direction"],
                                     maximum=mx):
                d.add(Rect(track_w * b["from"] / 100, 1, track_w * (b["to"] - b["from"]) / 100, 7,
                           fillColor=c(BAND_POOR if b["level"] == "poor" else BAND_FAIR), strokeColor=None))
            vp = mf.position(r["value"], mx)
            if vp is not None:
                d.add(Rect(0, 3, max(0.8, track_w * vp / 100), 3, fillColor=c(PALETTE[0]), strokeColor=None))
            # The row's own reference (its budget), else the board's (Budget 100%).
            rp = (mf.position(r["reference"], mx) if r["reference"] is not None
                  else mf.position(ref_value, mx) if shared else None)
            if rp is not None:
                x = track_w * rp / 100
                d.add(Line(x, 0, x, 9, strokeColor=c(MUTED), strokeWidth=0.5, strokeDashArray=[1.2, 1.2]))
            tp = mf.position(r["target"], mx)
            if tp is not None:
                x = min(track_w - 0.6, max(0.6, track_w * tp / 100))
                d.add(Line(x, 0, x, 9, strokeColor=c(INK), strokeWidth=1.2))
            cell = P(verdict["text"], "verdict_" + verdict["tone"], limit=60) if verdict else P(
                "No figure yet" if r["value"] is None else "", "small")
            line = Table([[d, "", cell]], colWidths=[track_w, 6, verdict_w])
            line.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                      ("TOPPADDING", (0, 0), (-1, -1), 0),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                                      ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
                                     + ([("BACKGROUND", (2, 0), (2, 0), c(TONE_SOFT[verdict["tone"]]))]
                                        if verdict else [])))
            inner.append(line)
        if len(rows) > limit:
            inner.append(P(f"and {len(rows) - limit} more", "small"))
        if cfg.get("reference_field") and rows:
            inner.append(P(f"Dashed line: the {cfg.get('reference_label') or 'reference'}. "
                           f"Tick: {target_label}.", "small"))
        elif shared and rows:
            ref_label = (cfg.get("reference") or {}).get("label")
            scale = f"Scale 0 to {format_value(shared_max, unit0)}"
            if ref_value is not None:
                scale += f". Dashed line: {ref_label + ' ' if ref_label else ''}{format_value(ref_value, unit0)}"
            inner.append(P(scale + ". Tick: " + target_label + ".", "small"))
        if not rows:
            inner.append(P(cfg.get("empty") or "Nothing recorded for this selection.", "small"))
        return card(inner, width)

    def text_card(w, width):
        cfg = w.config or {}
        return card([P(w.title, "card_title"), P(cfg.get("content") or "", "label", limit=1200)],
                    width)

    def render_widget(w, width):
        kind = w.widget_type
        if kind == "text":
            return text_card(w, width)
        data = widget_data(w, owner)
        if data.error:
            return unavailable(w, data.error, width)
        counted["rendered"] += 1
        if kind == "kpi":
            return kpi(w, data, width)
        if kind in ("bar_list", "pie"):
            return ranked(w, data, width)
        if kind == "funnel":
            return ranked(w, data, width, keep_order=True)
        if kind == "progress":
            return progress(w, data, width)
        if kind == "pareto":
            return pareto(w, data, width)
        if kind == "bullet":
            return bullet(w, data, width)
        if kind == "table":
            return table(w, data, width)
        if kind in ("bar", "line", "area"):
            return chart(w, data, width)
        counted["rendered"] -= 1
        return card([P(w.title, "card_title"), P("This view is on the board.", "small")],
                    width)

    # ---- the story -------------------------------------------------------------
    story = []
    stale = bool(freshness and freshness.get("kind") == "stale")
    as_of = format_stamp((freshness or {}).get("data_as_of")) if freshness else ""
    story.append(P(dashboard.name, "title"))
    if dashboard.description:
        story.append(P(dashboard.description, "sub", limit=400))
    if stale:
        story.append(P(f"These figures are from {as_of}. The daily update is overdue, so later "
                       "changes are not shown." if as_of else
                       "The daily update is overdue, so later changes are not shown.", "warn"))
    story.append(Spacer(1, 3 * mm))

    pending_heading = None
    for kind, item in plan(widgets):
        if kind == "section":
            if pending_heading:
                story.append(KeepTogether(pending_heading))
            content = (item.config or {}).get("content") or ""
            pending_heading = [P(item.title, "section")]
            if content:
                pending_heading.append(P(content, "section_q", limit=400))
            pending_heading.append(Spacer(1, 2 * mm))
            continue
        units = [max(1, min(GRID_COLUMNS, int(w.w or GRID_COLUMNS))) for w in item]
        free = avail - gap * (len(item) - 1)
        widths = [free * u / sum(units) if sum(units) >= GRID_COLUMNS else free * u / GRID_COLUMNS
                  for u in units]
        cells = [render_widget(w, wd) for w, wd in zip(item, widths)]
        col_widths = []
        row_cells = []
        for i, (cell, wd) in enumerate(zip(cells, widths)):
            if i:
                row_cells.append("")
                col_widths.append(gap)
            row_cells.append(cell)
            col_widths.append(wd)
        row = Table([row_cells], colWidths=col_widths, hAlign="LEFT")
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        block = (pending_heading or []) + [row, Spacer(1, gap)]
        pending_heading = None
        story.append(KeepTogether(block))
    if pending_heading:
        story.append(KeepTogether(pending_heading))

    # ---- header and footer on every page ----------------------------------------
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total):
            self.setFont("Helvetica", 7.5)
            self.setFillColor(c(MUTED))
            if product:
                self.drawString(margin, page_h - 8 * mm, product)
            right = f"Data as of {as_of}" if as_of else ""
            if stale and right:
                right += " · update overdue"
            if right:
                self.setFillColor(c(STALE if stale else MUTED))
                self.drawRightString(page_w - margin, page_h - 8 * mm, right)
            self.setFillColor(c(MUTED))
            left = " · ".join(x for x in (generated_at and f"Generated {generated_at}",
                                          generated_by, footer_text) if x)
            self.drawString(margin, 7 * mm, left[:160])
            self.drawRightString(page_w - margin, 7 * mm,
                                 f"Page {self._pageNumber} of {total}")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=margin,
                            rightMargin=margin, topMargin=13 * mm, bottomMargin=12 * mm,
                            title=dashboard.name, author=product or "")
    doc.build(story, canvasmaker=NumberedCanvas)
    content = buf.getvalue()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", content))
    return BoardPdf(content=content, pages=pages, widgets=counted["rendered"],
                    unavailable=counted["unavailable"], data_as_of=as_of or "", stale=stale)
