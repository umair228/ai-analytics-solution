from rest_framework import serializers

from .models import QueryDefinition


class QueryDefinitionSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(source="owner.username", read_only=True)
    datasource_name = serializers.CharField(source="datasource.name", read_only=True)

    class Meta:
        model = QueryDefinition
        fields = [
            "id", "name", "description", "datasource", "datasource_name",
            "database", "mode", "spec", "raw_sql", "generated_sql",
            "owner", "owner_username", "site", "visibility", "shared_with",
            "parameters", "last_run_at", "last_row_count", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "owner", "generated_sql", "last_run_at",
            "last_row_count", "created_at", "updated_at",
        ]

    def create(self, validated_data):
        shared = validated_data.pop("shared_with", [])
        validated_data["owner"] = self.context["request"].user
        query = QueryDefinition.objects.create(**validated_data)
        query.shared_with.set(shared)
        return query

    def update(self, instance, validated_data):
        shared = validated_data.pop("shared_with", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if shared is not None:
            instance.shared_with.set(shared)
        return instance
