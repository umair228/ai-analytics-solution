"""One board as the PDF a board report emails, and its audited download (the preview)."""
from __future__ import annotations

from django.utils import timezone


def board_report_pdf(board, *, user=None, generated_by="", now=None):
    """(BoardPdf, file name) for ``board`` as ``user`` may see it.

    The figures are the board's own rows; the "Data as of" stamp is the board header's
    (the oldest of its sources); brand and footer are the deployment's.
    """
    from core.board_render import render_board_pdf
    from core.branding import footer, product_name
    from core.report_render import export_filename, format_timestamp

    from .serializers import DashboardSerializer

    now = now or timezone.now()
    out = render_board_pdf(
        board, owner=user, product=product_name(), footer_text=footer(),
        freshness=DashboardSerializer().get_freshness(board),
        generated_at=format_timestamp(now), generated_by=generated_by,
    )
    return out, export_filename(board.name, "pdf", when=now, fallback="board")


def audit_detail(out, filename) -> dict:
    return {"format": "pdf", "filename": filename, "pages": out.pages,
            "bytes": len(out.content), "data_as_of": out.data_as_of,
            "figures": out.widgets, "unavailable": out.unavailable}
