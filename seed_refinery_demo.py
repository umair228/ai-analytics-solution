"""
Seed the DSE platform with the Refinery LIMS demo (Refinery Lab) — standalone
runner for local/dev use.

The actual seeding logic lives in ``seed_refinery.platform_demo.seed_platform``
(shared with the ``manage.py seed_demo`` management command, which is what you
run on production). This script just configures Django for dev and calls it.

    .venv/bin/python -m seed_refinery.build_warehouse      # build the warehouse
    .venv/bin/python seed_refinery_demo.py                 # wire up the platform

On a server, prefer the management command (honours the prod settings, and also
seeds the users):

    python manage.py seed_demo
"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from seed_refinery.platform_demo import seed_platform  # noqa: E402


def main():
    counts = seed_platform(log=print)
    print("\n✓ Refinery LIMS demo seeded.")
    print(f"  Connection : {counts['connection']}")
    print(f"  Datasets   : {counts['datasets']}")
    print(f"  Dashboards : {counts['dashboards']}")
    print(f"  Alerts     : {counts['alerts']}  |  Events: {counts['events']} "
          f"({counts['unread']} unread)  |  Reports: {counts['reports']}")


if __name__ == "__main__":
    main()
