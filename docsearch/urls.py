"""URL routes for the docsearch app (mounted under /api/)."""
from django.urls import path

from .views import (
    DocSearchStatusView,
    DocSearchView,
    DocumentDeleteView,
    DocumentsView,
)

urlpatterns = [
    path("doc-search/", DocSearchView.as_view(), name="doc-search"),
    path("doc-search/status/", DocSearchStatusView.as_view(), name="doc-search-status"),
    path("doc-search/documents/", DocumentsView.as_view(), name="doc-search-documents"),
    path("doc-search/documents/<path:name>/", DocumentDeleteView.as_view(), name="doc-search-document-delete"),
]
