"""Serializers for the knowledge-base staging API."""
from rest_framework import serializers

from .models import KnowledgeRecord

_FIELDS = [
    "id", "source_type", "status", "doc_id",
    "content_hash", "passage_count", "token_estimate", "validation",
    "duplicate_of", "dataset", "dataset_name", "source_file", "ingest_options",
    "created_by", "created_by_username", "reviewed_by", "reviewed_by_username",
    "reviewed_at", "reject_reason", "created_at", "updated_at",
]


class KnowledgeRecordSerializer(serializers.ModelSerializer):
    """List/summary view — everything except the (potentially large) passages.
    Records are created only through the ingest endpoints, so all fields are
    read-only here."""
    created_by_username = serializers.CharField(source="created_by.username", read_only=True)
    reviewed_by_username = serializers.CharField(source="reviewed_by.username", read_only=True)
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)

    class Meta:
        model = KnowledgeRecord
        fields = _FIELDS
        read_only_fields = _FIELDS


class KnowledgeRecordDetailSerializer(KnowledgeRecordSerializer):
    """Detail view — adds the rendered passages for review."""
    passages = serializers.JSONField(read_only=True)

    class Meta(KnowledgeRecordSerializer.Meta):
        fields = _FIELDS + ["passages"]
        read_only_fields = _FIELDS + ["passages"]
