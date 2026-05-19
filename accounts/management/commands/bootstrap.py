"""Bootstrap a default org/site/lab and an administrator account."""
from decouple import config
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from accounts.models import Lab, Organization, Site

User = get_user_model()


class Command(BaseCommand):
    help = "Create a default Organization/Site/Lab and an admin user."

    def handle(self, *args, **options):
        org, _ = Organization.objects.get_or_create(
            name="Default Organization", defaults={"code": "DEFAULT"}
        )
        site, _ = Site.objects.get_or_create(
            organization=org, name="Main Site", defaults={"code": "MAIN"}
        )
        lab, _ = Lab.objects.get_or_create(
            site=site, name="Main Lab", defaults={"code": "LAB1"}
        )

        username = config("DSE_ADMIN_USERNAME", default="admin")
        password = config("DSE_ADMIN_PASSWORD", default="admin12345")
        email = config("DSE_ADMIN_EMAIL", default="admin@dse.local")

        user, created = User.objects.get_or_create(username=username)
        user.email = email
        user.role = User.Role.ADMIN
        user.organization = org
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        user.sites.add(site)
        user.labs.add(lab)

        verb = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} admin user '{username}' (password: '{password}'). "
                f"Default Organization / Main Site / Main Lab are ready."
            )
        )
