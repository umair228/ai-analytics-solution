"""URL routes for the docsearch app (mounted under /api/)."""
from django.urls import path

from .views import (
    DocSearchStatusView,
    DocSearchView,
    DocumentDeleteView,
    DocumentsView,
    KnowledgeApproveView,
    KnowledgeDetailView,
    KnowledgeIngestDatasetView,
    KnowledgeIngestDocumentsView,
    KnowledgeListView,
    KnowledgeRejectView,
    KnowledgeSourcesView,
)

urlpatterns = [
    path("doc-search/", DocSearchView.as_view(), name="doc-search"),
    path("doc-search/status/", DocSearchStatusView.as_view(), name="doc-search-status"),
    path("doc-search/documents/", DocumentsView.as_view(), name="doc-search-documents"),
    path("doc-search/documents/<path:name>/", DocumentDeleteView.as_view(), name="doc-search-document-delete"),

    # Knowledge-base staging pipeline
    path("doc-search/knowledge/", KnowledgeListView.as_view(), name="kb-list"),
    path("doc-search/knowledge/documents/", KnowledgeIngestDocumentsView.as_view(), name="kb-ingest-documents"),
    path("doc-search/knowledge/datasets/", KnowledgeIngestDatasetView.as_view(), name="kb-ingest-datasets"),
    path("doc-search/knowledge/sources/", KnowledgeSourcesView.as_view(), name="kb-sources"),
    path("doc-search/knowledge/<int:pk>/", KnowledgeDetailView.as_view(), name="kb-detail"),
    path("doc-search/knowledge/<int:pk>/approve/", KnowledgeApproveView.as_view(), name="kb-approve"),
    path("doc-search/knowledge/<int:pk>/reject/", KnowledgeRejectView.as_view(), name="kb-reject"),
]
