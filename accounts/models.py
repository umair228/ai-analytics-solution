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
    """Platform user with a role and multi-site/lab scoping."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        ANALYST = "analyst", "Analyst"
        VIEWER = "viewer", "Viewer"

    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="users",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)
    sites = models.ManyToManyField(Site, blank=True, related_name="users")
    labs = models.ManyToManyField(Lab, blank=True, related_name="users")
    job_title = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)

    @property
    def is_admin(self) -> bool:
        return self.is_superuser or self.role == self.Role.ADMIN

    @property
    def is_analyst(self) -> bool:
        return self.role == self.Role.ANALYST

    @property
    def can_build(self) -> bool:
        """Whether the user may create/modify connections, queries & dashboards."""
        return self.is_admin or self.role == self.Role.ANALYST

    def __str__(self):
        return self.username
