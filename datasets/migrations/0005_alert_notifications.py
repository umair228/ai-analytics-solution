from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("datasets", "0004_alerts_and_params"),
    ]

    operations = [
        migrations.AddField(
            model_name="datasetalert",
            name="notify_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="datasetalert",
            name="notify_webhook",
            field=models.URLField(blank=True),
        ),
    ]
