"""TOTP — RFC 6238 — implemented against the standard's own published test vectors.

Why this is here rather than `pip install pyotp`:

TOTP is not a cryptographic *primitive*; it is a short, exactly-specified protocol on top of
HMAC, and the HMAC comes from ``hashlib``. "Don't roll your own crypto" is a rule about
inventing primitives, and nothing here is invented. What matters is whether the
implementation is *correct*, and RFC 6238 Appendix B publishes **official test vectors** — so
correctness is something this module can be shown to have, rather than something we assert by
naming a package. ``tests_mfa.py`` runs all of them.

The upside is real for an on-premise, regulated product: one less package in the
supply chain of a system that a customer's security team will audit, and an implementation
whose entire behaviour fits on one screen.

Two details that are easy to get wrong and are the difference between a control and a
decoration:

* **The window.** RFC 6238 §5.2 says at most ONE step of tolerance either side. Every extra
  step you allow is another 30 seconds during which an intercepted code still works.
* **Replay.** A code is valid for its whole 30-second step. Without recording the last step a
  user consumed, an attacker who observes a code — over the shoulder, in a screenshot, in a
  proxy log — can replay it inside that window. So :func:`verify` *returns the step it
  matched*, and the caller must refuse to reuse a step it has already seen. That is enforced
  in ``accounts/models.py::MfaDevice.verify``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
STEP_SECONDS = 30
# One step either side. RFC 6238 §5.2: "We RECOMMEND that at most one time step is allowed
# as the network delay." Widening this is not a kindness to users; it is a gift to whoever
# is watching them type.
WINDOW = 1


def new_secret() -> str:
    """A fresh base32 secret. 20 bytes = 160 bits, the size RFC 4226 §4 requires as a
    minimum for HMAC-SHA1."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 §5.3 — the HOTP that TOTP is built from."""
    # b32decode is strict about padding; a secret we generated has had it stripped.
    padding = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + padding, casefold=True)

    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F                       # dynamic truncation
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFF_FFFF
    return str(code % (10 ** digits)).zfill(digits)


def step_at(when: float | None = None) -> int:
    return int((when if when is not None else time.time()) // STEP_SECONDS)


def generate(secret_b32: str, when: float | None = None, digits: int = DIGITS) -> str:
    return _hotp(secret_b32, step_at(when), digits)


def verify(secret_b32: str, code: str, *, when: float | None = None,
           window: int = WINDOW, digits: int = DIGITS) -> int | None:
    """Return the time-step the code matched, or None.

    It returns the *step* rather than a bool on purpose: the caller has to record it, or a
    code stays replayable for the rest of its 30-second life. A ``verify()`` that answered
    only yes/no would make replay protection impossible to implement correctly, and would
    therefore make it something everybody skips.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != digits:
        return None

    now = step_at(when)
    for offset in range(-window, window + 1):
        step = now + offset
        if step < 0:
            continue
        # Constant time: a timing side-channel on a 6-digit code is not theoretical.
        if hmac.compare_digest(_hotp(secret_b32, step, digits), code):
            return step
    return None


def provisioning_uri(secret_b32: str, *, account: str, issuer: str = "DIS") -> str:
    """The ``otpauth://`` URI every authenticator app understands (Key Uri Format)."""
    label = quote(f"{issuer}:{account}", safe="")
    return (f"otpauth://totp/{label}"
            f"?secret={secret_b32}"
            f"&issuer={quote(issuer, safe='')}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}")


def qr_svg(uri: str) -> str:
    """The provisioning URI as an inline SVG.

    SVG rather than PNG because the SVG factory is pure Python — no Pillow, no native
    image codecs in the container of a system that a security team will inventory.
    """
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")
