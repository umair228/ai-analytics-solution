"""Root URL configuration for the DSE backend."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    path("api/auth/", include("accounts.urls_auth")),
    path("api/", include("accounts.urls")),
    path("api/", include("connections.urls")),
    path("api/", include("querybuilder.urls")),
    path("api/", include("analytics.urls")),
    path("api/", include("datasets.urls")),
    path("api/", include("dashboards.urls")),
    path("api/", include("ai.urls")),
    path("api/", include("forecasting.urls")),
    path("api/", include("docsearch.urls")),
]

# OIDC SSO routes (login/callback/logout) — only when enabled (see config.settings.sso).
if getattr(settings, "OIDC_ENABLED", False):
    urlpatterns += [path("oidc/", include("mozilla_django_oidc.urls"))]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
