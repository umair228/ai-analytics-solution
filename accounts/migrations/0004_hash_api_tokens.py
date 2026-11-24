"""Stop storing API tokens in plaintext.

Every token was held in an indexed plaintext column and looked up by equality, so
database read — or a copy of the nightly dump, which was unencrypted — was enough to
impersonate any user. SECURITY.md §7 has listed this as a blocker; `manage.py preflight`
reports it on every run.

The migration is deliberately non-disruptive: because the plaintext is *currently*
present, we can hash it in place. Existing tokens therefore keep working — no integration
breaks, nobody has to reissue anything — and at the end of the migration the plaintext is
**deleted from the database and cannot be recovered**, which is the entire point.

There is no reverse. Un-hashing is not a thing, and a reverse that silently issued fresh
plaintext tokens would be a security regression disguised as a rollback.
"""
import hashlib

from django.db import migrations, models


def hash_existing_tokens(apps, schema_editor):
    # `key_hash` gets a UNIQUE constraint immediately after this runs, so a collision here
    # would abort the migration halfway. It cannot happen: the old `key` was declared
    # CharField(unique=True) with no null=True (see 0002), so it is NOT NULL and unique —
    # at most one row can hold "", hence at most one row can hash to the empty digest.
    APIToken = apps.get_model("accounts", "APIToken")
    for token in APIToken.objects.all().iterator():
        raw = token.key or ""
        token.key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        token.prefix = raw[:8]
        token.save(update_fields=["key_hash", "prefix"])


def irreversible(apps, schema_editor):
    raise RuntimeError(
        "Reversing this migration would mean recovering plaintext tokens from their "
        "hashes, which is not possible — and issuing new plaintext ones in their place "
        "would be a security regression wearing a rollback's clothes. Revoke and reissue "
        "the affected tokens instead."
    )


class Migration(migrations.Migration):

    dependencies = [("accounts", "0003_alter_user_role")]

    operations = [
        migrations.AddField(
            model_name="apitoken",
            name="key_hash",
            field=models.CharField(db_index=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="apitoken",
            name="prefix",
            field=models.CharField(blank=True, default="", max_length=12),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="apitoken",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(hash_existing_tokens, irreversible),
        migrations.AlterField(
            model_name="apitoken",
            name="key_hash",
            field=models.CharField(db_index=True, max_length=64, unique=True),
        ),
        # And now the plaintext is gone.
        migrations.RemoveField(model_name="apitoken", name="key"),
    ]
