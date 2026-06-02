from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

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


class APIToken(TimeStampedModel):
    """A personal long-lived API token for scripting and external integrations."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(max_length=100)
    key = models.CharField(max_length=64, unique=True, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} / {self.name}"
