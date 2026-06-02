"""
    GET /api/doc-search/status/  -> feature configuration + index summary.
"""
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import index_store


class DocSearchStatusView(APIView):
    def get(self, request):
        try:
            from ai.client import is_configured
            ai_ok = is_configured()
        except Exception:
            ai_ok = False
        return Response({
            "ai_configured": ai_ok,
            "model": settings.CLAUDE_MODEL if ai_ok else None,
            "answer_engine": "claude" if ai_ok else "extractive",
            "semantic_enabled": getattr(settings, "DOCSEARCH_ENABLE_SEMANTIC", True),
            "ocr_enabled": getattr(settings, "DOCSEARCH_ENABLE_OCR", False),
            "sql_enabled": getattr(settings, "DOCSEARCH_ENABLE_SQL", True),
            **index_store.status(),
        }, status=status.HTTP_200_OK)
