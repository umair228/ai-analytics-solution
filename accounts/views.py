from django.contrib.auth import get_user_model
from rest_framework import generics, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView

from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAdmin

from .models import APIToken, Lab, Organization, Site
from .serializers import (
    APITokenSerializer,
    DSETokenObtainPairSerializer,
    LabSerializer,
    OrganizationSerializer,
    RegisterSerializer,
    SiteSerializer,
    UserSerializer,
)

User = get_user_model()


class LoginView(TokenObtainPairView):
    """JWT login — returns access + refresh tokens and the user profile."""

    serializer_class = DSETokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            record_audit(
                request,
                AuditLog.Action.LOGIN,
                target_type="User",
                summary=f"Login: {request.data.get('username', '')}",
            )
        return response


class RegisterView(generics.CreateAPIView):
    """Self-service registration — new users start with the viewer role."""

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    """Return or update the authenticated user's own profile."""
    if request.method == "PATCH":
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        # users cannot escalate their own role/permissions
        serializer.validated_data.pop("role", None)
        serializer.validated_data.pop("is_active", None)
        serializer.save()
        return Response(serializer.data)
    return Response(UserSerializer(request.user).data)


class UserViewSet(viewsets.ModelViewSet):
    """Admin-only user management."""

    queryset = User.objects.all().order_by("username")
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]


class OrganizationViewSet(viewsets.ModelViewSet):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdmin()]


class SiteViewSet(viewsets.ModelViewSet):
    queryset = Site.objects.select_related("organization").all()
    serializer_class = SiteSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdmin()]


class LabViewSet(viewsets.ModelViewSet):
    queryset = Lab.objects.select_related("site").all()
    serializer_class = LabSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdmin()]


class APITokenViewSet(viewsets.ModelViewSet):
    """Personal API tokens — users manage only their own tokens."""

    serializer_class = APITokenSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return APIToken.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
