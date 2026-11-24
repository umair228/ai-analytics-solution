"""Read-only lineage + freshness API — where a number came from, and how old it is.

The replication app had no views, no urls and no admin: a whole data pipeline with
watermarks, marts and staleness tracking that was completely invisible to the people
being asked to trust its numbers. "Where did this come from and how fresh is it?" is
the first question an enterprise buyer asks, and DIS could not answer it on screen.

This is deliberately read-only. Triggering a sync is an operator action with a real
cost against the LIMS; it belongs in the scheduler, not behind a browser button.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from connections.constants import SourceType
from connections.access import visible_datasources
from connections.models import DataSource

from .freshness import describe
from .marts import mart_definitions
from .models import ReplicationState


# One definition, in connections/access.py. This was copy-pasted into four apps.
_visible_datasources = visible_datasources


def _table_node(state) -> dict:
    synced = state.last_synced_at
    return {
        "table": state.table_name,
        "cadence": state.cadence,
        "row_count": state.row_count,
        "watermark_column": state.watermark_column or None,
        "high_watermark": state.high_watermark or None,
        "last_synced_at": synced.isoformat() if synced else None,
        "last_full_at": state.last_full_at.isoformat() if state.last_full_at else None,
        "error": state.last_error or "",
        "stale": state.is_stale(),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lineage(request):
    """The pipeline as a graph the UI can draw: source → tables → marts → freshness.

    Returns one entry per (source → target) replica pair the caller can see, plus a
    bare "live" entry for every queryable datasource that nothing replicates into —
    so a deployment with no replica still renders honestly rather than empty.
    """
    visible = list(_visible_datasources(request.user))
    visible_ids = {d.id for d in visible}

    states = (ReplicationState.objects
              .filter(Q(source_id__in=visible_ids) | Q(target_id__in=visible_ids))
              .select_related("source", "target"))

    pipelines: dict[tuple, dict] = {}
    replicated_targets = set()
    for st in states:
        key = (st.source_id, st.target_id)
        replicated_targets.add(st.target_id)
        node = pipelines.setdefault(key, {
            "source": {"id": st.source.id, "name": st.source.name,
                       "type": st.source.source_type},
            "target": {"id": st.target.id, "name": st.target.name,
                       "type": st.target.source_type,
                       "schema": st.target.schema_name or None},
            "tables": [],
            "marts": [],
        })
        node["tables"].append(_table_node(st))

    for (_, target_id), node in pipelines.items():
        target = next((d for d in visible if d.id == target_id), None)
        fresh = describe(target) if target else None
        node["freshness"] = fresh.as_provenance() if fresh else {"freshness": "unknown"}
        node["stale_tables"] = fresh.stale_tables if fresh else []
        node["tables"].sort(key=lambda t: t["table"])
        if target:
            # The marts are derived, not replicated, so they have no ReplicationState.
            # Name them from the same definition the builder uses — a mart the UI
            # invents would be a lie the moment marts.py changes.
            node["marts"] = [
                {"name": name, "derived_from": ["SAMPLE", "RESULT"]}
                for name, _sql in mart_definitions(target.source_type,
                                                   target.schema_name or None)
            ]

    live = [
        {
            "source": None,
            "target": {"id": d.id, "name": d.name, "type": d.source_type,
                       "schema": d.schema_name or None},
            "tables": [],
            "marts": [],
            "freshness": describe(d).as_provenance(),
            "stale_tables": [],
        }
        for d in visible if d.id not in replicated_targets
    ]

    return Response({
        "pipelines": sorted(pipelines.values(), key=lambda p: p["target"]["name"]),
        "live_sources": sorted(live, key=lambda p: p["target"]["name"]),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def freshness(request):
    """Freshness of one datasource (?datasource=<id>) — the badge on an answer."""
    ds_id = request.query_params.get("datasource")
    ds = _visible_datasources(request.user).filter(pk=ds_id).first() if ds_id else None
    if ds is None:
        return Response({"detail": "Unknown or inaccessible datasource."}, status=404)
    f = describe(ds)
    return Response({
        "datasource": {"id": ds.id, "name": ds.name},
        "stale_tables": f.stale_tables,
        **f.as_provenance(),
    })
