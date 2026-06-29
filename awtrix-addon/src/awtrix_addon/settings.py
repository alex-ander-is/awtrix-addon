from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_PREFIX_LEN = 160
ALLOWED_OPTION_KEYS = {"app_name", "clock_prefixes", "default_clock_prefixes", "assets_dir", "auth_token"}


class StartupConfigError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def redacted(self) -> dict[str, Any]:
        return {"code": self.code, "details": self.details}


@dataclass(frozen=True)
class Settings:
    app_name: str
    clock_prefixes: tuple[str, ...]
    default_clock_prefixes: tuple[str, ...]
    assets_dir: Path
    auth_token: str | None = None


def load_settings(path: Path = Path("/data/options.json")) -> Settings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {
            "app_name": "awtrix_addon",
            "clock_prefixes": ["awtrix"],
            "assets_dir": "/share/awtrix-addon/assets",
        }
    except json.JSONDecodeError as exc:
        raise StartupConfigError("invalid_options_json", "Options JSON is invalid") from exc
    if not isinstance(raw, dict):
        raise StartupConfigError("invalid_options", "Options must be an object")
    return settings_from_options(raw)


def settings_from_options(raw: dict[str, Any]) -> Settings:
    unknown = sorted(set(raw) - ALLOWED_OPTION_KEYS)
    if unknown:
        raise StartupConfigError("invalid_options", "Options contain unsupported keys", {"invalid": unknown})

    app_name = raw.get("app_name", "awtrix_addon")
    if not isinstance(app_name, str) or not APP_NAME_RE.fullmatch(app_name):
        raise StartupConfigError("invalid_app_name", "app_name must be one MQTT segment")

    clock_prefixes = _validate_prefix_list(raw.get("clock_prefixes"), required=True)
    default_raw = raw.get("default_clock_prefixes")
    if default_raw is None or default_raw == []:
        default_clock_prefixes = clock_prefixes
    else:
        default_clock_prefixes = _validate_prefix_list(default_raw, required=False)
        missing = [prefix for prefix in default_clock_prefixes if prefix not in clock_prefixes]
        if missing:
            raise StartupConfigError(
                "invalid_default_clock_prefixes",
                "default_clock_prefixes must be a subset of clock_prefixes",
                {"invalid": missing, "allowed": list(clock_prefixes)},
            )

    assets_dir = raw.get("assets_dir", "/share/awtrix-addon/assets")
    if not isinstance(assets_dir, str) or not assets_dir:
        raise StartupConfigError("invalid_assets_dir", "assets_dir must be a path string")

    auth_token = raw.get("auth_token")
    if auth_token == "":
        auth_token = None
    if auth_token is not None and not isinstance(auth_token, str):
        raise StartupConfigError("invalid_auth_token", "auth_token must be a string")

    return Settings(
        app_name=app_name,
        clock_prefixes=clock_prefixes,
        default_clock_prefixes=default_clock_prefixes,
        assets_dir=Path(assets_dir),
        auth_token=auth_token,
    )


def validate_request_prefixes(value: Any, *, allowed: tuple[str, ...], default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    prefixes = _validate_prefix_list(value, required=True, error_type=ValueError)
    invalid = [prefix for prefix in prefixes if prefix not in allowed]
    if invalid:
        raise ValueError(",".join(invalid))
    return prefixes


def invalid_prefix_details(value: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    invalid: list[str] = []
    if not isinstance(value, list) or not value:
        invalid.append("<empty>")
    else:
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                invalid.append(str(item))
                continue
            if item in seen or not _is_clean_prefix(item):
                invalid.append(item)
            elif item not in allowed:
                invalid.append(item)
            seen.add(item)
    return {"invalid": invalid, "allowed": list(allowed)}


def _validate_prefix_list(
    value: Any,
    *,
    required: bool,
    error_type: type[Exception] = StartupConfigError,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        _raise(error_type, "invalid_clock_prefixes", "clock_prefixes must be a non-empty list")
    seen: set[str] = set()
    prefixes: list[str] = []
    for item in value:
        if not isinstance(item, str) or item in seen or not _is_clean_prefix(item):
            _raise(error_type, "invalid_clock_prefixes", "clock_prefixes contains invalid values")
        seen.add(item)
        prefixes.append(item)
    return tuple(prefixes)


def _is_clean_prefix(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= MAX_PREFIX_LEN
        and not value.startswith("/")
        and not value.endswith("/")
        and "//" not in value
        and "+" not in value
        and "#" not in value
        and "\x00" not in value
        and all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)
    )


def _raise(error_type: type[Exception], code: str, message: str) -> None:
    if error_type is StartupConfigError:
        raise StartupConfigError(code, message)
    raise error_type(message)
