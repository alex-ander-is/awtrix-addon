from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from aiohttp import web

from .api import make_app
from .auth import AuthManager
from .mqtt import PahoPublisher
from .settings import Settings
from .settings import StartupConfigError, load_settings


SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"


def load_mqtt_credentials() -> tuple[str, int, str, str]:
    """Read the mqtt:want connection details without exposing them outside startup."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise StartupConfigError("mqtt_credentials_unavailable", "MQTT credentials are unavailable")

    request = Request(SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=10) as response:  # nosec B310: fixed Supervisor-only URL
            payload = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError):
        raise StartupConfigError("mqtt_credentials_unavailable", "MQTT credentials are unavailable") from None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise StartupConfigError("mqtt_credentials_invalid", "MQTT credentials are invalid")
    host = data.get("host")
    port = data.get("port")
    username = data.get("username")
    password = data.get("password")
    if (
        not isinstance(host, str)
        or not host
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(username, str)
        or not username
        or not isinstance(password, str)
        or not password
    ):
        raise StartupConfigError("mqtt_credentials_invalid", "MQTT credentials are invalid")
    return host, port, username, password


def run(options_file: Path, data_dir: Path) -> None:
    try:
        settings = load_settings(options_file)
    except StartupConfigError as exc:
        auth = AuthManager(data_dir, _raw_option_token(options_file))
        app = make_app(None, auth, None, data_dir=data_dir, startup_error=exc)
        web.run_app(app, host="0.0.0.0", port=8099)
        return

    auth = AuthManager(data_dir, settings.auth_token)
    try:
        mqtt_host, mqtt_port, mqtt_username, mqtt_password = load_mqtt_credentials()
    except StartupConfigError as exc:
        app = make_app(None, auth, None, data_dir=data_dir, startup_error=exc)
        web.run_app(app, host="0.0.0.0", port=8099)
        return
    publisher = PahoPublisher(mqtt_host, mqtt_port, mqtt_username, mqtt_password)
    del mqtt_username, mqtt_password
    app = make_app(settings, auth, publisher, data_dir=data_dir)

    async def on_startup(_app: web.Application) -> None:
        await publisher.start()

    async def on_shutdown(_app: web.Application) -> None:
        await _app["store"].shutdown()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    for line in startup_log_lines(settings, auth):
        print(line)
    web.run_app(app, host="0.0.0.0", port=8099)


def startup_log_lines(settings: Settings, auth: AuthManager) -> list[str]:
    lines = [json.dumps({"status": "started", "port": 8099, "auth": "option" if settings.auth_token else "generated"})]
    if settings.auth_token:
        return lines

    token = auth.active_token()
    lines.extend(
        [
            "AWTRIX App generated auth token.",
            f"Use in HA secrets.yaml: awtrix_addon_authorization: Bearer {token}",
            "Token is stored in /data/auth.json",
        ]
    )
    return lines


def _raw_option_token(options_file: Path) -> str | None:
    try:
        raw = json.loads(options_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    token = raw.get("auth_token")
    return token if isinstance(token, str) and token else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options-file", type=Path, default=Path("/data/options.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    args = parser.parse_args()
    run(args.options_file, args.data_dir)


if __name__ == "__main__":
    main()
