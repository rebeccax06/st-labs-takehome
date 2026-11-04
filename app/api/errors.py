"""Consistent, sanitized errors with server-generated request correlation IDs."""

import logging

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

from app.api.inventory_service import MissingCI
from app.ai.client import AIError
from app.ingestion.errors import InputError
from app.ingestion.persistence import DatabaseBusy

logger = logging.getLogger(__name__)


def response(request, status, code, message, details=None):
    return JSONResponse(status_code=status, content={"error": dict(
        code=code, message=message, request_id=request.state.request_id, details=details or [])},
        headers={"X-Request-ID": request.state.request_id})


def install_errors(app):
    @app.exception_handler(AIError)
    async def ai_error(request, exc):
        return response(request, exc.status_code, exc.code, str(exc), exc.details)

    @app.exception_handler(InputError)
    async def invalid_input(request, exc):
        return response(request, {"limit_exceeded": 413, "unsupported_encoding": 415, "unsupported_file": 415}.get(exc.code, 422), exc.code, str(exc),
                        [dict(path=exc.path)])

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return response(request, 422, "invalid_request", "Request validation failed", [
            dict(path="/" + "/".join(str(p) for p in error["loc"]), type=error["type"])
            for error in exc.errors()[:50]])

    @app.exception_handler(MissingCI)
    async def missing(request, exc):
        return response(request, 404, "not_found", str(exc))

    @app.exception_handler(DatabaseBusy)
    async def busy(request, exc):
        return response(request, 503, "database_busy", str(exc))

    @app.exception_handler(OperationalError)
    async def database_error(request, exc):
        if any(word in str(exc).lower() for word in ("locked", "busy")):
            return response(request, 503, "database_busy", "Database is busy; retry the request")
        logger.error("Database request failed request_id=%s", request.state.request_id)
        return response(request, 500, "internal_error", "Internal server error")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return response(request, exc.status_code, "http_error", "Request could not be completed")

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        # Avoid serializing SQL parameters, payloads, or exception text into responses/logs.
        logger.error("Request failed request_id=%s category=%s", request.state.request_id, type(exc).__name__)
        return response(request, 500, "internal_error", "Internal server error")
