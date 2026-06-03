"""
Knowledge-base staging models for AI Document Search.

This is the *staging* layer for the document-search index. Content from two
sources — uploaded documents and dataset rows — is ingested into
``KnowledgeRecord`` rows in a ``pending`` state, deduped + validated, and only
merged into the searchable index once a reviewer ``approve``s them. This is
retrieval indexing (RAG), **not** model training — nothing here trains a model;
approved records simply become retrievable chunks (see ``docsearch/ingest.py``
and ``docsearch/index_store.py``).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class KnowledgeRecord(TimeStampedModel):
    """One logical source's worth of content staged for review.

    A record holds its pre-rendered, pre-packed ``passages`` (frozen at ingest
    time) so approval is deterministic and the index merge is a pure read — it
    never needs to re-extract a file or re-query a dataset.
    """

    class Source(models.TextChoices):
        DOCUMENT = "document", "Uploaded document"
        DATASET = "dataset", "Dataset rows"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        DUPLICATE = "duplicate", "Duplicate (auto-flagged)"

    source_type = models.CharField(max_length=16, choices=Source.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )

    # Virtual filename used as the index ``doc_id`` / ``meta["source"]``
    # (e.g. "Inventory SOP.docx" or "dataset:Veterinary Samples#42").
    doc_id = models.CharField(max_length=512)

    # Pre-rendered, pre-packed passages: [{"page": int|str, "text": str}, ...].
    passages = models.JSONField(default=list, blank=True)

    # sha256 of the normalised passage text — the exact-dedupe key.
    content_hash = models.CharField(max_length=64, db_index=True)

    # Provenance
    dataset = models.ForeignKey(
        "datasets.Dataset", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="knowledge_records",
    )
    source_file = models.CharField(max_length=512, blank=True)
    ingest_options = models.JSONField(default=dict, blank=True)

    # Staging / validation
    passage_count = models.IntegerField(default=0)
    token_estimate = models.IntegerField(default=0)
    validation = models.JSONField(default=dict, blank=True)  # {"warnings": [...], "errors": [...]}
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="duplicates",
    )

    # Review audit
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="kb_records_created",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="kb_records_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "source_type"]),
            models.Index(fields=["content_hash"]),
        ]
        constraints = [
            # Hard dedupe backstop: a given normalised content can be APPROVED
            # only once, so two identical copies can never both be indexed —
            # even if they came from different sources. (Partial unique index;
            # supported on the metadata DB, SQLite/PostgreSQL.)
            models.UniqueConstraint(
                fields=["content_hash"],
                condition=models.Q(status="approved"),
                name="uniq_approved_content_hash",
            ),
        ]

    def __str__(self):
        return f"{self.doc_id} [{self.status}]"

    @property
    def is_duplicate(self) -> bool:
        return self.status == self.Status.DUPLICATE or self.duplicate_of_id is not None
