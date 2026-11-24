"""The Operational Qualification as a durable artefact of a **specific build**.

``manage.py qualify`` executes the real test suite. That takes the better part of a minute
and needs a test database, so it can never run inside an HTTP request in production — and
it should not want to. In a validated system the OQ is *evidence produced when a build was
qualified*, not a thing you re-run live against production data. Re-running it on the
deployed box would prove something about the box, not about the software.

So: CI runs it, this module writes down what happened, the image carries the file, and the
API serves it. If the file is absent the API says exactly that — **"this build carries no
OQ evidence"** — because an empty validation page and a passing one must never look alike.

The report records *which build it is about*. A validation summary that does not name the
thing it validated is a piece of paper.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings


def report_path() -> Path:
    return Path(getattr(settings, "DSE_OQ_REPORT_PATH",
                        Path(settings.BASE_DIR) / "validation" / "oq_report.json"))


def _build_id() -> dict:
    """Name the build this evidence is about.

    ``git rev-parse`` is not available inside the deployed image, so the build stamps
    ``DIS_GIT_SHA``/``DIS_VERSION`` at image-build time. Falling back to git covers the
    developer running it locally, and "unknown" is reported as "unknown" — a validation
    artefact that quietly attributes itself to the wrong build is worse than one that
    admits it does not know.
    """
    sha = os.environ.get("DIS_GIT_SHA") or ""
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                 text=True, timeout=5, cwd=settings.BASE_DIR).stdout.strip()
        except Exception:  # noqa: BLE001
            sha = ""
    return {
        "git_sha": sha or "unknown",
        "version": os.environ.get("DIS_VERSION", "") or "unknown",
        "image_tag": os.environ.get("TAG", "") or "unknown",
    }


def to_dict(evidence: dict) -> dict:
    """Turn ``validation.runner.run()`` evidence into something JSON can hold."""
    outcomes = []
    for o in evidence["outcomes"]:
        r = o.requirement
        outcomes.append({
            "id": r.id,
            "category": r.category,
            "statement": r.statement,
            "rationale": r.rationale,
            "risk": r.risk,
            "status": o.status,                       # passed | failed | missing | manual
            "tests_run": o.tests_run,
            "verified_by": list(r.verified_by),
            "failures": list(o.failures),
            "missing": list(o.missing),
            "manual": r.manual or "",
        })

    n_manual = sum(1 for o in outcomes if o["status"] == "manual")
    n_failed = sum(1 for o in outcomes if o["status"] in ("failed", "missing"))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build": _build_id(),
        "tests_executed": evidence["tests_executed"],
        "requirements_total": len(outcomes),
        "requirements_automated": len(outcomes) - n_manual,
        "requirements_manual": n_manual,
        "requirements_failed": n_failed,
        # The OQ passes only over its AUTOMATED scope. Saying "OQ PASSED" while six
        # requirements sit unverified would be the exact lie this whole effort exists to
        # avoid, so the scope is part of the verdict rather than a footnote to it.
        "oq_passed": n_failed == 0,
        "scope_note": (
            f"{n_manual} requirement(s) cannot be qualified by automated test and are "
            f"OPERATOR TO VERIFY. They are not a pass. Run `manage.py preflight` on the "
            f"deployed system for those."),
        "outcomes": outcomes,
    }


def write(evidence: dict, path: Path | None = None) -> Path:
    path = Path(path or report_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(evidence), indent=2), encoding="utf-8")
    return path


def load() -> dict | None:
    """The stored report, or None if this build carries no OQ evidence."""
    path = report_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
