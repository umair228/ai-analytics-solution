"""Pareto and bullet figures for the board PDF: the same numbers and words as the board.

Mirrors src/lib/pareto.js and src/lib/bullet.js in interactive-analytics-dse (their
tests and core/tests_management_figures.py check the same cases), so a scheduled PDF
says "3 of 9 categories make up 78% of the NCRs" or "18 pts below target" exactly as the
screen does. Pure functions; formatting of values is passed in (board_render.format_value).
"""
from __future__ import annotations

import math

OTHERS = "All others"
STEPS = (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10)


def num(value):
    """A blank is not a zero: None, "" and non-numbers are None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def js_round(x: float) -> int:
    """Math.round: halves go up (Python's round() goes to even)."""
    return math.floor(x + 0.5)


def _round1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


def _plain1(x: float) -> str:
    r = _round1(x)
    return f"{int(r):,}" if r == int(r) else f"{r:,.1f}"


def _pts(d: float) -> str:
    """Percentage points to one decimal, or two when one would round a real change to 0."""
    if d < 0.095:
        r = math.floor(d * 100 + 0.5) / 100
        return f"{r:g} {'pt' if r == 1 else 'pts'}"
    text = _plain1(d)
    return f"{text} {'pt' if text == '1' else 'pts'}"


def _is_pct(unit) -> bool:
    return unit in ("%", "percent")


# ──────────────────────────────────────────────────────────────────────────────
#  Pareto
# ──────────────────────────────────────────────────────────────────────────────

def pareto_bars(rows, *, limit=10, threshold=80) -> dict:
    """rows [{name, value, raw?, sub?}] -> {bars, total, count, reach, vital, vital_share}.
    reach: how many categories it takes to reach the threshold, over all of them; vital:
    the ones of those that are drawn (paretoBars)."""
    merged: dict = {}
    for r in rows or []:
        name = r.get("name")
        if name is None or name == "":
            continue
        v = num(r.get("value"))
        if v is None or v <= 0:
            continue
        key = str(name)
        if key in merged:
            merged[key]["value"] += v
        else:
            merged[key] = {"name": key, "raw": r.get("raw", name), "sub": r.get("sub"), "value": v}
    ordered = sorted(merged.values(), key=lambda b: (-b["value"], b["name"]))
    total = sum(b["value"] for b in ordered)
    if not total:
        return {"bars": [], "total": 0, "count": 0, "reach": 0, "vital": 0, "vital_share": 0}
    try:
        lim = max(2, int(float(limit or 10)))
    except (TypeError, ValueError):
        lim = 10
    kept = ordered[: lim - 1] if len(ordered) > lim else ordered
    rest = ordered[lim - 1:] if len(ordered) > lim else []
    bars, run = [], 0.0
    for b in kept:
        run += b["value"]
        bars.append({**b, "share": 100 * b["value"] / total, "cumulative": 100 * run / total,
                     "others": False})
    if rest:
        v = sum(b["value"] for b in rest)
        bars.append({"name": f"{OTHERS} ({len(rest)})", "raw": None, "sub": None, "value": v,
                     "share": 100 * v / total, "cumulative": 100.0, "others": True})
    t = num(threshold)
    t = 80.0 if t is None else t
    acc, reach = 0.0, len(ordered)
    for i, b in enumerate(ordered):
        acc += b["value"]
        if 100 * acc / total >= t - 1e-9:
            reach = i + 1
            break
    vital = min(reach, len(kept))
    for i, b in enumerate(bars):
        b["vital"] = i < vital
    return {"bars": bars, "total": total, "count": len(ordered), "reach": reach, "vital": vital,
            "vital_share": bars[vital - 1]["cumulative"] if vital else 0}


def pareto_summary(p, *, items="categories", what="the total", threshold=80):
    if not p or not p["total"]:
        return None
    t = num(threshold)
    t = 80 if t is None else (int(t) if t == int(t) else t)
    if p["count"] == 1:
        return f"All of {what} is in one of the {items}."
    if p["reach"] > p["count"] / 2:
        return f"Spread out: it takes {p['reach']} of {p['count']} {items} to reach {t}% of {what}."
    if p["reach"] <= p["vital"]:
        verb = "makes" if p["reach"] == 1 else "make"
        return (f"{p['reach']} of {p['count']} {items} {verb} up "
                f"{js_round(p['vital_share'])}% of {what}.")
    return (f"The top {p['vital']} of {p['count']} {items} make up {js_round(p['vital_share'])}% "
            f"of {what}; it takes {p['reach']} to reach {t}%.")


def wrap_name(name, chars: int) -> list:
    """A category name on at most two lines of `chars` characters, broken between words;
    a short last word ("01") survives a cut (wrapName)."""
    words = str(name).split()
    lines, line = [], ""
    for w in words:
        if not line:
            line = w
        elif len(f"{line} {w}") <= chars:
            line = f"{line} {w}"
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)

    def cut(s):
        return f"{s[: max(1, chars - 1)].rstrip()}…" if len(s) > chars else s

    if len(lines) <= 2:
        return [cut(x) for x in lines]
    rest, last = " ".join(lines[1:]), words[-1]
    second = (f"{rest[: max(1, chars - len(last) - 2)].rstrip()}… {last}"
              if len(rest) > chars and len(last) <= chars / 2 else cut(rest))
    return [cut(lines[0]), second]


# ──────────────────────────────────────────────────────────────────────────────
#  Bullet
# ──────────────────────────────────────────────────────────────────────────────

def nice_ceil(x) -> float:
    if not (x and x > 0):
        return 1
    p = 10 ** math.floor(math.log10(x))
    f = x / p
    return next((s for s in STEPS if s >= f - 1e-9), 10) * p


def scale_max(values, unit="") -> float:
    nums = [n for n in (num(v) for v in values) if n is not None]
    top = max([0.0] + nums)
    if _is_pct(unit) and 40 <= top < 100:
        return 100
    return nice_ceil(top * 1.05)


def position(value, maximum):
    """0..100 (% of the track), or None."""
    v = num(value)
    if v is None or not (maximum and maximum > 0):
        return None
    return min(100.0, max(0.0, v / maximum * 100))


def bullet_bands(*, target, floor=None, direction="higher_better", maximum) -> list:
    t, f = position(target, maximum), position(floor, maximum)
    if t is None:
        return []
    out = []
    if direction == "lower_better":
        if f is not None and f > t:
            out += [(t, f, "fair"), (f, 100.0, "poor")]
        else:
            out.append((t, 100.0, "fair"))
    elif f is not None and f < t:
        out += [(0.0, f, "poor"), (f, t, "fair")]
    else:
        out.append((0.0, t, "fair"))
    return [{"from": a, "to": b, "level": lvl} for a, b, lvl in out if b > a]


def _plain(n: float, unit) -> str:
    if unit == "ratio":   # three decimals when two would show a real change as 0.00
        return f"{n:.3f}" if n < 0.0095 else f"{n:.2f}"
    return _plain1(n)


def _sized(d: float, unit, fmt) -> str:
    if _is_pct(unit):
        return _pts(d)
    if unit == "ratio" or not fmt:
        return _plain(d, unit)
    return fmt(d, unit)


def gap_text(value, target, *, unit="", gap="difference", fmt=None) -> str:
    v, t = num(value), num(target)
    if v is None or t is None:
        return ""
    diff = abs(v - t)
    if gap == "relative" and t != 0:
        return f"{js_round(abs(v / t - 1) * 100)}%"
    return _sized(diff, unit, fmt)


def bullet_verdict(*, value, target, floor=None, direction="higher_better", unit="",
                   gap="difference", words=None, fmt=None):
    """{"tone": good | watch | act, "text"} or None (no figure or no target)."""
    v, t, f = num(value), num(target), num(floor)
    if v is None or t is None:
        return None
    words = words or {}
    lower = direction == "lower_better"

    def better(a, b):
        return a <= b if lower else a >= b

    g = gap_text(v, t, unit=unit, gap=gap, fmt=fmt)

    def fill(s):
        return str(s).replace("{gap}", g, 1)

    if better(v, t):
        return {"tone": "good", "text": fill(words.get("met") or ("Within target" if lower else "On target"))}
    miss = words.get("miss") or ("{gap} over target" if lower else "{gap} below target")
    if f is not None and better(v, f):
        return {"tone": "watch", "text": fill(words.get("watch") or miss)}
    return {"tone": "act", "text": fill(miss)}


def bullet_delta(value, previous, *, direction="higher_better", unit="", fmt=None):
    """Change since the period before: {"text": "▲ 1.9 pts", "sense"} or None."""
    v, p = num(value), num(previous)
    if v is None or p is None:
        return None
    diff = v - p
    if abs(diff) < 1e-9:
        return {"text": "no change", "sense": "neutral"}
    arrow = "▲" if diff > 0 else "▼"
    size = _sized(abs(diff), unit, fmt)
    if direction == "lower_better":
        sense = "good" if diff < 0 else "bad"
    elif direction == "higher_better":
        sense = "good" if diff > 0 else "bad"
    else:
        sense = "neutral"
    return {"text": f"{arrow} {size}", "sense": sense}
