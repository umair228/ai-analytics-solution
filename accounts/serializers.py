from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Lab, Organization, Site

User = get_user_model()


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "code", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class SiteSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = Site
        fields = [
            "id", "organization", "organization_name", "name",
            "code", "location", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class LabSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source="site.name", read_only=True)

    class Meta:
        model = Lab
        fields = ["id", "site", "site_name", "name", "code", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        style={"input_type": "password"},
    )
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    is_admin = serializers.BooleanField(read_only=True)
    can_build = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "first_name", "last_name",
            "role", "organization", "organization_name", "sites", "labs",
            "job_title", "phone", "is_active", "is_admin", "can_build",
            "date_joined", "password",
        ]
        read_only_fields = ["id", "date_joined", "is_admin", "can_build"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        sites = validated_data.pop("sites", [])
        labs = validated_data.pop("labs", [])
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        user.sites.set(sites)
        user.labs.set(labs)
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        sites = validated_data.pop("sites", None)
        labs = validated_data.pop("labs", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        if sites is not None:
            instance.sites.set(sites)
        if labs is not None:
            instance.labs.set(labs)
        return instance


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True, min_length=8, style={"input_type": "password"}
    )

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "password"]

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.role = User.Role.VIEWER  # self-registered users start as viewers
        user.save()
        return user


class DSETokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login serializer that embeds the role and returns the user profile."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["username"] = user.username
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data
