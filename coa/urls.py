from django.urls import path

from . import views

urlpatterns = [
    path("coa/", views.certificates, name="coa-list"),
    path("coa/<int:pk>/", views.certificate_detail, name="coa-detail"),
    path("coa/<int:pk>/render/", views.certificate_render, name="coa-render"),
    path("coa/<int:pk>/sign/", views.certificate_sign, name="coa-sign"),
]
