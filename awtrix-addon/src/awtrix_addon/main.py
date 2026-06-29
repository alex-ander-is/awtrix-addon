from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from aiohttp import web

from .api import make_app
from .auth import AuthManager
from .mqtt import PahoPublisher
from .settings import StartupConfigError, load_settings


def run(options_file: Path, data_dir: Path) -> None:
    try:
        settings = load_settings(options_file)
    except StartupConfigError as exc:
        auth = AuthManager(data_dir, _raw_option_token(options_file))
        app = make_app(None, auth, None, startup_error=exc)
        web.run_app(app, host="0.0.0.0", port=8099)
        return

    mqtt_host = os.environ.get("MQTT_HOST", "core-mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    publisher = PahoPublisher(
        mqtt_host,
        mqtt_port,
        os.environ.get("MQTT_USERNAME"),
        os.environ.get("MQTT_PASSWORD"),
    )
    app = make_app(settings, AuthManager(data_dir, settings.auth_token), publisher)

    async def on_startup(_app: web.Application) -> None:
        await publisher.start()

    async def on_shutdown(_app: web.Application) -> None:
        await _app["store"].shutdown()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    print(json.dumps({"status": "started", "port": 8099, "auth": "option" if settings.auth_token else "generated"}))
    web.run_app(app, host="0.0.0.0", port=8099)


def _raw_option_token(options_file: Path) -> str | None:
    try:
        raw = json.loads(options_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    token = raw.get("auth_token")
    return token if isinstance(token, str) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options-file", type=Path, default=Path("/data/options.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    args = parser.parse_args()
    run(args.options_file, args.data_dir)


if __name__ == "__main__":
    main()
