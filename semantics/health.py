"""Lab Health — run the curated home-page tiles against the lab's data.

The home page is the first thing a lab manager sees, and it used to show counts of
*app objects*: connections, datasets, dashboards. All true, and of no interest to
anyone who runs a laboratory.

These tiles show the lab. They go through exactly the same machinery as any other
answer — curated SQL, rendered per-dialect, read-only-asserted, deadline-bounded,
stamped with freshness — because the home page must not be the one screen in the
product where numbers appear without provenance.

Cached briefly: six queries against the LIMS on every page load would be a poor
trade for numbers that move on a replication cadence measured in hours.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from querybuilder.executor import QueryError, assert_read_only
from texttosql.execution import execute_with_deadline

from .access import cleared_classes
from .layer import get_semantic_layer, layer_version, render_for

logger = logging.getLogger(__name__)

CACHE_TTL = 60  # seconds
# "Since last week": the newest recorded value at least this much older than the data
# on screen (a week less half a day, so a nightly copy taken a little earlier counts).
COMPARE_AFTER = timedelta(days=7) - timedelta(hours=12)
TREND_POINTS = 30           # nightly values kept for the small trend line
# A restored snapshot changes only when it is promoted, and the promotion changes its
# restore_id — which is part of the cache key — so its tiles can be kept far longer.
SNAPSHOT_CACHE_TTL = 6 * 3600


def _scalar(payload, unit="count"):
    """A tile is one row, one number. Anything else is a broken tile, not a value."""
    rows = payload.get("rows") or []
    if not rows:
        return None
    row = rows[0]
    value = row[0] if isinstance(row, (list, tuple)) else next(iter(row.values()), None)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    # A count of samples is an integer. Reporting "300.0 samples" is the kind of
    # small wrongness that makes a careful reader distrust the careful numbers too.
    return int(value) if unit == "count" else value


def _grade(value, tile):
    """good | warn | bad | none — never invent a threshold nobody agreed to.

    Explicit ``warn``/``critical`` bands (agreed by the KPI owner) win; otherwise the
    tile grades against ``target`` with the default bands; with neither, it is shown
    but not graded.
    """
    if value is None or tile.direction == "neutral":
        return "none"
    warn, critical = getattr(tile, "warn", None), getattr(tile, "critical", None)
    if warn is not None or critical is not None:
        lower = tile.direction == "lower_better"

        def at_or_beyond(v, band):
            return v >= band if lower else v <= band

        if critical is not None and at_or_beyond(value, critical):
            return "bad"
        if warn is not None and at_or_beyond(value, warn):
            return "warn"
        return "good"
    if tile.target is None:
        return "none"
    if tile.direction == "lower_better":
        if value <= tile.target:
            return "good"
        return "warn" if value <= tile.target * 1.25 else "bad"
    if value >= tile.target:
        return "good"
    return "warn" if value >= tile.target * 0.75 else "bad"


def _run_tile(datasource, tile, layer=None, cleared=frozenset()) -> dict:
    out = {
        "key": tile.key, "label": tile.label, "description": tile.description,
        "unit": tile.unit, "direction": tile.direction, "target": tile.target,
        "warn": getattr(tile, "warn", None), "critical": getattr(tile, "critical", None),
        "group": getattr(tile, "group", ""), "board": getattr(tile, "board", ""),
        "kpi_id": getattr(tile, "kpi_id", ""),
        "value": None, "grade": "none", "error": "", "sql": "", "restricted": False,
    }
    if not set(getattr(tile, "classes", None) or ()) <= set(cleared):
        # Classified (cost / bid-confidential) and this viewer is not cleared: say so,
        # and do not run the SQL at all.
        out["restricted"] = True
        return out
    try:
        sql = render_for(layer or get_semantic_layer(datasource), tile.sql, datasource)
        assert_read_only(sql)
        out["sql"] = sql
        payload = execute_with_deadline(datasource, sql)
        out["value"] = _scalar(payload, tile.unit)
        out["grade"] = _grade(out["value"], tile)
    except QueryError as exc:
        # A broken tile must read as broken, not as zero. A lab manager seeing "0
        # off-spec" because the query failed is worse than seeing no number at all.
        logger.warning("Health tile '%s' failed on '%s': %s", tile.key, datasource.name, exc)
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad tile must not blank the page
        logger.exception("Health tile '%s' raised", tile.key)
        out["error"] = str(exc)
    return out


def _as_dt(text):
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else timezone.make_aware(dt)


def attach_history(datasource, tiles, current_as_of) -> None:
    """Give each tile its recorded nightly values (``trend``) and the value from about a
    week before the data on screen (``previous``), from KpiObservation (P5).

    Only tiles shown with a value get history: a restricted tile the viewer is not
    cleared for stays blank (the recorder stores every value; reading back goes through
    the same clearance as the live tile), and a broken tile has nothing to compare.
    Failed recordings are skipped, never read as zero.
    """
    from .models import KpiObservation

    shown = {t["key"]: t for t in tiles
             if not t.get("restricted") and not t.get("error") and t.get("value") is not None}
    for t in tiles:
        t["trend"], t["previous"] = [], None
    if not shown:
        return
    now = _as_dt(current_as_of) or timezone.now()
    rows = (KpiObservation.objects
            .filter(datasource=datasource, kind=KpiObservation.Kind.TILE,
                    key__in=list(shown), status=KpiObservation.Status.OK,
                    value__isnull=False)
            .order_by("observed_at")
            .values_list("key", "value", "data_as_of", "observed_at"))
    by_key = {}
    for key, value, as_of, observed in rows:
        by_key.setdefault(key, []).append((_as_dt(as_of) or observed, value, as_of))
    for key, points in by_key.items():
        tile = shown[key]
        tile["trend"] = [{"value": v, "data_as_of": a} for _, v, a in points[-TREND_POINTS:]]
        older = [p for p in points if p[0] <= now - COMPARE_AFTER]
        if older:
            when, value, as_of = older[-1]
            tile["previous"] = {"value": value, "data_as_of": as_of,
                                "days": max(1, round((now - when).total_seconds() / 86400))}


def resolve_datasource(user, datasource_id=None):
    """The datasource the home page reports on.

    Prefers an explicit id, then DSE_TEXTTOSQL_DEFAULT_DATASOURCE (the same one the
    agent answers from, so the home page and the chat cannot disagree), then the
    caller's only accessible database.
    """
    from ai.tools import _accessible_datasources

    qs = _accessible_datasources(user)
    if datasource_id:
        return qs.filter(pk=datasource_id).first()

    default = str(getattr(settings, "DSE_TEXTTOSQL_DEFAULT_DATASOURCE", "") or "").strip()
    if default:
        ds = qs.filter(pk=default).first() if default.isdigit() else None
        return ds or qs.filter(name__iexact=default).first() or qs.first()
    return qs.first()


def lab_health(user, datasource_id=None) -> dict:
    """Health tiles visible to ``user``'s role, with the data's freshness."""
    from replication.freshness import describe

    datasource = resolve_datasource(user, datasource_id)
    if datasource is None:
        return {"configured": False, "reason": "no_datasource", "tiles": []}

    layer = get_semantic_layer(datasource)
    role = getattr(user, "role", "") or ""
    tiles = [t for t in sorted(layer.health, key=lambda t: (t.position, t.key))
             if t.visible_to(role)]
    if not tiles:
        return {
            "configured": False,
            "reason": "no_health_tiles" if layer.is_present() else "no_semantic_layer",
            "datasource": {"id": datasource.id, "name": datasource.name},
            "tiles": [],
        }

    # Cache per (datasource, layer, snapshot, role, clearance): the tile SET depends on
    # the role and the VALUES shown depend on clearance, so a shared key would serve a
    # section head the lab manager's tiles, or an uncleared viewer a cost figure. The
    # layer's load time is in the key so a reloaded spec shows at once, not hours later.
    cleared = cleared_classes(user)
    restore_id = str((getattr(datasource, "options", None) or {}).get("restore_id") or "")
    key = (f"lab-health:{datasource.id}:{layer_version(datasource)}:"
           f"{restore_id or 'live'}:{role}:{','.join(sorted(cleared))}")
    cached = cache.get(key)
    if cached is not None:
        return cached

    fresh = describe(datasource)
    result = {
        "configured": True,
        "datasource": {"id": datasource.id, "name": datasource.name},
        "role": role,
        "tiles": [_run_tile(datasource, t, layer, cleared) for t in tiles],
        **fresh.as_provenance(),
    }
    attach_history(datasource, result["tiles"], fresh.data_as_of)
    cache.set(key, result, SNAPSHOT_CACHE_TTL if restore_id else CACHE_TTL)
    return result
