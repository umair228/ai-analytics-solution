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
    # Where a number came from and how old it is (lineage/freshness), and the
    # curated layer the trust story rests on (data dictionary + certified metrics).
    # Both apps shipped with no API at all, so neither was visible to a customer.
    path("api/", include("replication.urls")),
    path("api/", include("semantics.urls")),
    # Certificates of Analysis + Part 11 electronic signatures.
    path("api/", include("coa.urls")),
    path("api/", include("validation.urls")),
]

# OIDC SSO routes (login/callback/logout) — only when enabled (see config.settings.sso).
if getattr(settings, "OIDC_ENABLED", False):
    urlpatterns += [path("oidc/", include("mozilla_django_oidc.urls"))]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
