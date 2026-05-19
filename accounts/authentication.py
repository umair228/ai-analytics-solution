"""Custom DRF authentication backend for personal API tokens."""
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class APITokenAuthentication(BaseAuthentication):
    """Authenticate via 'Authorization: ApiKey <token>' header."""

    keyword = "ApiKey"

    def authenticate(self, request):
        auth = request.META.get("HTTP_AUTHORIZATION", "").split()
        if not auth or auth[0].lower() != self.keyword.lower():
            return None
        if len(auth) != 2:
            raise AuthenticationFailed("Invalid API token header format.")
        key = auth[1]
        from .models import APIToken
        try:
            token = APIToken.objects.select_related("user").get(key=key)
        except APIToken.DoesNotExist:
            raise AuthenticationFailed("Invalid API token.")
        now = timezone.now()
        if not token.last_used_at or (now - token.last_used_at).total_seconds() > 3600:
            APIToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return (token.user, token)

    def authenticate_header(self, request):
        return self.keyword
