from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiohttp import web


@dataclass
class ApiError(Exception):
    status: int
    error: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def error_payload(error: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": error, "message": message, "details": details or {}}


def json_error(status: int, error: str, message: str, details: dict[str, Any] | None = None) -> web.Response:
    return web.json_response(error_payload(error, message, details), status=status)


@web.middleware
async def api_error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        return json_error(exc.status, exc.error, exc.message, exc.details)
    except web.HTTPException as exc:
        if not request.path.startswith("/api/"):
            raise
        if exc.status == 404:
            return json_error(404, "not_found", "API route not found")
        if exc.status == 405:
            allowed = sorted(exc.allowed_methods) if getattr(exc, "allowed_methods", None) else []
            return json_error(
                405,
                "method_not_allowed",
                "Method is not allowed for this API route",
                {"allowed_methods": allowed},
            )
        return json_error(exc.status, exc.reason.lower().replace(" ", "_"), exc.reason)
    except Exception:
        return json_error(500, "internal_error", "Internal server error")
