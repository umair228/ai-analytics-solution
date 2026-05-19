"""Uniform API error envelope so the frontend can rely on a stable shape."""
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {"error": True, "detail": response.data}
    return response
