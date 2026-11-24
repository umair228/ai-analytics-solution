"""Give `visibility` a meaning, and make `Lab` a control instead of a decoration.

`"shared"` -> `LAB`, never `-> ORG`. Mapping it to ORG would expose every previously
"shared" object to every authenticated user the instant the migration ran — a silent
widening of access performed by a schema change. Mapping it to LAB widens nothing: every
existing row has `lab = NULL`, and the LAB rule (`lab__in=user.labs.all()`) never matches
NULL, so the same people can see the same objects the day after as the day before.

The reasoning is set out in full in datasets/migrations/0011_visibility_lab_scope.py.
"""
import django.db.models.deletion
from django.db import migrations, models


def shared_becomes_lab(apps, schema_editor):
    apps.get_model("querybuilder", "QueryDefinition").objects.filter(
        visibility="shared").update(visibility="lab")


def lab_becomes_shared(apps, schema_editor):
    apps.get_model("querybuilder", "QueryDefinition").objects.filter(
        visibility__in=["lab", "org"]).update(visibility="shared")


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_hash_api_tokens'),
        ('querybuilder', '0003_queryrun'),
    ]

    operations = [
        migrations.AddField(
            model_name='querydefinition',
            name='lab',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='queries', to='accounts.lab'),
        ),
        migrations.AlterField(
            model_name='querydefinition',
            name='visibility',
            field=models.CharField(choices=[('private', 'Private — only me'), ('lab', 'My laboratory'), ('org', 'Everyone in the organisation')], default='private', max_length=10),
        ),
        migrations.RunPython(shared_becomes_lab, lab_becomes_shared),
    ]
