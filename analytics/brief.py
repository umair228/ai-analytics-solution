"""The nightly Lab Brief — the first thing DIS does without being asked.

Everything else in the product is pull: a user opens a page, types a question, clicks
Run. Nothing has ever run on its own behalf. That is the whole difference between a
tool and a colleague, and it is the "AI-driven decision-making" claim that the market
makes and almost nobody delivers.

The brief is deliberately boring in its construction and interesting in its content:

* **The engines compute; the model only narrates.** Every number below comes from the
  certified health tiles, real alert events, and real replica freshness. The LLM is
  handed those numbers and asked to write five bullets. It is never asked what the
  numbers are. If the LLM is unavailable the brief still sends, from a template —
  because a brief that only arrives when the GPU is up is not a brief.
* **It says when it has nothing to say.** A digest that manufactures urgency every
  night to justify itself gets filtered to a folder within a fortnight.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_ALERTS = 8


def gather(user, datasource_id=None) -> dict:
    """The facts. No model involved — this is the part that must be true."""
    from datasets.models import AlertEvent
    from semantics.health import lab_health

    health = lab_health(user, datasource_id)

    events = list(
        AlertEvent.objects
        .filter(alert__owner=user, acknowledged=False)
        .select_related("alert", "alert__dataset")
        .order_by("-created_at")[:MAX_ALERTS]
    )

    tiles = health.get("tiles") or []
    breaches = [t for t in tiles if t.get("grade") == "bad"]
    warnings = [t for t in tiles if t.get("grade") == "warn"]
    broken = [t for t in tiles if t.get("error")]

    return {
        "generated_at": timezone.now().isoformat(),
        "user": getattr(user, "username", ""),
        "role": getattr(user, "role", ""),
        "configured": bool(health.get("configured")),
        "datasource": health.get("datasource"),
        "freshness": health.get("freshness"),
        "freshness_detail": health.get("freshness_detail"),
        "data_as_of": health.get("data_as_of"),
        "tiles": tiles,
        "breaches": breaches,
        "warnings": warnings,
        "broken_tiles": broken,
        "alerts": [
            {
                "name": e.alert.name,
                "kind": e.alert.kind,
                "dataset": e.alert.dataset.name,
                "message": e.message,
                "when": e.created_at.isoformat(),
                "rules": (e.detail or {}).get("rejections_by_rule") or {},
                "error_types": (e.detail or {}).get("error_types") or [],
            }
            for e in events
        ],
    }


def _nothing_to_report(facts) -> bool:
    return not (facts["breaches"] or facts["warnings"] or facts["alerts"]
                or facts["broken_tiles"] or facts["freshness"] == "stale")


def _fallback(facts) -> str:
    """The brief the LLM would have written, written without it.

    This is not a degraded mode to apologise for. A digest that only arrives when the
    GPU is up is not a digest, and every number here is the same number the model
    would have been handed.
    """
    if not facts["configured"]:
        return "Lab health is not configured for this data source, so there is nothing to report."

    lines = []
    if _nothing_to_report(facts):
        lines.append("• Nothing needs your attention. Every health metric is within target, "
                     "there are no unacknowledged alerts, and the data is current.")
    for t in facts["breaches"]:
        unit = {"percent": "%", "hours": " h"}.get(t["unit"], "")
        arrow = "above" if t["direction"] == "lower_better" else "below"
        lines.append(f"• {t['label']} is {t['value']}{unit} — {arrow} the target of "
                     f"{t['target']}{unit}.")
    for t in facts["warnings"]:
        unit = {"percent": "%", "hours": " h"}.get(t["unit"], "")
        lines.append(f"• {t['label']} is {t['value']}{unit}, approaching its target of "
                     f"{t['target']}{unit}.")
    for a in facts["alerts"][:5]:
        lines.append(f"• Alert '{a['name']}' fired: {a['message'].split('[')[0].strip()}")
    for t in facts["broken_tiles"]:
        lines.append(f"• {t['label']} could not be computed — the query failed. "
                     "Treat its absence as unknown, not as zero.")
    if facts["freshness"] == "stale":
        lines.append(f"• ⚠ {facts['freshness_detail']}")
    return "\n".join(lines)


def narrate(facts) -> str:
    """Hand the LLM the numbers and ask for five bullets. Never ask it for a number."""
    from ai.client import run_chat
    from ai.providers.base import AINotConfigured

    if not facts["configured"]:
        return _fallback(facts)

    tiles = "\n".join(
        f"- {t['label']}: {t['value']}{'%' if t['unit'] == 'percent' else ''}"
        f"{' hours' if t['unit'] == 'hours' else ''}"
        f"{f' (target {t['target']}, {t['direction']})' if t.get('target') is not None else ''}"
        f"{f' [{t['grade'].upper()}]' if t.get('grade') != 'none' else ''}"
        f"{' [UNAVAILABLE — query failed]' if t.get('error') else ''}"
        for t in facts["tiles"]
    ) or "- (no health tiles)"

    alerts = "\n".join(f"- {a['name']} ({a['kind']}): {a['message']}"
                       for a in facts["alerts"]) or "- (no unacknowledged alerts)"

    prompt = f"""You are writing the overnight brief for a laboratory manager.

LAB HEALTH (as of {facts.get('data_as_of') or 'now'}; data freshness: {facts['freshness']}):
{tiles}

UNACKNOWLEDGED ALERTS:
{alerts}

DATA FRESHNESS: {facts.get('freshness_detail') or 'live'}

Write at most FIVE bullets, most urgent first. Rules you must follow:
- Use ONLY the numbers above. Do not compute, estimate, extrapolate or invent any figure.
- Lead with what needs a decision today, not with what is fine.
- If a control-rule alert indicates SYSTEMATIC error, say it points to a shift or drift
  (calibration, reagent lot, instrument setting). If RANDOM, say it points to imprecision.
- A metric marked UNAVAILABLE is unknown, NOT zero. Say so; never treat it as good news.
- If nothing needs attention, say exactly that in one bullet. Do not manufacture urgency.
- No preamble, no sign-off. Bullets only, starting with "• "."""

    try:
        text = run_chat([{"role": "user", "content": prompt}])
    except AINotConfigured as exc:
        logger.info("Lab brief: no LLM (%s) — sending the template instead.", exc)
        return _fallback(facts)
    except Exception as exc:  # noqa: BLE001 — the brief must send regardless
        logger.warning("Lab brief narration failed (%s) — sending the template.", exc)
        return _fallback(facts)
    return (text or "").strip() or _fallback(facts)


def build(user, datasource_id=None) -> dict:
    facts = gather(user, datasource_id)
    return {
        "facts": facts,
        "body": narrate(facts),
        "quiet": _nothing_to_report(facts),
        "subject": _subject(facts),
    }


def _subject(facts) -> str:
    lab = (facts.get("datasource") or {}).get("name", "the lab")
    n = len(facts["breaches"]) + len(facts["alerts"])
    if not facts["configured"]:
        return f"[DIS] Lab brief — {lab} (not configured)"
    if n:
        return f"[DIS] Lab brief — {n} item(s) need attention ({lab})"
    return f"[DIS] Lab brief — all clear ({lab})"
