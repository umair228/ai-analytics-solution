"""Give `visibility` a meaning, and make `Lab` a control instead of a decoration.

`visibility` was read by nothing: PRIVATE and SHARED behaved identically, because access
was decided entirely by owner/shared_with/admin. It now means something — see
`core/access.py` — which makes the old `"shared"` value a question this migration has to
answer: what did the people who clicked "Shared" actually consent to?

**`"shared"` -> `LAB`, and NOT `-> ORG`.** This is the whole security decision.

Right now a `"shared"` object is visible to its owner and to admins, and to nobody else.
Mapping it to `ORG` would, the instant this migration ran, expose every such object to
every authenticated user in the company — a silent widening of access performed by a
schema change, which nobody would see in a diff and nobody consented to.

Mapping it to `LAB` widens nothing: every existing row has `lab = NULL`, and the LAB rule
is `lab__in=user.labs.all()`, which never matches NULL. So the day after this migration,
exactly the same people can see exactly the same objects as the day before. The value now
*describes* an intent that an owner can act on — set a lab, and their team sees it — but
the migration itself grants nobody anything.

Access-neutral by construction. That is the only kind of access migration worth trusting.
"""
import django.db.models.deletion
from django.db import migrations, models


def shared_becomes_lab(apps, schema_editor):
    apps.get_model("datasets", "Dataset").objects.filter(
        visibility="shared").update(visibility="lab")


def lab_becomes_shared(apps, schema_editor):
    # Reversing is safe for the same reason: "shared" grants nobody anything either.
    apps.get_model("datasets", "Dataset").objects.filter(
        visibility__in=["lab", "org"]).update(visibility="shared")


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_hash_api_tokens'),
        ('datasets', '0010_alertevent_detail_datasetalert_config_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataset',
            name='lab',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='datasets', to='accounts.lab'),
        ),
        migrations.AlterField(
            model_name='dataset',
            name='visibility',
            field=models.CharField(choices=[('private', 'Private — only me'), ('lab', 'My laboratory'), ('org', 'Everyone in the organisation')], default='private', max_length=10),
        ),
        migrations.RunPython(shared_becomes_lab, lab_becomes_shared),
    ]
