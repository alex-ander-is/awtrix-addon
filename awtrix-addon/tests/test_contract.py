from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awtrix_addon.api import app_from_options, cancel_current, create_event, health, make_app, regenerate_auth
from awtrix_addon.api import cancel_event as cancel_event_handler
from awtrix_addon.api import auth_middleware, startup_middleware
from awtrix_addon import main as main_module
from awtrix_addon.auth import AuthManager, TokenManagedByOptions
from awtrix_addon.errors import api_error_middleware
from awtrix_addon.lifecycle import EventSpec, EventStore
from awtrix_addon.mqtt import MemoryPublisher, PahoPublisher
from awtrix_addon.renderer import AssetAnimation, blank_asset, load_asset, load_asset_bytes, render_frame
from awtrix_addon.settings import StartupConfigError, settings_from_options


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures"
SECRET_LINE_RE = re.compile(r"^\s*[A-Za-z0-9_-]+:\s*!secret\s+[A-Za-z0-9_-]+\s*(?:#.*)?$")
FORBIDDEN_ARTIFACT_IMAGE_RE = re.compile(
    r"^(?:screenshot(?:[-_].*)?|canvas(?:[-_].*)?|canvas-dump.*)\.(?:png|jpe?g)$",
    re.IGNORECASE,
)


def base_options(tmp: Path, **overrides):
    options = {
        "app_name": "awtrix_addon",
        "clock_prefixes": ["clock/kitchen", "clock/office"],
        "assets_dir": str(tmp),
        "auth_token": "secret-token",
    }
    options.update(overrides)
    return options


class FakeRequest:
    def __init__(self, app, path, *, headers=None, body=None, match_info=None):
        self.app = app
        self.path = path
        self.headers = headers or {}
        self._body = body
        self.match_info = match_info or {}
        self.content_length = 0 if body is None else 1

    async def json(self):
        if self._body is None:
            raise ValueError("empty")
        return self._body


class FakeSupervisorResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return False

    def read(self):
        return self.payload


class FakePahoClient:
    def __init__(self, connack, *, connect_result=0):
        self.connack = connack
        self.connect_result = connect_result
        self.calls: list[str] = []
        self.username_password = None
        self.on_connect = None
        self.connected = False
        self.published: list[tuple[str, str | bytes, int, bool]] = []

    def username_pw_set(self, username, password):
        self.calls.append("username_pw_set")
        self.username_password = (username, password)

    def connect(self, host, port, keepalive):
        self.calls.append("connect")
        self.address = (host, port, keepalive)
        return self.connect_result

    def loop_start(self):
        self.calls.append("loop_start")
        if self.connack is not None:
            self.on_connect(self, None, None, self.connack)
            self.connected = self.connack == 0

    def loop_stop(self):
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")
        self.connected = False

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return type("PublishResult", (), {"rc": 0})()


class FakeRecoveringPublisher:
    def __init__(self, *_args, **_kwargs):
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def publish(self, _topic, _payload):
        return None


class FailOncePublisher(MemoryPublisher):
    def __init__(self):
        super().__init__()
        self.fail_topic: str | None = None

    async def publish(self, topic: str, payload: str | bytes) -> None:
        if topic == self.fail_topic:
            self.fail_topic = None
            raise RuntimeError("planned publish failure")
        await super().publish(topic, payload)


async def dispatch(app, handler, path, *, headers=None, body=None, match_info=None):
    request = FakeRequest(app, path, headers=headers, body=body, match_info=match_info)

    async def final(req):
        return await handler(req)

    return await api_error_middleware(
        request,
        lambda req: auth_middleware(req, lambda req2: startup_middleware(req2, final)),
    )


def response_json(response):
    return json.loads(response.text)


def readme_yaml_blocks() -> list[str]:
    readme = REPO_ROOT.joinpath("README.md").read_text(encoding="utf-8")
    return re.findall(r"```ya?ml\n(.*?)```", readme, flags=re.DOTALL)


def assert_full_scalar_secrets_only(testcase: unittest.TestCase, yaml_text: str) -> None:
    for line in yaml_text.splitlines():
        if "!secret" not in line:
            continue
        testcase.assertRegex(line, SECRET_LINE_RE, msg=f"invalid embedded !secret usage: {line}")


def is_forbidden_artifact(path: Path) -> bool:
    name = path.name
    return (
        name == ".DS_Store"
        or name == ".env"
        or name.startswith(".env.")
        or name == "auth.json"
        or name == "options.json"
        or name.endswith(".log")
        or FORBIDDEN_ARTIFACT_IMAGE_RE.fullmatch(name) is not None
    )


def is_ignored_untracked_file(path: Path) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", path.relative_to(REPO_ROOT).as_posix()],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def repository_files() -> list[Path]:
    files: list[Path] = []
    stack = [REPO_ROOT]
    while stack:
        directory = stack.pop()
        for child in directory.iterdir():
            if child.is_dir():
                if child.name in {".git", ".codex-audit"}:
                    continue
                stack.append(child)
            elif child.is_file():
                files.append(child)
    return files


def frame_bitmap(image: Image.Image) -> tuple[str, ...]:
    rgb = image.convert("RGB")
    return tuple(
        "".join("#" if rgb.getpixel((x, y)) == (255, 255, 255) else "." for x in range(rgb.width))
        for y in range(rgb.height)
    )


class ConfigAuthTests(unittest.TestCase):
    def test_default_prefixes_and_strict_config(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_from_options(
                {
                    "app_name": "awtrix_addon",
                    "clock_prefixes": ["clock/a", "clock/b"],
                    "assets_dir": directory,
                }
            )
            self.assertEqual(settings.default_clock_prefixes, ("clock/a", "clock/b"))
            self.assertIsNone(
                settings_from_options(
                    {
                        "app_name": "awtrix_addon",
                        "clock_prefixes": ["clock/a", "clock/b"],
                        "default_clock_prefixes": [],
                        "assets_dir": directory,
                        "auth_token": "",
                    }
                ).auth_token
            )

            with self.assertRaises(StartupConfigError):
                settings_from_options({**base_options(Path(directory)), "extra": True})
            with self.assertRaises(StartupConfigError):
                settings_from_options({**base_options(Path(directory)), "app_name": "bad/name"})
            with self.assertRaises(StartupConfigError):
                settings_from_options({**base_options(Path(directory)), "clock_prefixes": ["bad/#"]})
            with self.assertRaises(StartupConfigError):
                settings_from_options(
                    {
                        **base_options(Path(directory)),
                        "default_clock_prefixes": ["clock/missing"],
                    }
                )

    def test_generated_token_persists_and_option_has_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            generated = AuthManager(data_dir)
            token = generated.active_token()
            self.assertEqual(AuthManager(data_dir).active_token(), token)
            self.assertEqual(json.loads((data_dir / "auth.json").read_text())["token"], token)

            option = AuthManager(data_dir, "from-options")
            self.assertEqual(option.active_token(), "from-options")
            with self.assertRaises(TokenManagedByOptions):
                option.regenerate()


class SupervisorMqttTests(unittest.IsolatedAsyncioTestCase):
    def test_supervisor_credentials_use_bearer_and_ignore_mqtt_environment(self):
        supervisor_token = "supervisor-token"
        mqtt_password = "mqtt-password"
        response = FakeSupervisorResponse(
            {"data": {"host": "core-mosquitto", "port": 1883, "username": "awtrix", "password": mqtt_password}}
        )
        with (
            patch.dict(
                os.environ,
                {
                    "SUPERVISOR_TOKEN": supervisor_token,
                    "MQTT_HOST": "ignored-host",
                    "MQTT_PORT": "1999",
                    "MQTT_USERNAME": "ignored-user",
                    "MQTT_PASSWORD": "ignored-password",
                },
                clear=True,
            ),
            patch.object(main_module, "urlopen", return_value=response) as opener,
        ):
            credentials = main_module.load_mqtt_credentials()

        self.assertEqual(credentials, ("core-mosquitto", 1883, "awtrix", mqtt_password))
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://supervisor/services/mqtt")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {supervisor_token}")

    def test_invalid_supervisor_credentials_never_fall_back_or_leak(self):
        password = "do-not-leak"
        with (
            patch.dict(
                os.environ,
                {"SUPERVISOR_TOKEN": "supervisor-token", "MQTT_HOST": "fallback", "MQTT_PASSWORD": password},
                clear=True,
            ),
            patch.object(main_module, "urlopen", return_value=FakeSupervisorResponse({"data": {"host": "", "port": "1883"}})),
            self.assertRaises(StartupConfigError) as raised,
        ):
            main_module.load_mqtt_credentials()

        self.assertEqual(raised.exception.code, "mqtt_credentials_invalid")
        self.assertNotIn(password, str(raised.exception))
        self.assertNotIn(password, json.dumps(raised.exception.redacted()))

    def test_supervisor_failure_is_redacted_from_startup_app(self):
        supervisor_token = "supervisor-token"
        mqtt_password = "do-not-leak"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options_file = root / "options.json"
            options_file.write_text(json.dumps(base_options(root)), encoding="utf-8")
            captured = {}

            def capture_run_app(app, **_kwargs):
                captured["app"] = app

            async def failed_recovery(*_args, **_kwargs):
                raise StartupConfigError("mqtt_credentials_invalid", "MQTT credentials are invalid")

            with (
                patch.dict(os.environ, {"SUPERVISOR_TOKEN": supervisor_token}, clear=True),
                patch.object(main_module, "_recover_mqtt_runtime", side_effect=failed_recovery),
                patch.object(main_module.web, "run_app", side_effect=capture_run_app),
            ):
                main_module.run(options_file, root / "data")

            app = captured["app"]
            error = app["startup_error"]
            self.assertEqual(error.code, "mqtt_credentials_invalid")
            self.assertNotIn(supervisor_token, str(error))
            self.assertNotIn(mqtt_password, json.dumps(error.redacted()))
            self.assertNotIn("publisher", app)

    async def test_publisher_waits_for_accepted_connack_before_ready(self):
        client = FakePahoClient(connack=0)
        publisher = PahoPublisher(
            "core-mosquitto", 1883, "awtrix", "mqtt-password", client_factory=lambda: client, connect_timeout=0.01
        )

        await publisher.start()

        self.assertIs(publisher._client, client)
        self.assertEqual(client.username_password, ("awtrix", "mqtt-password"))
        self.assertLess(client.calls.index("username_pw_set"), client.calls.index("connect"))
        self.assertIsNone(publisher._username)
        self.assertIsNone(publisher._password)

    async def test_unready_connection_failures_clean_up_without_ready_publisher(self):
        for connack, connect_result, message in ((5, 0, "rejected"), (None, 0, "timed out"), (None, 1, "connect failed")):
            with self.subTest(connack=connack, connect_result=connect_result):
                client = FakePahoClient(connack=connack, connect_result=connect_result)
                publisher = PahoPublisher(
                    "core-mosquitto", 1883, "awtrix", "mqtt-password", client_factory=lambda: client, connect_timeout=0.01
                )

                with self.assertRaisesRegex(RuntimeError, message):
                    await publisher.start()

                self.assertIsNone(publisher._client)
                self.assertEqual(client.calls[-2:], ["loop_stop", "disconnect"])
                self.assertIsNone(publisher._username)
                self.assertIsNone(publisher._password)

    async def test_stale_mqtt_session_refreshes_supervisor_credentials_before_retry(self):
        first = FakePahoClient(connack=0)
        refreshed = FakePahoClient(connack=0)
        clients = iter((first, refreshed))
        provider_calls = []

        def provide_credentials():
            provider_calls.append(True)
            return ("refreshed-broker", 1884, "refreshed-user", "refreshed-password")

        publisher = PahoPublisher(
            "initial-broker",
            1883,
            "initial-user",
            "initial-password",
            credentials_provider=provide_credentials,
            client_factory=lambda: next(clients),
            connect_timeout=0.01,
        )
        await publisher.start()
        first.connected = False

        await publisher.publish("bedroom-clock/custom/awtrix_addon", "payload")

        self.assertEqual(provider_calls, [True])
        self.assertEqual(first.calls[-2:], ["loop_stop", "disconnect"])
        self.assertEqual(refreshed.address, ("refreshed-broker", 1884, 30))
        self.assertEqual(refreshed.username_password, ("refreshed-user", "refreshed-password"))
        self.assertEqual(refreshed.published, [("bedroom-clock/custom/awtrix_addon", "payload", 0, False)])
        self.assertIsNone(publisher._username)
        self.assertIsNone(publisher._password)

    async def test_startup_recovers_when_supervisor_mqtt_is_not_ready_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            settings = settings_from_options(base_options(data_dir))
            app = make_app(
                settings,
                AuthManager(data_dir, settings.auth_token),
                None,
                data_dir=data_dir,
                startup_error=StartupConfigError("mqtt_credentials_unavailable", "MQTT credentials are unavailable"),
            )
            attempts = [
                StartupConfigError("mqtt_credentials_unavailable", "MQTT credentials are unavailable"),
                ("core-mosquitto", 1883, "user", "password"),
            ]
            sleeps = []

            async def fake_sleep(seconds):
                sleeps.append(seconds)

            with (
                patch.object(main_module, "load_mqtt_credentials", side_effect=attempts),
                patch.object(main_module, "PahoPublisher", FakeRecoveringPublisher),
            ):
                await main_module._recover_mqtt_runtime(app, settings, data_dir, sleep=fake_sleep)

            self.assertEqual(sleeps, [2])
            self.assertIsNone(app["startup_error"])
            self.assertIn("store", app)
            self.assertTrue(app["publisher"].started)

    async def test_invalid_supervisor_credentials_stop_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            settings = settings_from_options(base_options(data_dir))
            app = make_app(
                settings,
                AuthManager(data_dir, settings.auth_token),
                None,
                data_dir=data_dir,
                startup_error=StartupConfigError("mqtt_credentials_unavailable", "MQTT credentials are unavailable"),
            )
            with patch.object(
                main_module,
                "load_mqtt_credentials",
                side_effect=StartupConfigError("mqtt_credentials_invalid", "MQTT credentials are invalid"),
            ):
                with self.assertRaisesRegex(StartupConfigError, "invalid"):
                    await main_module._recover_mqtt_runtime(app, settings, data_dir)

            self.assertEqual(app["startup_error"].code, "mqtt_credentials_invalid")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = tempfile.TemporaryDirectory()
        self.publisher = MemoryPublisher()
        self.app = app_from_options(base_options(Path(self.tmp.name)), Path(self.data.name), self.publisher, start_tasks=False)

    async def asyncTearDown(self):
        self.tmp.cleanup()
        self.data.cleanup()

    def auth(self, token: str = "secret-token"):
        return {"Authorization": f"Bearer {token}"}

    async def test_health_public_and_api_auth_errors(self):
        health_response = await dispatch(self.app, health, "/health")
        self.assertEqual(health_response.status, 200)
        self.assertEqual(response_json(health_response), {"status": "ok"})

        missing = await dispatch(self.app, create_event, "/api/events", body={})
        self.assertEqual(missing.status, 401)
        self.assertEqual(response_json(missing), {"error": "auth_required", "message": "Bearer token is required", "details": {}})

        bad = await dispatch(self.app, create_event, "/api/events", headers=self.auth("bad"), body={})
        self.assertEqual(bad.status, 403)
        self.assertEqual(response_json(bad), {"error": "auth_failed", "message": "Bearer token is invalid", "details": {}})

    async def test_api_routing_errors_use_json_schema(self):
        self.app.freeze()

        missing_request = make_mocked_request("GET", "/api/missing", headers=self.auth(), app=self.app)
        missing = await self.app._handle(missing_request)
        self.assertEqual(missing.status, 404)
        self.assertEqual(response_json(missing), {"error": "not_found", "message": "API route not found", "details": {}})

        wrong_method_request = make_mocked_request("GET", "/api/events", headers=self.auth(), app=self.app)
        wrong_method = await self.app._handle(wrong_method_request)
        self.assertEqual(wrong_method.status, 405)
        self.assertEqual(
            response_json(wrong_method),
            {
                "error": "method_not_allowed",
                "message": "Method is not allowed for this API route",
                "details": {"allowed_methods": ["POST"]},
            },
        )

    async def test_create_cancel_and_allowed_topics(self):
        melody = "chime:d=4,o=5,b=120:c,e,g,c6,g,e,c,p,c,p"
        response = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "evt-1", "clock_prefixes": ["clock/kitchen"], "duration_seconds": 10, "rtttl": melody},
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response_json(response), {"event_id": "evt-1", "clock_prefixes": ["clock/kitchen"]})
        topics = [topic for topic, _payload in self.publisher.published]
        self.assertIn("clock/kitchen/custom/awtrix_addon", topics)
        self.assertIn("clock/kitchen/rtttl", topics)
        self.assertIn(("clock/kitchen/rtttl", melody), self.publisher.published)
        self.assertNotIn("clock/kitchen/settings", topics)
        self.assertTrue(all("/brightness" not in topic and "/palette" not in topic for topic in topics))

        cancel = await dispatch(
            self.app,
            cancel_current,
            "/api/events/current",
            headers=self.auth(),
            body={"clock_prefixes": ["clock/kitchen"]},
        )
        self.assertEqual(cancel.status, 200)
        self.assertEqual(response_json(cancel), {"restored": ["clock/kitchen"]})
        self.assertIn(("clock/kitchen/custom/awtrix_addon", ""), self.publisher.published)

    async def test_library_melody_resolves_default_and_personal(self):
        personal_dir = Path(self.data.name) / "library" / "melodies" / "Personal"
        personal_dir.mkdir(parents=True)
        personal = "Personal_chime:d=8,o=6,b=180:c,e,g"
        personal_dir.joinpath("My_chime.rtttl").write_text(personal, encoding="utf-8")

        default_response = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "default", "clock_prefixes": ["clock/kitchen"], "duration_seconds": 10, "melody": "Default/Arkanoid"},
        )
        personal_response = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "personal", "clock_prefixes": ["clock/office"], "duration_seconds": 10, "melody": "Personal/My_chime"},
        )

        self.assertEqual(default_response.status, 201)
        self.assertEqual(personal_response.status, 201)
        self.assertIn(
            ("clock/kitchen/rtttl", "Arkanoid:d=4,o=5,b=140:8g6,16p,16g.6,2a#6,32p,8a6,8g6,8f6,8a6,2g6"),
            self.publisher.published,
        )
        self.assertIn(("clock/office/rtttl", personal), self.publisher.published)

    async def test_personal_melody_persists_for_new_app_instance(self):
        personal_dir = Path(self.data.name) / "library" / "melodies" / "Personal"
        personal_dir.mkdir(parents=True)
        melody = "Persistent:d=4,o=5,b=120:c,e,g"
        personal_dir.joinpath("Persistent.rtttl").write_text(melody, encoding="utf-8")
        new_publisher = MemoryPublisher()
        new_app = app_from_options(base_options(Path(self.tmp.name)), Path(self.data.name), new_publisher, start_tasks=False)

        response = await dispatch(
            new_app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"clock_prefixes": ["clock/kitchen"], "duration_seconds": 10, "melody": "Personal/Persistent"},
        )

        self.assertEqual(response.status, 201)
        self.assertIn(("clock/kitchen/rtttl", melody), new_publisher.published)

    async def test_invalid_melody_and_rtttl_requests_leave_no_state_and_allow_same_id_retry(self):
        personal_dir = Path(self.data.name) / "library" / "melodies" / "Personal"
        personal_dir.mkdir(parents=True)
        personal_dir.joinpath("Not_utf8.rtttl").write_bytes(b"\xff")
        personal_dir.joinpath("Empty.rtttl").write_text(" \n", encoding="utf-8")
        personal_dir.joinpath("Malformed.rtttl").write_text("not an RTTTL expression", encoding="utf-8")
        personal_dir.joinpath("Bad_default.rtttl").write_text("Bad:d=3,o=5,b=120:c", encoding="utf-8")
        personal_dir.joinpath("Bad_note.rtttl").write_text("Bad:d=4,o=5,b=120:h", encoding="utf-8")
        cases = [
            ({"melody": ["Default/Arkanoid"]}, 400, {"error": "invalid_melody", "message": "melody must be a string", "details": {}}),
            ({"melody": "default/Arkanoid"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Default/Arkanoid/extra"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Default/.hidden"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Default/Missing"}, 404, {"error": "melody_not_found", "message": "Melody was not found", "details": {"melody": "Default/Missing"}}),
            ({"melody": "Personal/Not_utf8"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Personal/Empty"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Personal/Malformed"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Personal/Bad_default"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Personal/Bad_note"}, 400, {"error": "invalid_melody", "message": "melody must be a valid library reference", "details": {}}),
            ({"melody": "Default/Arkanoid", "rtttl": "Direct:d=4,o=5,b=120:c"}, 400, {"error": "invalid_melody", "message": "melody and rtttl are mutually exclusive", "details": {}}),
            ({"rtttl": ["c"]}, 400, {"error": "invalid_rtttl", "message": "rtttl must be a string", "details": {}}),
            ({"rtttl": "not an RTTTL expression"}, 400, {"error": "invalid_rtttl", "message": "rtttl must be a valid RTTTL expression", "details": {}}),
            ({"rtttl": "Bad:d=3,o=5,b=120:c"}, 400, {"error": "invalid_rtttl", "message": "rtttl must be a valid RTTTL expression", "details": {}}),
            ({"rtttl": "Bad:d=4,o=3,b=120:c"}, 400, {"error": "invalid_rtttl", "message": "rtttl must be a valid RTTTL expression", "details": {}}),
            ({"rtttl": "Bad:d=4,o=5,b=10:c"}, 400, {"error": "invalid_rtttl", "message": "rtttl must be a valid RTTTL expression", "details": {}}),
            ({"rtttl": "Bad:d=4,o=5,b=120:h"}, 400, {"error": "invalid_rtttl", "message": "rtttl must be a valid RTTTL expression", "details": {}}),
        ]
        for extra, status, expected in cases:
            with self.subTest(extra=extra):
                publisher = MemoryPublisher()
                app = app_from_options(base_options(Path(self.tmp.name)), Path(self.data.name), publisher, start_tasks=False)
                event_id = "retryable"
                response = await dispatch(
                    app,
                    create_event,
                    "/api/events",
                    headers=self.auth(),
                    body={"event_id": event_id, "clock_prefixes": ["clock/kitchen"], "duration_seconds": 10, **extra},
                )
                self.assertEqual(response.status, status)
                self.assertEqual(response_json(response), expected)
                self.assertEqual(publisher.published, [])
                self.assertEqual(app["store"].snapshot(), {})
                self.assertEqual(app["store"]._events, {})

                retry = await dispatch(
                    app,
                    create_event,
                    "/api/events",
                    headers=self.auth(),
                    body={
                        "event_id": event_id,
                        "clock_prefixes": ["clock/kitchen"],
                        "duration_seconds": 10,
                        "rtttl": "Retry:d=4,o=5,b=120:c",
                    },
                )
                self.assertEqual(retry.status, 201)
                self.assertEqual(response_json(retry), {"event_id": event_id, "clock_prefixes": ["clock/kitchen"]})
                self.assertEqual(
                    app["store"].snapshot(), {"clock/kitchen": {"event_id": event_id, "generation": 1}}
                )
                await app["store"].cancel_event(event_id)

    async def test_create_event_accepts_base64_asset_without_file(self):
        image = Image.new("RGB", (2, 2), (255, 0, 0))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        response = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={
                "event_id": "inline-asset",
                "clock_prefixes": ["clock/kitchen"],
                "duration_seconds": 10,
                "asset_base64": encoded,
            },
        )

        self.assertEqual(response.status, 201)
        custom_payload = next(
            payload
            for topic, payload in self.publisher.published
            if topic == "clock/kitchen/custom/awtrix_addon"
        )
        payload = json.loads(custom_payload)
        bitmap = payload["draw"][0]["db"][4]
        for y in range(8):
            self.assertEqual(bitmap[y * 32 : y * 32 + 10], [0xFF0000] * 10)

    async def test_base64_asset_validation(self):
        image = Image.new("RGB", (2, 2), (0, 255, 0))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

        ok = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "data-url", "clock_prefixes": ["clock/kitchen"], "duration_seconds": 10, "asset_base64": data_url},
        )
        self.assertEqual(ok.status, 201)

        conflict = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={
                "clock_prefixes": ["clock/kitchen"],
                "duration_seconds": 10,
                "asset": "icon.png",
                "asset_base64": data_url,
            },
        )
        self.assertEqual(conflict.status, 400)
        self.assertEqual(response_json(conflict)["message"], "asset and asset_base64 are mutually exclusive")

        invalid = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"clock_prefixes": ["clock/kitchen"], "duration_seconds": 10, "asset_base64": "not base64"},
        )
        self.assertEqual(invalid.status, 400)
        self.assertEqual(response_json(invalid)["message"], "asset could not be loaded")

    async def test_delete_event_by_id(self):
        response = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "evt-delete", "clock_prefixes": ["clock/office"], "duration_seconds": 10},
        )
        self.assertEqual(response.status, 201)
        delete = await dispatch(
            self.app,
            cancel_event_handler,
            "/api/events/evt-delete",
            headers=self.auth(),
            body={},
            match_info={"event_id": "evt-delete"},
        )
        self.assertEqual(delete.status, 200)
        self.assertEqual(response_json(delete), {"restored": ["clock/office"]})

    async def test_invalid_request_prefixes_publish_nothing(self):
        cases = [
            [],
            ["clock/kitchen", "clock/kitchen"],
            ["clock/missing"],
            ["/clock"],
            ["clock/#"],
        ]
        for case in cases:
            before = list(self.publisher.published)
            response = await dispatch(
                self.app,
                create_event,
                "/api/events",
                headers=self.auth(),
                body={"clock_prefixes": case, "duration_seconds": 10},
            )
            self.assertEqual(response.status, 400)
            body = response_json(response)
            self.assertEqual(body["error"], "invalid_clock_prefixes")
            self.assertIn("invalid", body["details"])
            self.assertEqual(body["details"]["allowed"], ["clock/kitchen", "clock/office"])
            self.assertEqual(self.publisher.published, before)

    async def test_unhashable_request_prefix_publishes_nothing(self):
        response = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"clock_prefixes": [{}], "duration_seconds": 10},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(
            response_json(response),
            {
                "error": "invalid_clock_prefixes",
                "message": "clock_prefixes must be unique, valid, and allowlisted",
                "details": {"invalid": ["{}"], "allowed": ["clock/kitchen", "clock/office"]},
            },
        )
        self.assertEqual(self.publisher.published, [])

    async def test_regenerate_managed_by_options(self):
        response = await dispatch(self.app, regenerate_auth, "/api/auth/regenerate", headers=self.auth(), body={})
        self.assertEqual(response.status, 409)
        self.assertEqual(response_json(response), {"error": "managed_by_options", "message": "Token is managed by App options", "details": {}})

    async def test_startup_config_failed_is_redacted_and_publishes_nothing(self):
        publisher = MemoryPublisher()
        app = app_from_options(
            {
                "app_name": "bad/name",
                "clock_prefixes": ["clock/kitchen"],
                "assets_dir": self.tmp.name,
                "auth_token": "secret-token",
            },
            Path(self.data.name),
            publisher,
            start_tasks=False,
        )
        health_response = await dispatch(app, health, "/health")
        self.assertEqual(response_json(health_response), {"status": "config_failed"})
        response = await dispatch(app, create_event, "/api/events", headers=self.auth(), body={})
        self.assertEqual(response.status, 503)
        body = response_json(response)
        self.assertEqual(body["error"], "startup_config_failed")
        self.assertEqual(body["details"]["config_error"]["code"], "invalid_app_name")
        self.assertNotIn("secret-token", json.dumps(body))
        self.assertEqual(publisher.published, [])

    async def test_main_run_startup_failure_uses_configured_auth_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options_file = root / "options.json"
            data_dir = root / "data"
            options_file.write_text(
                json.dumps(
                    {
                        "app_name": "bad/name",
                        "clock_prefixes": ["clock/kitchen"],
                        "assets_dir": self.tmp.name,
                        "auth_token": "configured-token",
                    }
                ),
                encoding="utf-8",
            )
            captured = {}

            def capture_run_app(app, **kwargs):
                captured["app"] = app
                captured["kwargs"] = kwargs

            with patch.object(main_module.web, "run_app", side_effect=capture_run_app):
                main_module.run(options_file, data_dir)

            self.assertEqual(captured["kwargs"], {"host": "0.0.0.0", "port": 8099})
            app = captured["app"]

            bad_auth = await dispatch(app, create_event, "/api/events", headers=self.auth("bad"), body={})
            self.assertEqual(bad_auth.status, 403)
            self.assertEqual(response_json(bad_auth)["error"], "auth_failed")

            good_auth = await dispatch(
                app,
                create_event,
                "/api/events",
                headers={"Authorization": "Bearer configured-token"},
                body={},
            )
            self.assertEqual(good_auth.status, 503)
            body = response_json(good_auth)
            self.assertEqual(body["error"], "startup_config_failed")
            self.assertEqual(body["details"]["config_error"]["code"], "invalid_app_name")
            self.assertFalse((data_dir / "auth.json").exists())

    def test_generated_auth_startup_log_is_short_and_copyable(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            settings = settings_from_options(
                {
                    "app_name": "awtrix_addon",
                    "clock_prefixes": ["clock/kitchen"],
                    "assets_dir": self.tmp.name,
                    "auth_token": "",
                }
            )
            lines = main_module.startup_log_lines(settings, AuthManager(data_dir))

            self.assertEqual(json.loads(lines[0]), {"status": "started", "port": 8099, "auth": "generated"})
            self.assertEqual(lines[1], "AWTRIX App generated auth token.")
            token = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))["token"]
            self.assertIsInstance(token, str)
            self.assertEqual(lines[2], "Use in HA secrets.yaml: awtrix_addon_authorization: Bearer " + token)
            self.assertEqual(lines[3], "Token is stored in /data/auth.json")
            self.assertNotIn("rest_command", "\n".join(lines))

    def test_option_auth_startup_log_does_not_echo_token(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_from_options(
                {
                    "app_name": "awtrix_addon",
                    "clock_prefixes": ["clock/kitchen"],
                    "assets_dir": self.tmp.name,
                    "auth_token": "configured-token",
                }
            )
            lines = main_module.startup_log_lines(settings, AuthManager(Path(directory), settings.auth_token))

            self.assertEqual(lines, ['{"status": "started", "port": 8099, "auth": "option"}'])

    async def test_api_cancel_allows_reusing_stable_event_id(self):
        first = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "doorbell", "clock_prefixes": ["clock/kitchen"], "duration_seconds": 10},
        )
        self.assertEqual(first.status, 201)

        cancel = await dispatch(
            self.app,
            cancel_event_handler,
            "/api/events/doorbell",
            headers=self.auth(),
            body={},
            match_info={"event_id": "doorbell"},
        )
        self.assertEqual(cancel.status, 200)
        self.assertEqual(response_json(cancel), {"restored": ["clock/kitchen"]})

        second = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "doorbell", "clock_prefixes": ["clock/office"], "duration_seconds": 10},
        )
        self.assertEqual(second.status, 201)
        self.assertEqual(response_json(second), {"event_id": "doorbell", "clock_prefixes": ["clock/office"]})


class LifecycleRendererTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings_from_options(
            {
                "app_name": "awtrix_addon",
                "clock_prefixes": ["clock/a", "clock/b"],
                "assets_dir": self.tmp.name,
            }
        )
        self.publisher = MemoryPublisher()
        self.current = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def now(self):
        return self.current

    def spec(self, event_id, prefixes):
        return EventSpec(event_id, tuple(prefixes), 10, AssetAnimation((blank_asset(),)))

    async def test_latest_wins_partial_cancel_and_stale_restore_suppression(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        await store.create(self.spec("old", ["clock/a", "clock/b"]))
        await store.create(self.spec("new", ["clock/a"]))

        restored_old = await store.cancel_event("old")
        self.assertEqual(restored_old, ["clock/b"])
        self.assertIn(("clock/b/custom/awtrix_addon", ""), self.publisher.published)
        self.assertNotIn(("clock/a/custom/awtrix_addon", ""), self.publisher.published)

        restored_new = await store.cancel_current(("clock/a",))
        self.assertEqual(restored_new, ["clock/a"])
        self.assertEqual(self.publisher.published.count(("clock/a/custom/awtrix_addon", "")), 1)

    async def test_active_event_id_replaces_its_existing_overlay(self):
        async def no_sleep(_seconds):
            return None

        store = EventStore(self.settings, self.publisher, now=self.now, sleep=no_sleep, start_tasks=False)
        await store.create(self.spec("same", ["clock/a", "clock/b"]))
        previous = store._events["same"]
        self.publisher.published.clear()

        replaced = await store.create(self.spec("same", ["clock/b"]))

        self.assertEqual(replaced, "same")
        self.assertEqual(store.snapshot(), {"clock/b": {"event_id": "same", "generation": 3}})
        self.assertIn(("clock/a/custom/awtrix_addon", ""), self.publisher.published)
        self.assertTrue(any(topic == "clock/b/custom/awtrix_addon" for topic, _payload in self.publisher.published))
        self.publisher.published.clear()

        await store._run_event(previous)

        self.assertEqual(self.publisher.published, [])

    async def test_new_event_switches_to_its_custom_app_once_then_cleanup_only_clears_it(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)

        await store.create(self.spec("evt", ["clock/a"]))

        self.assertEqual(self.publisher.published[0][0], "clock/a/custom/awtrix_addon")
        self.assertEqual(
            self.publisher.published[1],
            ("clock/a/switch", '{"name":"awtrix_addon","fast":true}'),
        )
        await store.render_once("evt")
        self.assertEqual(
            [item for item in self.publisher.published if item[0] == "clock/a/switch"],
            [("clock/a/switch", '{"name":"awtrix_addon","fast":true}')],
        )

        await store.cancel_event("evt")

        self.assertEqual(self.publisher.published[-1], ("clock/a/custom/awtrix_addon", ""))
        self.assertEqual(
            [item for item in self.publisher.published if item[0] == "clock/a/switch"],
            [("clock/a/switch", '{"name":"awtrix_addon","fast":true}')],
        )

    async def test_expiry_allows_reusing_stable_event_id(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        await store.create(self.spec("doorbell", ["clock/a"]))
        self.current = self.current + timedelta(seconds=11)

        await store.expire_due()

        self.assertEqual(store.snapshot(), {})
        reused = await store.create(self.spec("doorbell", ["clock/b"]))
        self.assertEqual(reused, "doorbell")
        self.assertEqual(store.snapshot(), {"clock/b": {"event_id": "doorbell", "generation": 1}})

    async def test_full_supersede_allows_reusing_stable_event_id(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        await store.create(self.spec("doorbell", ["clock/a", "clock/b"]))
        await store.create(self.spec("other", ["clock/a", "clock/b"]))

        reused = await store.create(self.spec("doorbell", ["clock/a"]))

        self.assertEqual(reused, "doorbell")
        self.assertEqual(store.snapshot()["clock/a"]["event_id"], "doorbell")

    async def _assert_create_failure_rolls_back_and_retries(self, spec, fail_topic):
        publisher = FailOncePublisher()
        store = EventStore(self.settings, publisher, now=self.now, start_tasks=False)
        await store.create(self.spec("old", ["clock/a", "clock/b"]))
        snapshot = store.snapshot()
        generations = dict(store._generations)
        old_states = dict(store._events["old"].states)
        old_bindings = dict(store._events["old"].bindings)
        old_frame_index = store._events["old"].frame_index
        publisher.fail_topic = fail_topic

        with self.assertRaisesRegex(RuntimeError, "planned publish failure"):
            await store.create(spec)

        self.assertEqual(store.snapshot(), snapshot)
        self.assertEqual(dict(store._generations), generations)
        self.assertEqual(store._events["old"].states, old_states)
        self.assertEqual(store._events["old"].bindings, old_bindings)
        self.assertEqual(store._events["old"].frame_index, old_frame_index)
        self.assertNotIn(spec.event_id, store._events)
        await store.render_once("old")
        self.assertEqual(await store.create(spec), spec.event_id)
        self.assertEqual(store.snapshot(), {"clock/a": {"event_id": spec.event_id, "generation": 2}, "clock/b": {"event_id": spec.event_id, "generation": 2}})
        self.assertEqual(await store.cancel_event("old"), [])

    async def test_second_frame_failure_rolls_back_multi_clock_supersede_and_allows_retry(self):
        spec = self.spec("new-frame", ["clock/a", "clock/b"])
        await self._assert_create_failure_rolls_back_and_retries(spec, "clock/b/custom/awtrix_addon")

    async def test_second_switch_failure_rolls_back_multi_clock_supersede_and_allows_retry(self):
        spec = self.spec("new-switch", ["clock/a", "clock/b"])
        await self._assert_create_failure_rolls_back_and_retries(spec, "clock/b/switch")

    async def test_second_rtttl_failure_rolls_back_multi_clock_supersede_and_allows_retry(self):
        spec = EventSpec(
            "new-rtttl",
            ("clock/a", "clock/b"),
            10,
            AssetAnimation((blank_asset(),)),
            rtttl="beep:d=4,o=5,b=100:c",
        )
        await self._assert_create_failure_rolls_back_and_retries(spec, "clock/b/rtttl")

    async def test_expiry_boundary_and_shutdown_snapshot(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        await store.create(self.spec("old", ["clock/a"]))
        await store.create(self.spec("new", ["clock/a"]))
        self.current = self.current + timedelta(seconds=11)
        await store.cancel_event("old", final_state="expired")
        self.assertNotIn(("clock/a/custom/awtrix_addon", ""), self.publisher.published)
        await store.shutdown()
        self.assertEqual(self.publisher.published.count(("clock/a/custom/awtrix_addon", "")), 1)

    async def test_rtttl_is_published_once_and_not_replayed_when_rendered(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        melody = "chime:d=4,o=5,b=120:c,e,g,c6,g,e,c,p,c,p"
        await store.create(EventSpec("evt", ("clock/a",), 10, AssetAnimation((blank_asset(),)), rtttl=melody))
        self.current = self.current + timedelta(seconds=1)
        await store.render_once("evt")
        rtttl_messages = [item for item in self.publisher.published if item == ("clock/a/rtttl", melody)]
        self.assertEqual(len(rtttl_messages), 1)
        custom_payloads = [payload for topic, payload in self.publisher.published if topic == "clock/a/custom/awtrix_addon"]
        self.assertEqual(len(custom_payloads), 2)
        self.assertNotEqual(custom_payloads[0], custom_payloads[1])


    async def test_render_loop_uses_one_second_cadence(self):
        intervals = []

        async def sleep(seconds):
            intervals.append(seconds)
            raise asyncio.CancelledError

        store = EventStore(self.settings, self.publisher, now=self.now, sleep=sleep, start_tasks=True)
        await store.create(self.spec("evt", ["clock/a"]))
        await asyncio.sleep(0)
        self.assertEqual(intervals, [1])

    def test_asset_normalization_and_colon_alternates(self):
        png_path = Path(self.tmp.name) / "icon.png"
        Image.new("RGB", (2, 2), (255, 0, 0)).save(png_path)
        asset = load_asset(Path(self.tmp.name), "icon.png")
        self.assertEqual(asset.frames[0].size, (10, 8))
        buffer = BytesIO()
        Image.new("RGB", (20, 16), (0, 255, 0)).save(buffer, format="PNG")
        inline_asset = load_asset_bytes(buffer.getvalue())
        self.assertEqual(inline_asset.frames[0].size, (10, 8))

        even = render_frame(blank_asset(), datetime(2026, 6, 28, 12, 34, 2, tzinfo=timezone.utc))
        odd = render_frame(blank_asset(), datetime(2026, 6, 28, 12, 34, 3, tzinfo=timezone.utc))
        self.assertNotEqual(even.tobytes(), odd.tobytes())

    def test_gif_asset_metadata_comes_from_load_asset(self):
        no_loop_path = Path(self.tmp.name) / "no-loop.gif"
        Image.new("RGB", (2, 2), (255, 0, 0)).save(
            no_loop_path,
            save_all=True,
            append_images=[Image.new("RGB", (2, 2), (0, 255, 0))],
            duration=100,
        )

        with Image.open(no_loop_path) as image:
            self.assertNotIn("loop", image.info)
        with Image.open(FIXTURES / "finite.gif") as image:
            self.assertEqual(image.info["loop"], 1)
        with Image.open(FIXTURES / "loop.gif") as image:
            self.assertEqual(image.info["loop"], 0)

        no_loop = load_asset(Path(self.tmp.name), "no-loop.gif")
        finite = load_asset(FIXTURES, "finite.gif")
        looping = load_asset(FIXTURES, "loop.gif")

        self.assertFalse(no_loop.loop)
        self.assertFalse(finite.loop)
        self.assertTrue(looping.loop)
        self.assertEqual([frame.size for frame in no_loop.frames], [(10, 8), (10, 8)])
        self.assertEqual([frame.size for frame in finite.frames], [(10, 8), (10, 8)])
        self.assertEqual([frame.size for frame in looping.frames], [(10, 8), (10, 8)])
        self.assertNotEqual(no_loop.frames[0].tobytes(), no_loop.frames[1].tobytes())
        self.assertNotEqual(finite.frames[0].tobytes(), finite.frames[1].tobytes())
        self.assertNotEqual(looping.frames[0].tobytes(), looping.frames[1].tobytes())
        self.assertEqual(no_loop.frame_at(len(no_loop.frames)).tobytes(), no_loop.frames[-1].tobytes())
        self.assertEqual(finite.frame_at(2).tobytes(), finite.frames[1].tobytes())
        self.assertEqual(looping.frame_at(2).tobytes(), looping.frames[0].tobytes())

    def test_clock_render_golden_bitmap(self):
        frame = render_frame(blank_asset(), datetime(2026, 6, 28, 12, 34, 2, tzinfo=timezone.utc))

        self.assertEqual(
            frame_bitmap(frame),
            (
                "................................",
                ".............#..###...###.#.#...",
                "............##....#.#...#.#.#...",
                ".............#..###...###.###...",
                ".............#..#...#...#...#...",
                "............###.###...###...#...",
                "................................",
                "................................",
            ),
        )


class MetadataTests(unittest.TestCase):
    def test_displayed_terminology_uses_apps(self):
        legacy_term = re.compile(r"\badd-" + r"ons?\b", re.IGNORECASE)
        text_paths = [
            path
            for path in repository_files()
            if path.suffix in {".md", ".py", ".toml", ".yaml", ".yml"}
        ]
        matches = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in text_paths
            if legacy_term.search(path.read_text(encoding="utf-8", errors="ignore"))
        ]

        self.assertEqual(matches, [])

    def test_repository_contains_no_local_artifacts(self):
        forbidden = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in repository_files()
            if is_forbidden_artifact(path) and not is_ignored_untracked_file(path)
        )

        self.assertEqual(forbidden, [])

    def test_home_assistant_repository_layout(self):
        import yaml

        repository = yaml.safe_load(REPO_ROOT.joinpath("repository.yaml").read_text())
        dockerfile = ROOT.joinpath("Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(repository["url"], "https://github.com/alex-ander-is/awtrix-addon")
        self.assertEqual(repository["maintainer"], "alex-ander-is")
        self.assertEqual(repository["name"], "AWTRIX Apps")
        self.assertTrue(ROOT.joinpath("config.yaml").is_file())
        self.assertTrue(ROOT.joinpath("Dockerfile").is_file())
        self.assertTrue(ROOT.joinpath("CHANGELOG.md").is_file())
        run_script = ROOT.joinpath("run.sh").read_text(encoding="utf-8")
        self.assertIn("COPY run.sh /run.sh", dockerfile)
        self.assertIn('CMD ["/run.sh"]', dockerfile)
        self.assertTrue(run_script.startswith("#!/usr/bin/with-contenv bashio\n"))
        self.assertIn("exec awtrix-addon", run_script)
        self.assertTrue(ROOT.joinpath("README.md").is_file())
        self.assertTrue(ROOT.joinpath("icon.png").is_file())
        self.assertFalse(REPO_ROOT.joinpath("config.yaml").exists())
        self.assertFalse(REPO_ROOT.joinpath("Dockerfile").exists())
        self.assertIn("ARG BUILD_ARCH=amd64", dockerfile)
        self.assertIn("ghcr.io/home-assistant/${BUILD_ARCH}-base-python", dockerfile)
        self.assertNotIn("amd64-base", dockerfile)

    def test_artifact_matcher_uses_narrow_basenames(self):
        blocked = [
            ".DS_Store",
            ".env",
            ".env.local",
            "auth.json",
            "options.json",
            "service.log",
            "screenshot.png",
            "screenshot-1.jpg",
            "screenshot_final.jpeg",
            "canvas.png",
            "canvas-1.jpg",
            "canvas_final.jpeg",
            "canvas-dump.png",
            "canvas-dump-final.jpeg",
        ]
        allowed = [
            "src/awtrix_addon/auth.py",
            "src/awtrix_addon/auth_helpers.py",
            "tests/test_auth_contract.py",
            "authentication.json",
            "auth.json.example",
            "screenshot.gif",
            "my-screenshot.png",
            "canvas.txt",
            "canvas-dump.txt",
            "tests/fixtures/finite.gif",
        ]

        for path in blocked:
            with self.subTest(path=path):
                self.assertTrue(is_forbidden_artifact(Path(path)))

        for path in allowed:
            with self.subTest(path=path):
                self.assertFalse(is_forbidden_artifact(Path(path)))

    def test_readme_yaml_uses_full_scalar_secret_references(self):
        import yaml

        class SecretLoader(yaml.SafeLoader):
            pass

        SecretLoader.add_constructor("!secret", lambda loader, node: loader.construct_scalar(node))

        secret_blocks = [block for block in readme_yaml_blocks() if "!secret" in block]
        self.assertTrue(secret_blocks)
        for block in secret_blocks:
            assert_full_scalar_secrets_only(self, block)
            yaml.load(block, Loader=SecretLoader)

    def test_readme_rejects_quoted_or_embedded_secret_references(self):
        bad_blocks = [
            'headers:\n  Authorization: "Bearer !secret awtrix_addon_token"\n',
            'rest_command:\n  awtrix_event:\n    url: "http://!secret awtrix_addon_host:8099/api/events"\n',
            'headers:\n  Authorization: "!secret awtrix_addon_authorization"\n',
        ]
        for block in bad_blocks:
            with self.assertRaises(AssertionError):
                assert_full_scalar_secrets_only(self, block)

        assert_full_scalar_secrets_only(
            self,
            "headers:\n  Authorization: !secret awtrix_addon_authorization\nurl: !secret awtrix_addon_events_url\n",
        )

    def test_dockerignore_excludes_audit_and_git(self):
        lines = set(ROOT.joinpath(".dockerignore").read_text().splitlines())
        self.assertIn(".codex-audit", lines)
        self.assertIn(".git", lines)

    def test_smoke_commands_are_documented_from_their_roots(self):
        repository_readme = REPO_ROOT.joinpath("README.md").read_text(encoding="utf-8")
        addon_readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")
        self.assertIn("python3 awtrix-addon/scripts/smoke.py", repository_readme)
        self.assertIn("python3 scripts/smoke.py", addon_readme)
        self.assertNotIn("pytest tests", repository_readme)
        self.assertNotIn("pytest tests", addon_readme)

    def test_readmes_use_app_dns_service_url_from_home_assistant(self):
        for path in (REPO_ROOT / "README.md", ROOT / "README.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("http://35664e22-awtrix-addon:8099", text)
            self.assertNotIn("http://127.0.0.1:8099", text)
            self.assertNotIn("homeassistant.local:8099", text)

    def test_readmes_document_event_id_replacement(self):
        for path in (REPO_ROOT / "README.md", ROOT / "README.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("event_id` is a replace key", text)
            self.assertNotIn("duplicate_event_id", text)

    def test_readmes_document_immediate_custom_app_switch_and_cleanup(self):
        for path in (REPO_ROOT / "README.md", ROOT / "README.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("`<prefix>/switch`", text)
            self.assertIn('`{"name":"<app_name>","fast":true}`', text)
            self.assertIn("forced `Clock` command", text)

    def test_app_info_readme_documents_asset_resolution(self):
        readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")
        self.assertIn("10x8", readme)
        self.assertIn("32x8", readme)
        self.assertIn("resized to `10x8`", readme)
        self.assertIn("does not crop, pad, or preserve aspect ratio", readme)
        self.assertIn("asset_base64", readme)
        self.assertIn("Use either `asset` or `asset_base64`, not both.", readme)

    def test_melody_library_is_packaged_and_documented(self):
        default_melody = ROOT / "src" / "awtrix_addon" / "library" / "melodies" / "Default" / "Arkanoid.rtttl"
        self.assertTrue(default_melody.is_file())
        self.assertIn("Arkanoid:d=4,o=5,b=140", default_melody.read_text(encoding="utf-8"))
        self.assertIn("library/melodies/Default/*.rtttl", ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8"))
        for path in (REPO_ROOT / "README.md", ROOT / "README.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn('"melody"', text)
            self.assertIn("Default/Arkanoid", text)
            self.assertIn("/data/library/melodies/Personal", text)

    def test_installed_package_contains_default_arkanoid_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            project = temporary_root / "project"
            target = temporary_root / "site"
            shutil.copytree(ROOT, project, ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"))
            self.assertEqual(project.joinpath("pyproject.toml").read_bytes(), ROOT.joinpath("pyproject.toml").read_bytes())
            packaging_python = self._local_pep517_python()
            pip_root = temporary_root / "pip-root"
            subprocess.run([str(packaging_python), "-m", "ensurepip", "--root", str(pip_root)], check=True, capture_output=True)
            site_packages = next(pip_root.glob("**/site-packages"))
            environment = {**os.environ, "PYTHONPATH": str(site_packages)}
            subprocess.run(
                [
                    str(packaging_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-build-isolation",
                    "--target",
                    str(target),
                    str(project),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            verifier = """
import sys
from pathlib import Path
target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))
import awtrix_addon.melodies as melodies
origin = Path(melodies.__file__).resolve()
assert origin.is_relative_to(target), origin
melody = origin.with_name('library') / 'melodies' / 'Default' / 'Arkanoid.rtttl'
assert melody.read_text(encoding='utf-8') == 'Arkanoid:d=4,o=5,b=140:8g6,16p,16g.6,2a#6,32p,8a6,8g6,8f6,8a6,2g6\\n'
"""
            subprocess.run(
                [str(packaging_python), "-I", "-c", verifier, str(target)],
                check=True,
                capture_output=True,
                text=True,
            )

    def _local_pep517_python(self) -> Path:
        candidates = (Path(sys.executable), Path("/opt/local/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"))
        check = (
            "import sys, setuptools; "
            "assert sys.version_info >= (3, 11); "
            "assert tuple(map(int, setuptools.__version__.split('.')[:2])) >= (68, 0)"
        )
        for candidate in candidates:
            if candidate.is_file() and subprocess.run([str(candidate), "-c", check], capture_output=True).returncode == 0:
                return candidate
        self.fail("No local Python with Python >=3.11 and setuptools >=68 is available for unchanged pyproject packaging proof")

    def test_readmes_document_the_stable_melody_contract(self):
        required = (
            '"melody": "{{ melody | default(\'\') }}"',
            '"rtttl": "{{ rtttl | default(\'\') }}"',
            'melody: "Default/Arkanoid"',
            "/data/library/melodies/Personal",
            "Names are case-sensitive",
            "An empty `melody` or `rtttl` means no melody.",
            "Specify either `melody` or `rtttl`, not both.",
            "RTTTL defaults must include exactly `d`, `o`, and `b`",
            "tempo is `25` through `900`",
            '{"error":"invalid_melody","message":"melody must be a string","details":{}}',
            '{"error":"invalid_melody","message":"melody must be a valid library reference","details":{}}',
            '{"error":"melody_not_found","message":"Melody was not found","details":{"melody":"Default/Missing"}}',
            '{"error":"invalid_rtttl","message":"rtttl must be a string","details":{}}',
            '{"error":"invalid_rtttl","message":"rtttl must be a valid RTTTL expression","details":{}}',
            '{"error":"invalid_melody","message":"melody and rtttl are mutually exclusive","details":{}}',
            "creates no event and publishes no MQTT payload",
            "same `event_id` can be retried safely",
        )
        for path in (REPO_ROOT / "README.md", ROOT / "README.md"):
            text = path.read_text(encoding="utf-8")
            for value in required:
                with self.subTest(path=path, value=value):
                    self.assertIn(value, text)

    def test_config_yaml_contract(self):
        import yaml

        config = yaml.safe_load(ROOT.joinpath("config.yaml").read_text())
        self.assertEqual(config["name"], "AWTRIX App")
        self.assertFalse(config["ingress"])
        self.assertEqual(config["ports"]["8099/tcp"], 8099)
        self.assertEqual(config["arch"], ["aarch64", "amd64"])
        self.assertEqual(
            set(config["options"]),
            {"app_name", "clock_prefixes", "default_clock_prefixes", "assets_dir", "auth_token"},
        )
        self.assertEqual(config["options"]["auth_token"], "")
        self.assertEqual(config["options"]["default_clock_prefixes"], [])
        self.assertIn("auth_token", config["schema"])
        self.assertIn("default_clock_prefixes", config["schema"])
        self.assertNotIn("auth_token?", config["schema"])
        self.assertNotIn("default_clock_prefixes?", config["schema"])


if __name__ == "__main__":
    unittest.main()
