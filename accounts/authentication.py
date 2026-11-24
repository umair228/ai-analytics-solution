"""Custom DRF authentication backend for personal API tokens.

The token presented on the wire is hashed and looked up by hash. The database never
holds a usable credential, so a stolen dump — or the nightly backup — yields nothing an
attacker can present.
"""
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

        from .models import APIToken

        try:
            token = APIToken.objects.select_related("user").get(
                key_hash=APIToken.hash_key(auth[1]))
        except APIToken.DoesNotExist:
            raise AuthenticationFailed("Invalid API token.")

        # An expired token is not a valid token. Saying so plainly is kinder than a
        # generic failure that sends someone hunting for a typo they did not make.
        if token.expired:
            raise AuthenticationFailed(
                f"This API token expired on {token.expires_at:%Y-%m-%d}. Create a new one.")
        # An offboarded user's token must stop working the moment the account is
        # disabled — otherwise deactivating a leaver does nothing to their scripts.
        if not token.user.is_active:
            raise AuthenticationFailed("The owner of this API token is no longer active.")

        now = timezone.now()
        if not token.last_used_at or (now - token.last_used_at).total_seconds() > 3600:
            APIToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return (token.user, token)

    def authenticate_header(self, request):
        return self.keyword
