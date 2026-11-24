from django.urls import path

from . import views

urlpatterns = [
    path("lab-health/", views.health, name="lab-health"),
    path("semantics/", views.layers, name="semantic-layers"),
    path("semantics/<int:pk>/", views.layer_detail, name="semantic-layer-detail"),
]
