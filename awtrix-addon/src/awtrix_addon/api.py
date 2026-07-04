from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from aiohttp import web

from .auth import AuthManager, TokenManagedByOptions
from .errors import ApiError, api_error_middleware, error_payload, json_error
from .lifecycle import EventSpec, EventStore
from .melodies import MelodyError, MelodyLibrary, MelodyNotFound, validate_rtttl
from .mqtt import Publisher
from .renderer import load_asset, load_asset_bytes
from .settings import Settings, StartupConfigError, invalid_prefix_details, settings_from_options, validate_request_prefixes


def make_app(
    settings: Settings | None,
    auth: AuthManager,
    publisher: Publisher | None,
    *,
    data_dir: Path = Path("/data"),
    startup_error: StartupConfigError | None = None,
    start_tasks: bool = True,
) -> web.Application:
    app = web.Application(middlewares=[api_error_middleware, auth_middleware, startup_middleware])
    app["settings"] = settings
    app["auth"] = auth
    app["startup_error"] = startup_error
    if settings and publisher:
        attach_runtime(app, settings, publisher, data_dir=data_dir, start_tasks=start_tasks)

    app.router.add_get("/health", health)
    app.router.add_post("/api/events", create_event)
    app.router.add_delete("/api/events/current", cancel_current)
    app.router.add_delete("/api/events/{event_id}", cancel_event)
    app.router.add_post("/api/auth/regenerate", regenerate_auth)
    return app


def attach_runtime(
    app: web.Application,
    settings: Settings,
    publisher: Publisher,
    *,
    data_dir: Path = Path("/data"),
    start_tasks: bool = True,
) -> None:
    app["settings"] = settings
    app["store"] = EventStore(settings, publisher, start_tasks=start_tasks)
    app["melody_library"] = MelodyLibrary(data_dir)
    app["publisher"] = publisher


def app_from_options(raw: dict[str, Any], data_dir: Path, publisher: Publisher, *, start_tasks: bool = True) -> web.Application:
    try:
        settings = settings_from_options(raw)
    except StartupConfigError as exc:
        option_token = raw.get("auth_token") if isinstance(raw.get("auth_token"), str) else None
        return make_app(None, AuthManager(data_dir, option_token), None, data_dir=data_dir, startup_error=exc, start_tasks=start_tasks)
    return make_app(settings, AuthManager(data_dir, settings.auth_token), publisher, data_dir=data_dir, start_tasks=start_tasks)


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
    asset = _request_asset(settings, body)
    rtttl = _request_rtttl(body, request.app["melody_library"])

    spec = EventSpec(
        event_id=body.get("event_id") if isinstance(body.get("event_id"), str) else None,
        clock_prefixes=clock_prefixes,
        duration_seconds=duration,
        asset=asset,
        rtttl=rtttl,
    )
    store: EventStore = request.app["store"]
    event_id = await store.create(spec)
    return web.json_response({"event_id": event_id, "clock_prefixes": list(clock_prefixes)}, status=201)


def _request_rtttl(body: dict[str, Any], library: MelodyLibrary) -> str | None:
    raw = body.get("rtttl")
    melody = body.get("melody")
    if raw is not None and not isinstance(raw, str):
        raise ApiError(400, "invalid_rtttl", "rtttl must be a string")
    if melody is not None and not isinstance(melody, str):
        raise ApiError(400, "invalid_melody", "melody must be a string")
    raw = raw or None
    melody = melody or None
    if raw and melody:
        raise ApiError(400, "invalid_melody", "melody and rtttl are mutually exclusive")
    try:
        if melody:
            return library.resolve(melody)
        return validate_rtttl(raw) if raw else None
    except MelodyNotFound:
        raise ApiError(404, "melody_not_found", "Melody was not found", {"melody": melody}) from None
    except MelodyError:
        if melody:
            raise ApiError(400, "invalid_melody", "melody must be a valid library reference") from None
        raise ApiError(400, "invalid_rtttl", "rtttl must be a valid RTTTL expression") from None


def _request_asset(settings: Settings, body: dict[str, Any]):
    asset_name = body.get("asset")
    asset_base64 = body.get("asset_base64")
    if asset_name is not None and not isinstance(asset_name, str):
        raise ApiError(400, "bad_request", "asset must be a string")
    if asset_base64 is not None and not isinstance(asset_base64, str):
        raise ApiError(400, "bad_request", "asset_base64 must be a string")
    if asset_name and asset_base64:
        raise ApiError(400, "bad_request", "asset and asset_base64 are mutually exclusive")
    try:
        if asset_base64:
            return load_asset_bytes(_decode_asset_base64(asset_base64))
        return load_asset(settings.assets_dir, asset_name)
    except (FileNotFoundError, ValueError, OSError, binascii.Error):
        raise ApiError(400, "bad_request", "asset could not be loaded")


def _decode_asset_base64(value: str) -> bytes:
    encoded = value.strip()
    if encoded.startswith("data:"):
        header, separator, payload = encoded.partition(",")
        if not separator or ";base64" not in header:
            raise ValueError("asset_base64 data URL must be base64")
        encoded = payload
    return base64.b64decode(encoded, validate=True)


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
        raise ApiError(409, "managed_by_options", "Token is managed by App options")
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
