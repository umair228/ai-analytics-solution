import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("datasets", "0003_dataset_next_refresh_at_dataset_refresh_interval"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # param_defaults on Dataset
        migrations.AddField(
            model_name="dataset",
            name="param_defaults",
            field=models.JSONField(blank=True, default=dict),
        ),
        # DatasetAlert model
        migrations.CreateModel(
            name="DatasetAlert",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("column", models.CharField(max_length=200)),
                ("aggregation", models.CharField(
                    choices=[("avg","Average"),("sum","Sum"),("count","Count"),
                             ("max","Maximum"),("min","Minimum"),("last","Last value")],
                    default="avg", max_length=10,
                )),
                ("condition", models.CharField(
                    choices=[("gt","Greater than"),("gte","Greater than or equal"),
                             ("lt","Less than"),("lte","Less than or equal"),("eq","Equal to")],
                    max_length=5,
                )),
                ("threshold", models.FloatField()),
                ("is_active", models.BooleanField(default=True)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_triggered_at", models.DateTimeField(blank=True, null=True)),
                ("dataset", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="alerts", to="datasets.dataset",
                )),
                ("owner", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="alerts", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"ordering": ["name"]},
        ),
        # AlertEvent model
        migrations.CreateModel(
            name="AlertEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("triggered_value", models.FloatField()),
                ("message", models.TextField()),
                ("acknowledged", models.BooleanField(default=False)),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("alert", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="events", to="datasets.datasetalert",
                )),
                ("acknowledged_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="acknowledged_events", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
