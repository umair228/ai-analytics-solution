"""Symmetric encryption for data-source credentials (encrypted at rest)."""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.DSE_FERNET_KEY
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(value: str | None) -> str:
    """Encrypt a plaintext string -> token. Empty input yields empty output."""
    if not value:
        return ""
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str:
    """Decrypt a token -> plaintext. Invalid/empty input yields empty string."""
    if not token:
        return ""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
