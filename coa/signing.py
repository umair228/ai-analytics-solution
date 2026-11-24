"""Electronic signatures — 21 CFR Part 11 subpart C.

Three requirements, three mechanisms, and none of them is decorative:

* **§11.200 — signature components.** A signature requires re-authentication at the
  point of signing. Being logged in is not signing; a signature is a deliberate act,
  and an unattended terminal must not be able to release a batch.
* **§11.50 — manifestation.** The printed name, the date/time, and the *meaning* of the
  signature ("approved", "reviewed") are captured and printed on the certificate. A
  signature with no meaning tells you somebody clicked something.
* **§11.70 — record linking.** The signature stores the SHA-256 of the content it
  signed. Change the certificate and the hashes stop matching, so the signature is
  visibly invalid — it cannot be excised, copied, or transferred to another record.

And one rule that is ours rather than the FDA's, but matters just as much:

* **An INCOMPLETE certificate cannot be approved.** If the lab cannot account for every
  test it ordered, no amount of authority makes the sample conforming. Approval is
  refused at the door, not warned about.
"""
from __future__ import annotations

from django.contrib.auth import authenticate
from django.utils import timezone

from .models import Certificate, Signature


class SigningError(Exception):
    """The signature was refused. The message is shown to the signer."""


def sign(certificate, *, user, password, meaning, reason="", ip_address=None) -> Signature:
    """Apply an electronic signature to ``certificate`` after re-authenticating."""
    if meaning not in Signature.Meaning.values:
        raise SigningError(f"Unknown signature meaning '{meaning}'.")

    if certificate.status == Certificate.Status.SUPERSEDED:
        raise SigningError("This certificate has been superseded and cannot be signed.")

    # An incomplete certificate is not a certificate you may approve. Not "approve with
    # a warning" — refuse. The whole point of the incomplete verdict is that the lab
    # cannot say what happened, and authority does not substitute for a missing result.
    if meaning == Signature.Meaning.APPROVED and not certificate.certifiable:
        raise SigningError(
            "This certificate is INCOMPLETE and cannot be approved: "
            f"{certificate.content.get('verdict_reason', '')} "
            "Report the outstanding tests, then re-issue the certificate."
        )

    # §11.200: the signature is a deliberate act, not a side-effect of being logged in.
    if not password or authenticate(username=user.get_username(), password=password) is None:
        raise SigningError("Password does not match. The signature was not applied.")

    signature = Signature.objects.create(
        certificate=certificate,
        signer=user,
        meaning=meaning,
        # Stored, not derived: §11.50 wants the printed name ON the record, and it must
        # survive the user being renamed or deleted.
        printed_name=(user.get_full_name() or user.get_username()),
        signed_at=timezone.now(),
        reason=reason or "",
        # §11.70: bound to what was signed, not merely to which row.
        content_hash=certificate.content_hash,
        ip_address=ip_address,
    )

    if meaning == Signature.Meaning.APPROVED:
        certificate.status = Certificate.Status.APPROVED
    elif meaning == Signature.Meaning.REJECTED:
        certificate.status = Certificate.Status.REJECTED
    certificate.save(update_fields=["status", "updated_at"])

    return signature


def manifestation(certificate) -> list[dict]:
    """The signature block as it appears on the document (§11.50).

    Invalid signatures are NOT hidden. A certificate whose content has moved out from
    under its signature is the single most important thing to show a reader — quietly
    dropping the broken signature would leave an unsigned document that looks merely
    unsigned, rather than tampered with.
    """
    out = []
    for s in certificate.signatures.all():
        out.append({
            "meaning": s.get_meaning_display(),
            "printed_name": s.printed_name,
            "signed_at": s.signed_at.isoformat(),
            "reason": s.reason,
            "valid": s.valid,
            "note": "" if s.valid else (
                "INVALID — the certificate content has changed since this signature "
                "was applied. This document must not be relied upon."),
        })
    return out
