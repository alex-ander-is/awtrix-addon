from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from .auth import AuthManager, TokenManagedByOptions
from .errors import ApiError, api_error_middleware, error_payload, json_error
from .lifecycle import DuplicateEventId, EventSpec, EventStore
from .mqtt import Publisher
from .renderer import load_asset
from .settings import Settings, StartupConfigError, invalid_prefix_details, settings_from_options, validate_request_prefixes


def make_app(
    settings: Settings | None,
    auth: AuthManager,
    publisher: Publisher | None,
    *,
    startup_error: StartupConfigError | None = None,
    start_tasks: bool = True,
) -> web.Application:
    app = web.Application(middlewares=[api_error_middleware, auth_middleware, startup_middleware])
    app["settings"] = settings
    app["auth"] = auth
    app["startup_error"] = startup_error
    if settings and publisher:
        app["store"] = EventStore(settings, publisher, start_tasks=start_tasks)
        app["publisher"] = publisher

    app.router.add_get("/health", health)
    app.router.add_post("/api/events", create_event)
    app.router.add_delete("/api/events/current", cancel_current)
    app.router.add_delete("/api/events/{event_id}", cancel_event)
    app.router.add_post("/api/auth/regenerate", regenerate_auth)
    return app


def app_from_options(raw: dict[str, Any], data_dir: Path, publisher: Publisher, *, start_tasks: bool = True) -> web.Application:
    try:
        settings = settings_from_options(raw)
    except StartupConfigError as exc:
        option_token = raw.get("auth_token") if isinstance(raw.get("auth_token"), str) else None
        return make_app(None, AuthManager(data_dir, option_token), None, startup_error=exc, start_tasks=start_tasks)
    return make_app(settings, AuthManager(data_dir, settings.auth_token), publisher, start_tasks=start_tasks)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not header.removeprefix("Bearer ").strip():
        return json_error(401, "auth_required", "Bearer token is required")
    token = header.removeprefix("Bearer ").strip()
    auth: AuthManager = request.app["auth"]
    if not auth.verify(token):
        return json_error(403, "auth_failed", "Bearer token is invalid")
    return await handler(request)


@web.middleware
async def startup_middleware(request: web.Request, handler):
    if request.path.startswith("/api/") and request.app.get("startup_error"):
        exc: StartupConfigError = request.app["startup_error"]
        return json_error(
            503,
            "startup_config_failed",
            "Startup configuration failed",
            {"config_error": exc.redacted()},
        )
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    status = "config_failed" if request.app.get("startup_error") else "ok"
    return web.json_response({"status": status})


async def create_event(request: web.Request) -> web.Response:
    body = await _json_body(request)
    settings: Settings = request.app["settings"]
    try:
        clock_prefixes = validate_request_prefixes(
            body.get("clock_prefixes"),
            allowed=settings.clock_prefixes,
            default=settings.default_clock_prefixes,
        )
    except ValueError:
        raise ApiError(
            400,
            "invalid_clock_prefixes",
            "clock_prefixes must be unique, valid, and allowlisted",
            invalid_prefix_details(body.get("clock_prefixes"), settings.clock_prefixes),
        )
    duration = body.get("duration_seconds", 30)
    if not isinstance(duration, int) or duration <= 0:
        raise ApiError(400, "bad_request", "duration_seconds must be a positive integer")
    asset_name = body.get("asset")
    if asset_name is not None and not isinstance(asset_name, str):
        raise ApiError(400, "bad_request", "asset must be a string")
    try:
        asset = load_asset(settings.assets_dir, asset_name)
    except (FileNotFoundError, ValueError, OSError):
        raise ApiError(400, "bad_request", "asset could not be loaded")

    spec = EventSpec(
        event_id=body.get("event_id") if isinstance(body.get("event_id"), str) else None,
        clock_prefixes=clock_prefixes,
        duration_seconds=duration,
        asset=asset,
        sound=body.get("sound") if isinstance(body.get("sound"), str) else None,
        rtttl=body.get("rtttl") if isinstance(body.get("rtttl"), str) else None,
    )
    store: EventStore = request.app["store"]
    try:
        event_id = await store.create(spec)
    except DuplicateEventId:
        raise ApiError(409, "duplicate_event_id", "event_id already exists")
    return web.json_response({"event_id": event_id, "clock_prefixes": list(clock_prefixes)}, status=201)


async def cancel_current(request: web.Request) -> web.Response:
    body = await _json_body(request, allow_empty=True)
    settings: Settings = request.app["settings"]
    try:
        prefixes = None
        if "clock_prefixes" in body:
            prefixes = validate_request_prefixes(
                body.get("clock_prefixes"),
                allowed=settings.clock_prefixes,
                default=settings.default_clock_prefixes,
            )
    except ValueError:
        raise ApiError(
            400,
            "invalid_clock_prefixes",
            "clock_prefixes must be unique, valid, and allowlisted",
            invalid_prefix_details(body.get("clock_prefixes"), settings.clock_prefixes),
        )
    store: EventStore = request.app["store"]
    restored = await store.cancel_current(prefixes)
    return web.json_response({"restored": restored})


async def cancel_event(request: web.Request) -> web.Response:
    store: EventStore = request.app["store"]
    restored = await store.cancel_event(request.match_info["event_id"])
    return web.json_response({"restored": restored})


async def regenerate_auth(request: web.Request) -> web.Response:
    auth: AuthManager = request.app["auth"]
    try:
        token = auth.regenerate()
    except TokenManagedByOptions:
        raise ApiError(409, "managed_by_options", "Token is managed by add-on options")
    return web.json_response({"token": token})


async def _json_body(request: web.Request, *, allow_empty: bool = False) -> dict[str, Any]:
    if allow_empty and request.content_length in (None, 0):
        return {}
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "bad_request", "Request body must be JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "Request body must be a JSON object")
    return body


def startup_failed_payload(exc: StartupConfigError) -> dict[str, Any]:
    return error_payload("startup_config_failed", "Startup configuration failed", {"config_error": exc.redacted()})
