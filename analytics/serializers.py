"""Serializers for saved analytics runs.

A light list serializer keeps history listings cheap (no ``result``/``config``
blobs); the detail serializer ships the full payload for the run-detail view.
"""
from rest_framework import serializers

from .models import AnalysisRun


class AnalysisRunListSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True)

    class Meta:
        model = AnalysisRun
        fields = [
            "id", "name", "workflow", "method",
            "dataset", "dataset_name", "owner", "owner_username",
            "status", "metrics", "duration_ms",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class AnalysisRunDetailSerializer(AnalysisRunListSerializer):
    class Meta(AnalysisRunListSerializer.Meta):
        fields = AnalysisRunListSerializer.Meta.fields + [
            "config", "result", "error", "shared_with",
        ]
        read_only_fields = fields
