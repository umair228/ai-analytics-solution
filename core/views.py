from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe."""
    return Response(
        {
            "status": "ok",
            "service": "dse-api",
            "name": "Interactive Reporting & AI Analytics Solution",
            "time": timezone.now().isoformat(),
        }
    )
