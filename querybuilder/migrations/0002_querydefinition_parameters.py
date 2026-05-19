from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("querybuilder", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="querydefinition",
            name="parameters",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
