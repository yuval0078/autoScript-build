"""Request identifiers and secret-safe structured HTTP logging."""

import json
import logging
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse


REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
access_logger = logging.getLogger("autoscript.access")
error_logger = logging.getLogger("autoscript.error")


def _safe_identifier(value):
    value = str(value or "").strip()
    return value if _SAFE_ID.fullmatch(value) else None


def request_identifiers(request: Request):
    request_id = _safe_identifier(request.headers.get(REQUEST_ID_HEADER))
    request_id = request_id or str(uuid.uuid4())
    correlation_id = _safe_identifier(request.headers.get(CORRELATION_ID_HEADER))
    return request_id, correlation_id or request_id


def _log(logger, level, payload):
    getattr(logger, level)(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def install_observability(application):
    # Alembic/Uvicorn logging configuration may disable previously-created
    # library loggers. Re-enable these named structured streams per app.
    access_logger.disabled = False
    error_logger.disabled = False
    access_logger.setLevel(logging.INFO)
    error_logger.setLevel(logging.ERROR)

    @application.middleware("http")
    async def request_observability(request: Request, call_next):
        request_id, correlation_id = request_identifiers(request)
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        started = time.perf_counter()
        unhandled_logged = False
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            _log(
                error_logger,
                "error",
                {
                    "event": "http_unhandled_error",
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                    "exception_type": type(exc).__name__,
                },
            )
            unhandled_logged = True
            response = JSONResponse(
                {
                    "detail": "Internal server error.",
                    "request_id": request_id,
                },
                status_code=500,
            )

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        response.headers.setdefault("Cache-Control", "no-store")
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        access_payload = {
            "event": "http_access",
            "request_id": request_id,
            "correlation_id": correlation_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }
        _log(access_logger, "info", access_payload)
        if response.status_code >= 500 and not unhandled_logged:
            _log(error_logger, "error", {**access_payload, "event": "http_server_error"})
        return response

    return application
