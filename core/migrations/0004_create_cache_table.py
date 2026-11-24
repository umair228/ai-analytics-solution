"""Create the database cache table when DIS_CACHE=db (no-op otherwise).

The shared cache (settings.CACHES, "dis_cache") must exist before the first request
reads it; ``createcachetable`` is idempotent and only touches database caches, so a
deployment on the default per-process cache is unaffected. A later switch to
DIS_CACHE=db needs ``manage.py createcachetable`` once (this migration has run by then).
"""
from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_auditlog_export_send_actions"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
