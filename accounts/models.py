from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


class Organization(TimeStampedModel):
    """The top-level tenant — a company or enterprise."""

    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Site(TimeStampedModel):
    """A physical location / facility belonging to an organization."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="sites"
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30, blank=True)
    location = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("organization", "name")]

    def __str__(self):
        return self.name


class Lab(TimeStampedModel):
    """A laboratory operating within a site."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="labs")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("site", "name")]

    def __str__(self):
        return self.name


class User(AbstractUser):
    """Platform user with a role and multi-site/lab scoping.

    Roles mirror a real petroleum/QC laboratory hierarchy — from the system
    administrator and laboratory management down to the bench analysts and
    support staff who log samples and maintain instruments. The legacy
    ``admin`` / ``analyst`` / ``viewer`` values are retained so existing
    accounts and self-registration keep working.
    """

    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        LAB_MANAGER = "lab_manager", "Laboratory Manager"
        QA_MANAGER = "qa_manager", "QA / QC Manager"
        SECTION_HEAD = "section_head", "Section Head"
        SENIOR_ANALYST = "senior_analyst", "Senior Analyst"
        ANALYST = "analyst", "Analyst / Chemist"
        QC_CHEMIST = "qc_chemist", "QC Chemist"
        LAB_TECHNICIAN = "lab_technician", "Lab Technician"
        SAMPLE_CUSTODIAN = "sample_custodian", "Sample Custodian"
        INSTRUMENT_ENGINEER = "instrument_engineer", "Instrument & Calibration Engineer"
        VIEWER = "viewer", "Viewer / Client"

    # Roles that may create/modify connections, queries, datasets & dashboards.
    # Everyone else (viewer/client and sample custodians) gets read-only access.
    BUILDER_ROLES = frozenset({
        Role.LAB_MANAGER, Role.QA_MANAGER, Role.SECTION_HEAD,
        Role.SENIOR_ANALYST, Role.ANALYST, Role.QC_CHEMIST,
        Role.LAB_TECHNICIAN, Role.INSTRUMENT_ENGINEER,
    })
    # Roles with laboratory-management oversight (not platform administration).
    MANAGER_ROLES = frozenset({
        Role.LAB_MANAGER, Role.QA_MANAGER, Role.SECTION_HEAD,
    })
    # Roles whose work is mostly bench analysis (used for filtering / display).
    ANALYST_ROLES = frozenset({
        Role.SENIOR_ANALYST, Role.ANALYST, Role.QC_CHEMIST,
    })

    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="users",
    )
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.VIEWER)
    sites = models.ManyToManyField(Site, blank=True, related_name="users")
    labs = models.ManyToManyField(Lab, blank=True, related_name="users")
    job_title = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)

    # Lockout after repeated failures (Part 11 §11.300(d)). Deliberately separate from
    # `is_active`: "temporarily locked because someone is guessing your password" and
    # "this person has left the company" are different facts, and conflating them means an
    # attacker can make a leaver look present, or an offboarding look like an attack.
    failed_login_count = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())

    @property
    def is_admin(self) -> bool:
        return self.is_superuser or self.role == self.Role.ADMIN

    @property
    def is_manager(self) -> bool:
        """Laboratory management (lab/QA manager, section head)."""
        return self.role in self.MANAGER_ROLES

    @property
    def is_analyst(self) -> bool:
        return self.role in self.ANALYST_ROLES

    @property
    def can_build(self) -> bool:
        """Whether the user may create/modify connections, queries & dashboards."""
        return self.is_admin or self.role in self.BUILDER_ROLES

    def __str__(self):
        return self.username


class MfaDevice(TimeStampedModel):
    """A second factor — the last open row in SECURITY.md §7.

    Part 11 §11.200(a)(1) requires an electronic signature to use *at least two distinct
    identification components*, and DIS already does that (an identification code and a
    password, re-entered at the point of signing). MFA is therefore not what makes us
    Part 11 compliant — it is what makes the first of those two components worth anything
    once a password has been phished, reused, or read out of a breach dump somewhere else.

    Four decisions, each of which is the difference between a control and a decoration:

    **The secret is encrypted at rest, not hashed.** It cannot be hashed — verifying a code
    means recomputing the HMAC, which needs the key back. So it is a credential in the same
    class as a data-source password and it is stored the same way (Fernet, ``core.encryption``).
    A stolen database must not yield a working authenticator.

    **Enrolment is not complete until a code has been proved.** A secret that becomes active
    the moment it is generated locks the user out if they close the tab before scanning it —
    and then the recovery path is an administrator, at whatever hour they discover it.
    ``confirmed_at`` is set only after they demonstrate the app works.

    **A code cannot be replayed.** A TOTP code is valid for its whole 30-second step, so
    anyone who sees it — over a shoulder, in a screenshot, in a proxy log — can use it again
    inside that window. ``last_step`` records what has been consumed and refuses it twice.
    This is the control that is most often left out, because a demo works fine without it.

    **There are backup codes, and they are hashed and single-use.** A second factor with no
    way past it is a way to lock a chemist out of their own laboratory data at two in the
    morning when their phone is in a taxi. Backup codes are shown exactly once, stored as
    SHA-256, and each dies when used.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mfa",
    )
    # Fernet, via core.encryption — see the class docstring for why this is not a hash.
    secret_encrypted = models.TextField(blank=True, default="")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # The last TOTP time-step this device consumed. Replay protection.
    last_step = models.BigIntegerField(null=True, blank=True)
    # SHA-256 of each unused backup code. Plaintext exists only in the response that
    # created them, exactly once.
    backup_code_hashes = models.JSONField(default=list, blank=True)
    failed_attempts = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "MFA device"

    # ---- the secret -------------------------------------------------------
    @property
    def secret(self) -> str:
        from core.encryption import decrypt

        return decrypt(self.secret_encrypted)

    @secret.setter
    def secret(self, raw: str):
        from core.encryption import encrypt

        self.secret_encrypted = encrypt(raw or "")

    @property
    def is_active(self) -> bool:
        """Enrolled AND proved. A half-finished enrolment must not gate a login."""
        return bool(self.confirmed_at and self.secret_encrypted)

    # ---- verification -----------------------------------------------------
    def verify(self, code: str) -> bool:
        """One code, once.

        Returns True at most once per time-step. The step is recorded before we return, so
        the same code presented a second time inside its 30-second window is refused —
        which is the whole point, and the thing a bool-returning TOTP library makes it easy
        to forget.
        """
        from . import totp

        step = totp.verify(self.secret, code)
        if step is None:
            return False
        if self.last_step is not None and step <= self.last_step:
            return False        # already used — or older than one we have already used
        self.last_step = step
        self.failed_attempts = 0
        self.save(update_fields=["last_step", "failed_attempts", "updated_at"])
        return True

    # ---- backup codes -----------------------------------------------------
    @staticmethod
    def hash_backup(code: str) -> str:
        import hashlib

        return hashlib.sha256(code.strip().replace("-", "").upper().encode()).hexdigest()

    def issue_backup_codes(self, n: int = 10) -> list[str]:
        """Generate, store the hashes, and return the plaintext ONCE.

        Issuing new codes invalidates the old ones. Anything else means a code printed on a
        piece of paper two years ago still opens the account.
        """
        import secrets as _secrets

        codes = []
        for _ in range(n):
            raw = _secrets.token_hex(4).upper()          # 8 hex chars, 32 bits
            codes.append(f"{raw[:4]}-{raw[4:]}")
        self.backup_code_hashes = [self.hash_backup(c) for c in codes]
        self.save(update_fields=["backup_code_hashes", "updated_at"])
        return codes

    def consume_backup_code(self, code: str) -> bool:
        """Single use. A backup code that survives its use is a password with extra steps."""
        digest = self.hash_backup(code)
        if digest not in self.backup_code_hashes:
            return False
        self.backup_code_hashes = [h for h in self.backup_code_hashes if h != digest]
        self.failed_attempts = 0
        self.save(update_fields=["backup_code_hashes", "failed_attempts", "updated_at"])
        return True

    def __str__(self):
        state = "active" if self.is_active else "not confirmed"
        return f"MFA for {self.user} ({state})"


class APIToken(TimeStampedModel):
    """A personal long-lived API token for scripting and external integrations.

    The token is stored as a **hash**. It used to be stored in plaintext, in an indexed
    column, and looked up by equality — which meant anyone with database read, or a copy
    of the nightly dump (which was unencrypted), could impersonate any user. The
    database was a credential store in the worst sense: it held the credentials.

    Now the plaintext exists exactly once, in the HTTP response that creates it. We keep
    a SHA-256 of it and an 8-character prefix so a user can still tell their tokens
    apart. Stealing the database now yields hashes.

    SHA-256 rather than bcrypt/argon2 is deliberate and correct here: the token is 32
    bytes from ``secrets.token_urlsafe``, so it has ~256 bits of entropy and cannot be
    brute-forced. Password hashes are slow *because* passwords are weak; running a slow
    KDF on every authenticated request to protect a value that is already unguessable
    would buy nothing and cost every API call.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(max_length=100)
    # The only form of the token we keep.
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    # Enough to recognise a token in a list, not enough to use one.
    prefix = models.CharField(max_length=12, blank=True)
    # A token that never expires is a credential nobody ever gets round to revoking.
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} / {self.name}"

    @staticmethod
    def hash_key(raw: str) -> str:
        import hashlib
        return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()

    @classmethod
    def generate(cls, user, name, expires_at=None):
        """Create a token and return ``(instance, raw)``. ``raw`` is never stored, and
        this is the only moment it exists."""
        import secrets

        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user, name=name, key_hash=cls.hash_key(raw), prefix=raw[:8],
            expires_at=expires_at,
        )
        return token, raw

    @property
    def expired(self) -> bool:
        from django.utils import timezone
        return bool(self.expires_at and self.expires_at <= timezone.now())
