from django.urls import path
from rest_framework_simplejwt.views import TokenBlacklistView, TokenRefreshView

from . import views_mfa
from .views import LoginView, RegisterView, me

urlpatterns = [
    path("token/", LoginView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("logout/", TokenBlacklistView.as_view(), name="token_blacklist"),
    path("register/", RegisterView.as_view(), name="register"),
    path("me/", me, name="me"),

    # Two-factor. `verify` is the second half of the login and is therefore unauthenticated
    # — it is guarded by the signed challenge, the throttle, and the account lockout.
    path("mfa/", views_mfa.status_view, name="mfa-status"),
    path("mfa/setup/", views_mfa.setup, name="mfa-setup"),
    path("mfa/confirm/", views_mfa.confirm, name="mfa-confirm"),
    path("mfa/disable/", views_mfa.disable, name="mfa-disable"),
    path("mfa/verify/", views_mfa.verify, name="mfa-verify"),
]
