from rest_framework import serializers

from .models import Dashboard, Widget


class WidgetSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)

    class Meta:
        model = Widget
        fields = [
            "id", "dashboard", "title", "widget_type", "dataset", "dataset_name",
            "config", "x", "y", "w", "h", "order", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class DashboardSerializer(serializers.ModelSerializer):
    widgets = WidgetSerializer(many=True, read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True)
    widget_count = serializers.IntegerField(source="widgets.count", read_only=True)

    class Meta:
        model = Dashboard
        fields = [
            "id", "name", "description", "owner", "owner_username", "site",
            "visibility", "shared_with", "filters", "widgets", "widget_count",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]

    def create(self, validated_data):
        shared = validated_data.pop("shared_with", [])
        validated_data["owner"] = self.context["request"].user
        dashboard = Dashboard.objects.create(**validated_data)
        dashboard.shared_with.set(shared)
        return dashboard

    def update(self, instance, validated_data):
        shared = validated_data.pop("shared_with", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if shared is not None:
            instance.shared_with.set(shared)
        return instance
