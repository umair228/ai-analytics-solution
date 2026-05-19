from rest_framework import serializers

from .models import AlertEvent, Dataset, DatasetAlert


class DatasetSerializer(serializers.ModelSerializer):
    query_name      = serializers.CharField(source="query.name",            read_only=True)
    datasource_name = serializers.CharField(source="query.datasource.name", read_only=True)
    owner_username  = serializers.CharField(source="owner.username",        read_only=True)
    # Expose which query parameters are defined on the backing query
    query_parameters = serializers.JSONField(source="query.parameters",     read_only=True)

    class Meta:
        model = Dataset
        fields = [
            "id", "name", "description", "query", "query_name", "datasource_name",
            "owner", "owner_username", "site", "visibility", "shared_with",
            "cached_columns", "calculated_fields", "row_count",
            "last_refreshed_at", "last_error", "refresh_interval",
            "next_refresh_at", "param_defaults", "query_parameters",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "owner", "cached_columns", "row_count",
            "last_refreshed_at", "last_error", "next_refresh_at",
            "created_at", "updated_at",
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


class DatasetAlertSerializer(serializers.ModelSerializer):
    dataset_name   = serializers.CharField(source="dataset.name",   read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True)
    unread_count   = serializers.SerializerMethodField()

    class Meta:
        model = DatasetAlert
        fields = [
            "id", "name", "dataset", "dataset_name", "column",
            "aggregation", "condition", "threshold", "is_active",
            "owner", "owner_username", "unread_count",
            "last_checked_at", "last_triggered_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "owner", "last_checked_at", "last_triggered_at",
            "created_at", "updated_at",
        ]

    def get_unread_count(self, obj):
        return obj.events.filter(acknowledged=False).count()

    def create(self, validated_data):
        validated_data["owner"] = self.context["request"].user
        return super().create(validated_data)


class AlertEventSerializer(serializers.ModelSerializer):
    alert_name     = serializers.CharField(source="alert.name",          read_only=True)
    dataset_name   = serializers.CharField(source="alert.dataset.name",  read_only=True)
    dataset_id     = serializers.IntegerField(source="alert.dataset.id", read_only=True)

    class Meta:
        model = AlertEvent
        fields = [
            "id", "alert", "alert_name", "dataset_id", "dataset_name",
            "triggered_value", "message",
            "acknowledged", "acknowledged_at", "acknowledged_by",
            "created_at",
        ]
        read_only_fields = [
            "id", "alert", "triggered_value", "message",
            "acknowledged_at", "acknowledged_by", "created_at",
        ]
