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

from django.conf import settings
from django.core.cache import cache

from querybuilder.executor import QueryError, assert_read_only
from texttosql.execution import execute_with_deadline

from .layer import get_semantic_layer
from .metric_sql import render as render_sql

logger = logging.getLogger(__name__)

CACHE_TTL = 60  # seconds


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
    """good | warn | bad | none — never invent a threshold nobody agreed to."""
    if value is None or tile.target is None or tile.direction == "neutral":
        return "none"
    if tile.direction == "lower_better":
        if value <= tile.target:
            return "good"
        return "warn" if value <= tile.target * 1.25 else "bad"
    if value >= tile.target:
        return "good"
    return "warn" if value >= tile.target * 0.75 else "bad"


def _run_tile(datasource, tile) -> dict:
    out = {
        "key": tile.key, "label": tile.label, "description": tile.description,
        "unit": tile.unit, "direction": tile.direction, "target": tile.target,
        "value": None, "grade": "none", "error": "", "sql": "",
    }
    try:
        sql = render_sql(tile.sql, datasource)
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

    # Cache per (datasource, role) — the tile SET depends on the role, so a shared
    # key would serve a section head the lab manager's tiles.
    key = f"lab-health:{datasource.id}:{role}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    fresh = describe(datasource)
    result = {
        "configured": True,
        "datasource": {"id": datasource.id, "name": datasource.name},
        "role": role,
        "tiles": [_run_tile(datasource, t) for t in tiles],
        **fresh.as_provenance(),
    }
    cache.set(key, result, CACHE_TTL)
    return result
