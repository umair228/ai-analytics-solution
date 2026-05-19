import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datasets", "0005_alert_notifications"),
    ]

    operations = [
        migrations.CreateModel(
            name="DatasetReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("recipient_emails", models.TextField(help_text="Comma-separated list of recipient email addresses")),
                ("schedule", models.CharField(choices=[("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")], default="daily", max_length=10)),
                ("is_active", models.BooleanField(default=True)),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
                ("dataset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reports", to="datasets.dataset")),
            ],
            options={"ordering": ["name"]},
        ),
    ]
