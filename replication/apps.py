from django.apps import AppConfig


class ReplicationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "replication"
    verbose_name = "LIMS replica & marts"
