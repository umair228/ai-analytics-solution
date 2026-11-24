from django.urls import path

from . import views

urlpatterns = [
    path("lineage/", views.lineage, name="replication-lineage"),
    path("lineage/freshness/", views.freshness, name="replication-freshness"),
]
