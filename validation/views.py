"""The validation evidence, over HTTP — so "Compliance by Construction" is a thing a
customer can *look at* rather than a claim on a slide.

Two endpoints, and they are deliberately not equally visible.

``GET /api/validation/oq/``  — the Operational Qualification for **this build**.
    Requirements, the tests that verify each one, what passed, and the six that no
    automated test can settle. Any authenticated user may read it. It is the argument for
    trusting the system, and it should be easy to make.

``GET /api/validation/posture/`` — the Installation Qualification: what is true of **this
    running deployment** right now. **ADMINISTRATORS ONLY**, and that restriction is the
    most important line in this file.

    Preflight is an inventory of the system's weaknesses, written in plain language, with
    remediation attached: *"DIS connects to this LIMS as 'sa' — the database SUPERUSER"*;
    *"the application is serving over plaintext HTTP"*. That is a roadmap. Publishing it to
    every authenticated user — on a system that, until a few days ago, would hand an
    account to anyone who asked — would turn an honesty feature into a reconnaissance
    endpoint. Honest about our flaws to the operator; not a briefing for whoever is
    guessing passwords.

The OQ is *not* re-run here. It executes the real test suite against a test database and
takes the better part of a minute; more to the point, in a validated system the OQ is
evidence about the build that was qualified, not something production re-runs against live
data. CI produces it, the image carries it, this serves it. If the build carries no
evidence, the endpoint says so — an unvalidated build and a validated one must never look
the same.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.permissions import IsAdmin

from . import artifact


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oq_report(request):
    """The Operational Qualification for the build that is running."""
    report = artifact.load()
    if report is None:
        # Not an error, and emphatically not an empty pass. A build with no OQ evidence is
        # a build nobody qualified, and the page must say that in those words.
        return Response({
            "available": False,
            "detail": ("This build carries no Operational Qualification evidence. It was "
                       "not produced by a pipeline that ran `manage.py qualify --json`, "
                       "so nothing here has been verified for THIS build."),
            "fix": "make ci   (or: python manage.py qualify --json)",
        })
    return Response({"available": True, **report})


@api_view(["GET"])
@permission_classes([IsAdmin])
def posture(request):
    """The Installation Qualification: the live security posture of this deployment.

    Administrators only — see the module docstring. This is a list of the ways in which
    this particular installation is currently weak.
    """
    from .preflight import run

    checks = run()
    return Response({
        "checks": [
            {"id": c.id, "name": c.name, "status": c.status, "detail": c.detail,
             "fix": c.fix, "evidence": c.evidence}
            for c in checks
        ],
        "summary": {
            "passed": sum(1 for c in checks if c.status == "PASS"),
            "failed": sum(1 for c in checks if c.status == "FAIL"),
            "warnings": sum(1 for c in checks if c.status == "WARN"),
            "unknown": sum(1 for c in checks if c.status == "UNKNOWN"),
        },
        # Said here as well as in the CLI, because this is the number people will read off
        # a screen and repeat in a meeting.
        "note": ("UNKNOWN is not a pass. A check that could not run has proven nothing."),
    })
