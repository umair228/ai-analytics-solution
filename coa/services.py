"""Issuing certificates.

A COA certifies the data *as it was* when issued. If the LIMS is later corrected, that
is a NEW certificate superseding the old one — never a silent edit to a document a
customer already holds. That is the difference between a record and a web page.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from core.audit import log_audit
from core.models import AuditLog

from . import builder
from .models import Certificate


def next_number(sample_number) -> str:
    """COA-<sample>-<nn> — the revision is visible in the number itself, because a
    customer holding revision 1 must be able to tell that revision 2 exists."""
    n = Certificate.objects.filter(sample_number=str(sample_number)).count() + 1
    return f"COA-{sample_number}-{n:02d}"


@transaction.atomic
def issue(datasource, sample_number, *, user, supersede=True) -> Certificate:
    """Build and issue a certificate for one sample."""
    content = builder.build(datasource, sample_number)

    prior = None
    if supersede:
        prior = (Certificate.objects
                 .select_for_update()
                 .filter(sample_number=str(sample_number))
                 .exclude(status=Certificate.Status.SUPERSEDED)
                 .order_by("-created_at").first())

    cert = Certificate.objects.create(
        number=next_number(sample_number),
        datasource=datasource,
        sample_number=str(sample_number),
        sample_text_id=content["sample"]["text_id"],
        product=content["sample"]["product"],
        content=content,
        verdict=content["verdict"],
        status=Certificate.Status.DRAFT,
        issued_by=user if getattr(user, "is_authenticated", False) else None,
    )

    if prior:
        prior.status = Certificate.Status.SUPERSEDED
        prior.superseded_by = cert
        prior.save(update_fields=["status", "superseded_by", "updated_at"])

    log_audit(
        user=user,
        action=AuditLog.Action.CREATE,
        target_type="coa.Certificate",
        target_id=cert.id,
        summary=f"Issued {cert.number} for sample {sample_number}: {cert.verdict}",
        detail={
            "number": cert.number,
            "sample_number": str(sample_number),
            "verdict": cert.verdict,
            "verdict_reason": content["verdict_reason"],
            "content_hash": cert.content_hash,
            "tests_ordered": content["tests_ordered"],
            "tests_reported": content["tests_reported"],
            "missing_tests": [m["analysis"] for m in content["missing_tests"]],
            "supersedes": prior.number if prior else None,
            "datasource": content["provenance"]["datasource"],
            "freshness": content["provenance"].get("freshness"),
        },
    )
    return cert


def summary(cert) -> dict:
    """The face of the certificate, for a list view."""
    c = cert.content or {}
    return {
        "id": cert.id,
        "number": cert.number,
        "sample_number": cert.sample_number,
        "sample_text_id": cert.sample_text_id,
        "product": cert.product,
        "verdict": cert.verdict,
        "verdict_reason": c.get("verdict_reason", ""),
        "status": cert.status,
        "certifiable": cert.certifiable,
        "tests_ordered": c.get("tests_ordered"),
        "tests_reported": c.get("tests_reported"),
        "missing_tests": [m["analysis"] for m in c.get("missing_tests", [])],
        "issued_by": getattr(cert.issued_by, "username", None),
        "issued_at": cert.created_at.isoformat(),
        "content_hash": cert.content_hash,
        "signatures": len(cert.valid_signatures),
        "tampered": bool(cert.broken_signatures),
        "superseded_by": cert.superseded_by.number if cert.superseded_by else None,
    }
