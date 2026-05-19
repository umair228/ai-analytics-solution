from rest_framework import serializers

from .models import Dataset


class DatasetSerializer(serializers.ModelSerializer):
    query_name = serializers.CharField(source="query.name", read_only=True)
    datasource_name = serializers.CharField(
        source="query.datasource.name", read_only=True
    )
    owner_username = serializers.CharField(source="owner.username", read_only=True)

    class Meta:
        model = Dataset
        fields = [
            "id", "name", "description", "query", "query_name", "datasource_name",
            "owner", "owner_username", "site", "visibility", "shared_with",
            "cached_columns", "calculated_fields", "row_count",
            "last_refreshed_at", "last_error", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "owner", "cached_columns", "row_count",
            "last_refreshed_at", "last_error", "created_at", "updated_at",
        ]

    def create(self, validated_data):
        shared = validated_data.pop("shared_with", [])
        validated_data["owner"] = self.context["request"].user
        dataset = Dataset.objects.create(**validated_data)
        dataset.shared_with.set(shared)
        return dataset

    def update(self, instance, validated_data):
        shared = validated_data.pop("shared_with", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if shared is not None:
            instance.shared_with.set(shared)
        return instance
