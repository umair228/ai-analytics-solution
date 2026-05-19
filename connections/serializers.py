from rest_framework import serializers

from .constants import SourceType
from .models import DataSource


class DataSourceSerializer(serializers.ModelSerializer):
    """Serializer for data-source connections. The password is write-only and
    never returned; ``has_password`` signals whether one is stored."""

    password = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        style={"input_type": "password"},
    )
    has_password = serializers.SerializerMethodField()
    owner_username = serializers.CharField(source="owner.username", read_only=True)
    source_type_label = serializers.CharField(
        source="get_source_type_display", read_only=True
    )
    is_file_source = serializers.BooleanField(read_only=True)

    class Meta:
        model = DataSource
        fields = [
            "id", "name", "description", "source_type", "source_type_label",
            "host", "port", "database_name", "schema_name", "username",
            "password", "has_password", "options",
            "owner", "owner_username", "site", "visibility", "shared_with",
            "test_status", "last_tested_at", "last_test_error",
            "is_file_source", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "owner", "test_status", "last_tested_at",
            "last_test_error", "created_at", "updated_at",
        ]

    def get_has_password(self, obj) -> bool:
        return bool(obj.password_encrypted)

    def validate_source_type(self, value):
        if value not in {choice[0] for choice in SourceType.CHOICES}:
            raise serializers.ValidationError(f"Unsupported source type '{value}'.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", "")
        shared = validated_data.pop("shared_with", [])
        datasource = DataSource(**validated_data)
        datasource.owner = self.context["request"].user
        if password:
            datasource.password = password
        datasource.save()
        datasource.shared_with.set(shared)
        return datasource

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        shared = validated_data.pop("shared_with", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:  # only replace when a non-empty password is supplied
            instance.password = password
        instance.save()
        if shared is not None:
            instance.shared_with.set(shared)
        return instance
