from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import generics, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView

from core.audit import client_ip, log_audit, record_audit
from core.models import AuditLog
from core.permissions import IsAdmin

from . import lockout, mfa
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
    """JWT login — returns access + refresh tokens and the user profile.

    It used to audit a login only ``if response.status_code == 200``, so a **failed**
    login produced no record at all: the trail could not distinguish a quiet night from a
    night spent guessing passwords. Every attempt is now recorded, the account locks after
    repeated failures, and the lockout is reported — see :mod:`accounts.lockout` and Part
    11 §11.300(d).
    """

    serializer_class = DSETokenObtainPairSerializer
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        username = str(request.data.get("username") or "")[:150]
        ip = client_ip(request)

        if lockout.check_locked(username, ip):
            # Same response as a wrong password. Saying "that account is locked" confirms
            # the username exists AND hands over a denial-of-service in one sentence.
            return Response({"detail": "No active account found with the given credentials."},
                            status=status.HTTP_401_UNAUTHORIZED)

        try:
            response = super().post(request, *args, **kwargs)
        except AuthenticationFailed:
            lockout.record_failure(username, ip)
            raise

        if response.status_code == 200:
            user = User.objects.filter(username=username).first()

            # The password was right. If this account carries a second factor, that is NOT
            # a successful login — it is half of one, and it must not hand back tokens.
            #
            # The challenge is a short-lived signed blob (accounts/mfa.py), not a JWT.
            # Returning anything usable here would make the second factor optional, which
            # is the single way this feature gets built wrong.
            if user is not None and mfa.is_active(user):
                return Response({
                    "mfa_required": True,
                    "mfa_token": mfa.issue_challenge(user),
                    "detail": "Enter the code from your authenticator app.",
                })

            if user is not None:
                lockout.record_success(user, ip)
                # An account whose role obliges it to have a second factor, and which does
                # not, is let in — and then stopped at every endpoint except the ones that
                # let it enrol (accounts.middleware). Refusing the login outright
                # would lock people out of the system on the day the policy is switched on,
                # with no way to comply.
                if mfa.required_for(user):
                    response.data["mfa_enrolment_required"] = True
        else:
            lockout.record_failure(username, ip)
        return response


class RegisterView(generics.CreateAPIView):
    """Self-service registration — **off unless DSE_ALLOW_SELF_REGISTRATION is set.**

    This endpoint was open, and it was survivable only by accident: a brand-new account
    could see nothing, because visibility was decided entirely by owner/shared_with and
    both are empty for a stranger. The moment ``Visibility.ORG`` began to mean something
    (``core.access``), an open registration endpoint became "read every organisation-wide
    dataset in the laboratory" — for anyone who can reach the box, over the plaintext HTTP
    the pilot still serves.

    Part 11 §11.10(d) requires *limiting system access to authorized individuals*. An
    endpoint that mints an account for whoever asks is the plain opposite of that, so an
    administrator creates users, and an attempt to self-register is itself recorded: on a
    closed system, someone trying to make themselves an account is a security event.
    """

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    throttle_scope = "register"

    def create(self, request, *args, **kwargs):
        if not getattr(settings, "DSE_ALLOW_SELF_REGISTRATION", False):
            log_audit(
                action=AuditLog.Action.DENIED, target_type="User",
                summary="Self-registration attempt refused (registration is closed)",
                detail={"username": str(request.data.get("username") or "")[:150]},
                ip_address=client_ip(request),
            )
            return Response(
                {"detail": "Registration is closed. Ask an administrator for an account."},
                status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)


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
