"""Read-only data-dictionary + certified-metrics API.

The entire trust story rests on this layer — certified metrics are the QA-approved
SQL that answers a hot question with the LLM writing nothing at all — and it had no
UI whatsoever. A QA manager could not see which metrics existed, read their SQL, or
check what phrasings route to them. "Trust us, there's a certified layer" is not a
control; a control is one you can show someone.

Read-only on purpose. Metrics are version-controlled YAML loaded by
`load_semantic_layer`, and that is the point: a metric that can be edited in a
browser is no longer certified. Editing belongs in review, not in a text box.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from connections.constants import SourceType
from connections.access import visible_datasources
from connections.models import DataSource

from .health import lab_health
from .layer import get_semantic_layer, render_for



# One definition, in connections/access.py. This was copy-pasted into four apps.
_visible_datasources = visible_datasources


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def health(request):
    """The Lab Health home page: certified tiles, scoped to the caller's lab role.

    Degrades honestly. If no datasource is reachable, or the datasource has no
    curated layer, it says so rather than rendering zeros — a lab manager reading
    "0 off-spec" because a query failed is worse off than one reading nothing.
    """
    ds_id = request.query_params.get("datasource")
    return Response(lab_health(request.user, ds_id))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def layers(request):
    """Which datasources have a curated semantic layer, and how big it is."""
    out = []
    for ds in _visible_datasources(request.user):
        layer = get_semantic_layer(ds)
        out.append({
            "datasource": {"id": ds.id, "name": ds.name, "type": ds.source_type},
            "present": layer.is_present(),
            "tables": len(layer.tables),
            "columns": sum(len(t.columns) for t in layer.tables.values()),
            "joins": len(layer.joins),
            "metrics": len(layer.metrics),
        })
    return Response({"layers": out})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def layer_detail(request, pk):
    """The full data dictionary for one datasource: tables, columns, joins, metrics.

    Metric SQL is returned BOTH as authored (portable, with macros) and as it will
    actually run against this datasource's dialect. Showing only the portable form
    would hide the query that really executes; showing only the rendered form would
    hide that one definition serves every dialect. An auditor wants both.
    """
    ds = _visible_datasources(request.user).filter(pk=pk).first()
    if ds is None:
        return Response({"detail": "Unknown or inaccessible datasource."}, status=404)

    layer = get_semantic_layer(ds)

    tables = [
        {
            "name": t.name,
            "description": t.description,
            "in_scope": t.in_scope,
            "row_count": t.row_count,
            "synonyms": t.synonyms,
            "columns": [
                {
                    "name": c.name,
                    "description": c.description,
                    "encoding": c.encoding,          # e.g. "T=in-spec, F=off-spec"
                    "needs_cast": c.needs_cast,      # NUMERIC_ENTRY is text in LabWare
                    "do_not_group": c.do_not_group,  # INSTRUMENT is ~empty in EGPC
                    "null_fraction": c.null_fraction,
                    "synonyms": c.synonyms,
                }
                for c in sorted(t.columns.values(), key=lambda c: c.name)
            ],
        }
        for t in sorted(layer.tables.values(), key=lambda t: t.name)
    ]

    metrics = [
        {
            "key": m.key,
            "name": m.name,
            "description": m.description,
            "row_limit": m.row_limit,
            "synonyms": m.synonyms,
            # KPI metadata (P5): what the number is, who owns it, how it is judged.
            "kpi_id": m.kpi_id, "unit": m.unit, "direction": m.direction,
            "owner": m.owner, "brd_ref": m.brd_ref, "thresholds": m.thresholds,
            "status": m.status, "classes": m.classes, "scoped": m.scoped,
            "sql_authored": m.sql,
            "sql_rendered": render_for(layer, m.sql, ds),
        }
        for m in sorted(layer.metrics, key=lambda m: m.key)
    ]

    # The Lab Health tiles are certified SQL too. Exposing them here is what makes
    # the "Certified" chip on the home page checkable rather than a slogan: a QA
    # manager can read the exact query behind the off-spec rate they were shown.
    health = [
        {
            "key": h.key,
            "name": h.label,
            "description": h.description,
            "unit": h.unit,
            "direction": h.direction,
            "target": h.target,
            "warn": h.warn,
            "critical": h.critical,
            "classes": h.classes,
            "roles": h.roles,
            "sql_authored": h.sql,
            "sql_rendered": render_for(layer, h.sql, ds),
        }
        for h in sorted(layer.health, key=lambda h: (h.position, h.key))
    ]

    return Response({
        "datasource": {"id": ds.id, "name": ds.name, "type": ds.source_type,
                       "schema": ds.schema_name or None},
        "present": layer.is_present(),
        "notes": layer.notes,
        "tables": tables,
        "joins": [{"left": j.left, "right": j.right, "on": j.on,
                   "description": j.description} for j in layer.joins],
        "metrics": metrics,
        "health": health,
    })
