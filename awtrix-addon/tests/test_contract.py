from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awtrix_addon.api import app_from_options, cancel_current, create_event, health, regenerate_auth
from awtrix_addon.api import cancel_event as cancel_event_handler
from awtrix_addon.api import auth_middleware, startup_middleware
from awtrix_addon import main as main_module
from awtrix_addon.auth import AuthManager, TokenManagedByOptions
from awtrix_addon.errors import api_error_middleware
from awtrix_addon.lifecycle import DuplicateEventId, EventSpec, EventStore
from awtrix_addon.mqtt import MemoryPublisher
from awtrix_addon.renderer import AssetAnimation, blank_asset, load_asset, render_frame
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
        response = await dispatch(
            self.app,
            create_event,
            "/api/events",
            headers=self.auth(),
            body={"event_id": "evt-1", "clock_prefixes": ["clock/kitchen"], "duration_seconds": 10, "sound": "beep"},
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response_json(response), {"event_id": "evt-1", "clock_prefixes": ["clock/kitchen"]})
        topics = [topic for topic, _payload in self.publisher.published]
        self.assertIn("clock/kitchen/custom/awtrix_addon", topics)
        self.assertIn("clock/kitchen/sound", topics)
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
        self.assertEqual(response_json(response), {"error": "managed_by_options", "message": "Token is managed by add-on options", "details": {}})

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
            self.assertEqual(lines[1], "AWTRIX add-on generated auth token.")
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

    async def test_duplicate_event_id_is_rejected_before_publishing(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        await store.create(self.spec("same", ["clock/a"]))
        before = list(self.publisher.published)

        with self.assertRaises(DuplicateEventId):
            await store.create(self.spec("same", ["clock/b"]))

        self.assertEqual(self.publisher.published, before)
        self.assertEqual(store.snapshot(), {"clock/a": {"event_id": "same", "generation": 1}})
        restored = await store.cancel_event("same")
        self.assertEqual(restored, ["clock/a"])
        self.assertEqual(self.publisher.published.count(("clock/a/custom/awtrix_addon", "")), 1)

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

    async def test_expiry_boundary_and_shutdown_snapshot(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        await store.create(self.spec("old", ["clock/a"]))
        await store.create(self.spec("new", ["clock/a"]))
        self.current = self.current + timedelta(seconds=11)
        await store.cancel_event("old", final_state="expired")
        self.assertNotIn(("clock/a/custom/awtrix_addon", ""), self.publisher.published)
        await store.shutdown()
        self.assertEqual(self.publisher.published.count(("clock/a/custom/awtrix_addon", "")), 1)

    async def test_sound_once_and_render_fresh_now(self):
        store = EventStore(self.settings, self.publisher, now=self.now, start_tasks=False)
        await store.create(EventSpec("evt", ("clock/a",), 10, AssetAnimation((blank_asset(),)), sound="beep"))
        self.current = self.current + timedelta(seconds=1)
        await store.render_once("evt")
        sound_topics = [item for item in self.publisher.published if item == ("clock/a/sound", "beep")]
        self.assertEqual(len(sound_topics), 1)
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
    def test_repository_contains_no_local_artifacts(self):
        forbidden = sorted(path.relative_to(REPO_ROOT).as_posix() for path in repository_files() if is_forbidden_artifact(path))

        self.assertEqual(forbidden, [])

    def test_home_assistant_repository_layout(self):
        import yaml

        repository = yaml.safe_load(REPO_ROOT.joinpath("repository.yaml").read_text())
        dockerfile = ROOT.joinpath("Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(repository["url"], "https://github.com/alex-ander-is/awtrix-addon")
        self.assertEqual(repository["maintainer"], "alex-ander-is")
        self.assertTrue(ROOT.joinpath("config.yaml").is_file())
        self.assertTrue(ROOT.joinpath("Dockerfile").is_file())
        self.assertTrue(ROOT.joinpath("CHANGELOG.md").is_file())
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

    def test_smoke_command_is_documented(self):
        readme = REPO_ROOT.joinpath("README.md").read_text(encoding="utf-8")
        self.assertIn("python3 awtrix-addon/scripts/smoke.py", readme)
        self.assertNotIn("pytest tests", readme)

    def test_config_yaml_contract(self):
        import yaml

        config = yaml.safe_load(ROOT.joinpath("config.yaml").read_text())
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
