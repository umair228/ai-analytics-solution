from django.urls import path

from . import views

urlpatterns = [
    # The OQ — evidence about the build. Any authenticated user.
    path("validation/oq/", views.oq_report, name="validation-oq"),
    # The IQ — the live posture of THIS deployment. Administrators only: it is an
    # inventory of the ways this installation is currently weak.
    path("validation/posture/", views.posture, name="validation-posture"),
]
