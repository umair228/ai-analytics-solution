"""Certificate of Analysis API — issue, read, sign, render.

There is no update endpoint, and that is not an omission. A COA certifies the data as
it was when issued; correcting it means issuing a NEW certificate that supersedes the
old one. A document a customer already holds does not get edited underneath them.
"""
from __future__ import annotations

from django.db.models import Q
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.exporters import ExportUnavailable
from connections.constants import SourceType
from connections.access import visible_datasources
from connections.models import DataSource
from core.audit import client_ip, record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove

from . import renderer, services
from .builder import CoaError
from .models import Certificate, Signature
from .signing import SigningError, manifestation, sign


# One definition, in connections/access.py. This was copy-pasted into four apps.
_visible_datasources = visible_datasources


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAnalystOrAbove])
def certificates(request):
    if request.method == "GET":
        qs = Certificate.objects.select_related("issued_by", "superseded_by") \
                                .prefetch_related("signatures")
        if sample := request.query_params.get("sample"):
            qs = qs.filter(sample_number=str(sample))
        if status_ := request.query_params.get("status"):
            qs = qs.filter(status=status_)
        return Response({"certificates": [services.summary(c) for c in qs[:200]]})

    ds_id = request.data.get("datasource")
    sample = request.data.get("sample_number")
    if not sample:
        return Response({"error": True, "detail": "sample_number is required."}, status=400)

    ds = _visible_datasources(request.user).filter(pk=ds_id).first()
    if ds is None:
        return Response({"error": True,
                         "detail": "Unknown or inaccessible data source."}, status=404)
    try:
        cert = services.issue(ds, sample, user=request.user)
    except CoaError as exc:
        return Response({"error": True, "detail": str(exc)}, status=400)

    return Response(services.summary(cert), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def certificate_detail(request, pk):
    cert = Certificate.objects.filter(pk=pk).prefetch_related("signatures").first()
    if cert is None:
        return Response({"error": True, "detail": "Certificate not found."}, status=404)
    return Response({
        **services.summary(cert),
        "content": cert.content,
        "manifestation": manifestation(cert),
        "is_approved": cert.is_approved,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def certificate_render(request, pk):
    cert = Certificate.objects.filter(pk=pk).prefetch_related("signatures").first()
    if cert is None:
        return Response({"error": True, "detail": "Certificate not found."}, status=404)

    # NB `fmt`, not `format`. DRF reserves `?format=` for content negotiation, and with
    # only JSONRenderer registered it answers `?format=pdf` with a 404 — so the PDF
    # download would have silently become "Not found" in production.
    fmt = (request.query_params.get("fmt") or "html").lower()
    # A certificate leaving the building is a record leaving the building.
    record_audit(request, AuditLog.Action.QUERY, target_type="coa.Certificate",
                 target_id=cert.id,
                 summary=f"Rendered {cert.number} as {fmt}",
                 detail={"number": cert.number, "format": fmt,
                         "status": cert.status, "verdict": cert.verdict,
                         "content_hash": cert.content_hash})

    if fmt == "pdf":
        try:
            pdf = renderer.to_pdf(cert)
        except ExportUnavailable as exc:
            return Response({"error": True, "detail": str(exc)}, status=501)
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="{cert.number}.pdf"'
        return resp

    return HttpResponse(renderer.to_html(cert), content_type="text/html")


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAnalystOrAbove])
def certificate_sign(request, pk):
    """Apply an electronic signature (21 CFR Part 11 subpart C).

    Requires the signer's password: being logged in is not signing, and an unattended
    terminal must not be able to release a batch.
    """
    cert = Certificate.objects.filter(pk=pk).prefetch_related("signatures").first()
    if cert is None:
        return Response({"error": True, "detail": "Certificate not found."}, status=404)

    try:
        signature = sign(
            cert,
            user=request.user,
            password=request.data.get("password") or "",
            meaning=request.data.get("meaning") or Signature.Meaning.APPROVED,
            reason=request.data.get("reason") or "",
            ip_address=client_ip(request),
        )
    except SigningError as exc:
        # A refused signature is itself an event worth recording — repeated failures on
        # a release-critical document are exactly what §11.300(d) asks you to detect.
        record_audit(request, AuditLog.Action.UPDATE, target_type="coa.Certificate",
                     target_id=cert.id,
                     summary=f"Signature REFUSED on {cert.number}",
                     detail={"number": cert.number, "reason": str(exc),
                             "meaning": request.data.get("meaning")})
        return Response({"error": True, "detail": str(exc)}, status=400)

    record_audit(request, AuditLog.Action.UPDATE, target_type="coa.Certificate",
                 target_id=cert.id,
                 summary=f"{signature.get_meaning_display()} {cert.number} "
                         f"by {signature.printed_name}",
                 detail={"number": cert.number, "meaning": signature.meaning,
                         "printed_name": signature.printed_name,
                         "signed_at": signature.signed_at.isoformat(),
                         "reason": signature.reason,
                         "content_hash": signature.content_hash,
                         "verdict": cert.verdict})

    cert.refresh_from_db()
    return Response({**services.summary(cert), "manifestation": manifestation(cert)})
