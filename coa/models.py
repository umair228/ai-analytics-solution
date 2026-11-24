"""Certificates of Analysis, and the electronic signatures that approve them.

A COA is not a report. It is the document a customer receives that says *this batch
of diesel meets specification*, and somebody's name is on it. If DIS issues one and it
is wrong, that is not a bad chart — it is a released batch that should not have been.

So this module is built around three invariants, and each of them is a test:

1. **An incomplete certificate never reads as passing.** LabWare's TEST table records
   what was *ordered*; RESULT records what came *back*. A test that was ordered and
   never reported is the dangerous case: a naive builder renders a table with a quiet
   gap and an overall PASS, which is a batch released without being fully tested. Any
   unreconciled test forces the verdict to INCOMPLETE, and the missing tests are
   printed on the face of the document.

2. **A signature is bound to the bytes it signed.** ``Signature.content_hash`` is the
   SHA-256 of the certificate content at the moment of signing. If the content later
   changes, the hashes no longer match and the signature is *invalid* — it cannot be
   excised, copied, or transferred to another record (21 CFR Part 11 §11.70).

3. **The LLM is nowhere near the numbers.** Every figure comes from certified SQL
   against the LIMS. The model may summarise a certificate; it may never produce one.
"""
from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


def content_hash(content: dict) -> str:
    """The canonical hash of a certificate's content — the thing a signature binds to.

    Canonical means sort_keys + tight separators: two dicts that mean the same thing
    must hash the same, or a signature would spuriously invalidate on a re-serialise.
    """
    blob = json.dumps(content or {}, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Certificate(TimeStampedModel):
    """A Certificate of Analysis for one LIMS sample, as issued."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded"

    class Verdict(models.TextChoices):
        CONFORMS = "conforms", "Conforms to specification"
        DOES_NOT_CONFORM = "does_not_conform", "Does NOT conform"
        # Deliberately NOT a pass. A certificate that cannot account for every test
        # ordered has no business saying the sample conforms.
        INCOMPLETE = "incomplete", "Incomplete — cannot certify"

    number = models.CharField(max_length=40, unique=True)
    datasource = models.ForeignKey("connections.DataSource", on_delete=models.PROTECT,
                                   related_name="certificates")
    sample_number = models.CharField(max_length=64)
    sample_text_id = models.CharField(max_length=120, blank=True)
    product = models.CharField(max_length=200, blank=True)

    # The immutable snapshot. A COA certifies the data AS IT WAS when issued; if the
    # LIMS is later corrected, that is a NEW certificate superseding this one, not a
    # silent edit to a document a customer already holds.
    content = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64, editable=False)

    verdict = models.CharField(max_length=20, choices=Verdict.choices)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.DRAFT)

    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL,
                                  related_name="certificates_issued")
    superseded_by = models.ForeignKey("self", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="supersedes")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["sample_number"]),
                   models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.number} ({self.get_verdict_display()})"

    def save(self, *args, **kwargs):
        # The hash is derived, never supplied. Otherwise a caller could hand us a hash
        # that matches a signature while the content says something else entirely.
        self.content_hash = content_hash(self.content)
        super().save(*args, **kwargs)

    # ── signature integrity ────────────────────────────────────────────────
    @property
    def valid_signatures(self):
        """Signatures still bound to the current content (Part 11 §11.70)."""
        return [s for s in self.signatures.all() if s.content_hash == self.content_hash]

    @property
    def broken_signatures(self):
        """Signatures whose record has changed underneath them. Should be empty; if it
        is not, the certificate has been tampered with and must not be relied upon."""
        return [s for s in self.signatures.all() if s.content_hash != self.content_hash]

    @property
    def is_approved(self) -> bool:
        return (self.status == self.Status.APPROVED
                and any(s.meaning == Signature.Meaning.APPROVED
                        for s in self.valid_signatures))

    @property
    def certifiable(self) -> bool:
        """Can this even be approved? An INCOMPLETE certificate cannot."""
        return self.verdict in (self.Verdict.CONFORMS, self.Verdict.DOES_NOT_CONFORM)


class Signature(TimeStampedModel):
    """An electronic signature — 21 CFR Part 11 subpart C.

    §11.50 manifestation: printed name, date/time, and the MEANING of the signature.
    §11.70 record linking: bound to the content hash, so it cannot be excised, copied
    or transferred to another record.
    §11.200 components: applied only after re-authentication (see ``coa.signing``).
    """

    class Meaning(models.TextChoices):
        REVIEWED = "reviewed", "Reviewed by"
        APPROVED = "approved", "Approved by"
        REJECTED = "rejected", "Rejected by"

    certificate = models.ForeignKey(Certificate, on_delete=models.PROTECT,
                                    related_name="signatures")
    signer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="signatures")
    meaning = models.CharField(max_length=12, choices=Meaning.choices)

    # The manifestation is stored, not derived: §11.50 wants the printed name ON the
    # record. If the user is later renamed or deleted, the signature must still say
    # who signed it.
    printed_name = models.CharField(max_length=200)
    signed_at = models.DateTimeField()
    reason = models.TextField(blank=True)

    # What was signed. Not "which record" — WHAT.
    content_hash = models.CharField(max_length=64)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["signed_at"]

    def __str__(self):
        return f"{self.get_meaning_display()} {self.printed_name} @ {self.signed_at:%Y-%m-%d %H:%M}"

    @property
    def valid(self) -> bool:
        return self.content_hash == self.certificate.content_hash
