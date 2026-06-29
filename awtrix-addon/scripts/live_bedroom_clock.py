#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import fcntl
import getpass
import json
import os
import socket
import sys
import tempfile
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from awtrix_addon.live_payload_helpers import (  # noqa: E402
    LIVE_APP_NAME,
    LIVE_CLOCK_PREFIX,
    LIVE_CUSTOM_TOPIC,
    LIVE_SCREEN_URL,
    active_color_clusters,
    all_pixel_pattern_10x8,
    build_pattern_payload,
    cleanup_live_custom,
    final_pattern_match,
    native_clock_like,
    publish_live_custom,
    publish_live_switch,
    right_zone_has_clock_pixels,
    restore_state_check,
    sample_detected_canvas_grid,
    validate_live_publish,
)


OPT_IN_ENV = "AWTRIX_LIVE_BEDROOM_CLOCK"
LOCK_PATH = Path(tempfile.gettempdir()) / "awtrix_addon_bedroom_clock_live_test.lock"
PLAYWRIGHT_APP = Path.home() / "Applications" / "Playwright"
PLAYWRIGHT_RUNTIME = PLAYWRIGHT_APP / "src" / "runtime.mjs"
BEDROOM_CLOCK_IP = "192.168.0.141"


@dataclass(frozen=True)
class BrowserAuth:
    username: str
    password: str


class GuardedMqttPublisher:
    def __init__(self, host: str, port: int, username: str | None, password: str | None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._socket: socket.socket | None = None

    async def start(self) -> None:
        def connect() -> None:
            sock = socket.create_connection((self.host, self.port), timeout=10)
            sock.settimeout(10)
            client_id = f"awtrix-addon-live-test-{os.getpid()}"
            sock.sendall(mqtt_connect_packet(client_id, self.username, self.password))
            response = sock.recv(4)
            if len(response) != 4 or response[0] != 0x20 or response[3] != 0:
                sock.close()
                code = response[3] if len(response) == 4 else "missing"
                raise RuntimeError(f"MQTT connect failed with CONNACK={code}")
            self._socket = sock

        await asyncio.to_thread(connect)

    async def stop(self) -> None:
        if self._socket is None:
            return
        sock = self._socket
        self._socket = None

        def disconnect() -> None:
            try:
                try:
                    sock.sendall(b"\xe0\x00")
                except OSError:
                    pass
            finally:
                sock.close()

        await asyncio.to_thread(disconnect)

    async def publish(self, topic: str, payload: str | bytes) -> None:
        validate_live_publish(topic, payload)
        if self._socket is None:
            await self.start()
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        try:
            await asyncio.to_thread(self._socket.sendall, mqtt_publish_packet(topic, payload_bytes))
        except OSError:
            await self.stop()
            await self.start()
            if self._socket is None:
                raise RuntimeError("MQTT publisher reconnect failed")
            await asyncio.to_thread(self._socket.sendall, mqtt_publish_packet(topic, payload_bytes))


def mqtt_connect_packet(client_id: str, username: str | None, password: str | None) -> bytes:
    flags = 0x02
    fields = [mqtt_utf8(client_id)]
    if username:
        flags |= 0x80
        fields.append(mqtt_utf8(username))
        if password is not None:
            flags |= 0x40
            fields.append(mqtt_utf8(password))
    variable_header = mqtt_utf8("MQTT") + bytes([4, flags, 1, 44])
    body = variable_header + b"".join(fields)
    return bytes([0x10]) + mqtt_remaining_length(len(body)) + body


def mqtt_publish_packet(topic: str, payload: bytes) -> bytes:
    body = mqtt_utf8(topic) + payload
    return bytes([0x30]) + mqtt_remaining_length(len(body)) + body


def mqtt_utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 65535:
        raise ValueError("MQTT string is too long")
    return len(encoded).to_bytes(2, "big") + encoded


def mqtt_remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)


@contextmanager
def single_run_lock() -> Iterator[None]:
    LOCK_PATH.touch(mode=0o600, exist_ok=True)
    with LOCK_PATH.open("r+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another bedroom-clock live test is already running") from exc
        yield


def require_opt_in() -> None:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError(f"refusing live publish without {OPT_IN_ENV}=1")
    if LIVE_CLOCK_PREFIX != "bedroom-clock":
        raise RuntimeError("live clock prefix guard failed")
    if LIVE_SCREEN_URL != "http://bedroom-clock.ander.is/screen":
        raise RuntimeError("live screen URL guard failed")
    if LIVE_APP_NAME != "awtrix_addon_live_test":
        raise RuntimeError("live app name guard failed")
    validate_live_publish(LIVE_CUSTOM_TOPIC, "")


def read_mqtt_config() -> tuple[str, int, str | None, str | None]:
    host = os.environ.get("AWTRIX_LIVE_MQTT_HOST") or input("MQTT host: ").strip()
    if not host:
        raise RuntimeError("MQTT host is required")
    port = int(os.environ.get("AWTRIX_LIVE_MQTT_PORT", "1883"))
    username = os.environ.get("AWTRIX_LIVE_MQTT_USERNAME") or None
    password = os.environ.get("AWTRIX_LIVE_MQTT_PASSWORD") or None
    if username and password is None:
        password = getpass.getpass("MQTT password: ")
    return host, port, username, password


def read_browser_auth() -> BrowserAuth:
    username = os.environ.get("AWTRIX_LIVE_SCREEN_USERNAME") or input("Screen username: ").strip()
    password = os.environ.get("AWTRIX_LIVE_SCREEN_PASSWORD") or getpass.getpass("Screen password: ")
    if not username or not password:
        raise RuntimeError("screen username and password are required")
    return BrowserAuth(username=username, password=password)


async def sample_canvas_grid(auth: BrowserAuth):
    script = build_probe_script()
    env = os.environ.copy()
    env.update(
        {
            "AWTRIX_SCREEN_URL": LIVE_SCREEN_URL,
            "AWTRIX_SCREEN_USERNAME": auth.username,
            "AWTRIX_SCREEN_PASSWORD": auth.password,
            "AWTRIX_PLAYWRIGHT_RUNTIME": str(PLAYWRIGHT_RUNTIME),
            "AWTRIX_CLOCK_IP": BEDROOM_CLOCK_IP,
        }
    )
    process = await asyncio.create_subprocess_exec(
        "node",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    if process.returncode != 0:
        lines = [line.strip() for line in stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
        meaningful = [line for line in lines if not set(line) <= {"═", "╔", "╗", "╚", "╝", "║", " "}]
        detail = meaningful[-1:] or lines[-1:]
        raise RuntimeError("canvas probe failed" + (f": {detail[0]}" if detail else ""))
    data = json.loads(stdout.decode("utf-8"))
    return sample_detected_canvas_grid(data["width"], data["height"], data["rgba"])


def build_probe_script() -> str:
    return textwrap.dedent(
        r"""
        (async () => {
          const { chromium } = await import(process.env.AWTRIX_PLAYWRIGHT_RUNTIME);
          const browser = await chromium.launch({
            headless: true,
            args: [`--host-resolver-rules=MAP bedroom-clock.ander.is ${process.env.AWTRIX_CLOCK_IP}`]
          });
          try {
            const context = await browser.newContext({
              httpCredentials: {
                username: process.env.AWTRIX_SCREEN_USERNAME,
                password: process.env.AWTRIX_SCREEN_PASSWORD
              }
            });
            const page = await context.newPage();
            await page.addInitScript(() => {
              window.__awtrixCanvasRenderState = {
                drawCount: 0,
                lastDrawAt: 0
              };
              const markDraw = () => {
                const state = window.__awtrixCanvasRenderState;
                state.drawCount += 1;
                state.lastDrawAt = performance.now();
              };
              for (const name of ["fillRect", "drawImage", "putImageData", "strokeRect", "fillText", "clearRect"]) {
                const original = CanvasRenderingContext2D.prototype[name];
                if (typeof original === "function") {
                  CanvasRenderingContext2D.prototype[name] = function (...args) {
                    const result = original.apply(this, args);
                    markDraw();
                    return result;
                  };
                }
              }
            });
            await page.goto(process.env.AWTRIX_SCREEN_URL, { waitUntil: "domcontentloaded", timeout: 15000 });
            const canvas = page.locator("canvas:visible");
            await canvas.first().waitFor({ state: "visible", timeout: 10000 });
            const count = await canvas.count();
            if (count !== 1) throw new Error(`expected one visible canvas, got ${count}`);
            const snapshot = await canvas.first().evaluate(async (node) => {
              const canvas = node;
              const ctx = canvas.getContext("2d", { willReadFrequently: true });
              const deadline = performance.now() + 10000;
              let stable = 0;
              let lastActive = 0;
              const activePixels = (image) => {
                let active = 0;
                for (let index = 0; index < image.length; index += 4) {
                  const red = image[index];
                  const green = image[index + 1];
                  const blue = image[index + 2];
                  const alpha = image[index + 3];
                  if (alpha >= 24 && Math.max(red, green, blue) >= 24) active += 1;
                }
                return active;
              };

              while (performance.now() < deadline) {
                await new Promise((resolve) => requestAnimationFrame(resolve));
                const image = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
                lastActive = activePixels(image);
                if (lastActive > 0) {
                  stable += 1;
                  if (stable >= 2) {
                    return { width: canvas.width, height: canvas.height, rgba: Array.from(image) };
                  }
                } else {
                  stable = 0;
                }
              }

              const state = window.__awtrixCanvasRenderState || { drawCount: 0, lastDrawAt: 0 };
              throw new Error(
                `canvas did not render a non-empty frame before timeout; ` +
                  `size=${canvas.width}x${canvas.height}; ` +
                  `drawCount=${state.drawCount}; ` +
                  `lastDrawAt=${Math.round(state.lastDrawAt)}; ` +
                  `lastActive=${lastActive}`
              );
            });
            console.log(JSON.stringify(snapshot));
          } finally {
            await browser.close();
          }
        })().catch((error) => {
          console.error(error && error.message ? error.message : String(error));
          process.exit(1);
        });
        """
    )


async def wait_for_state(
    label: str,
    auth: BrowserAuth,
    predicate,
    *,
    timeout_seconds: float = 25,
    stable_frames: int = 3,
):
    deadline = time.monotonic() + timeout_seconds
    stable = 0
    last_grid = None
    last_detail = ""
    while time.monotonic() < deadline:
        sample = await sample_canvas_grid(auth)
        last_grid = sample.grid
        matched = False
        if sample.valid:
            result = predicate(sample.grid)
            if isinstance(result, tuple):
                matched = bool(result[0])
                last_detail = str(result[1])
            else:
                matched = bool(result)
                last_detail = f"active={sample.diagnostics.get('active_pixels')}"
        else:
            last_detail = f"invalid geometry: {sample.diagnostics}"
        if sample.valid and matched:
            stable += 1
            if stable >= stable_frames:
                return sample.grid
        else:
            stable = 0
        await asyncio.sleep(0.8)
    detail = ""
    try:
        sample = await sample_canvas_grid(auth)
        detail = f"; last canvas diagnostics={sample.diagnostics}"
    except Exception:
        pass
    if last_detail:
        detail = f"; last={last_detail}" + detail
    raise RuntimeError(f"timed out waiting for {label}{detail}")


async def wait_for_native_restore(
    auth: BrowserAuth,
    baseline_clusters,
    *,
    label: str,
    timeout_seconds: float = 35,
    stable_frames: int = 3,
):
    def restored(grid):
        check = restore_state_check(
            valid_geometry=True,
            grid=grid,
            baseline_clusters=baseline_clusters,
            diagnostic_ids=(),
            pattern_state_known=True,
        )
        return check.success, check.reason

    return await wait_for_state(label, auth, restored, timeout_seconds=timeout_seconds, stable_frames=stable_frames)


async def observe_final_pattern(auth: BrowserAuth):
    def matches(grid):
        match = final_pattern_match(grid)
        right = right_zone_has_clock_pixels(grid)
        success = match.success and right
        return (
            success,
            f"final matched {match.matched_cells}/{match.expected_cells}; "
            f"active_left={match.active_cells}; right_clock={right};\n{match.summary}",
        )

    return await wait_for_state(
        "final custom 10x8 all-pixel pattern",
        auth,
        matches,
        timeout_seconds=75,
        stable_frames=2,
    )


async def run_live_test() -> int:
    require_opt_in()
    with single_run_lock():
        mqtt_host, mqtt_port, mqtt_username, mqtt_password = read_mqtt_config()
        browser_auth = read_browser_auth()
        publisher = GuardedMqttPublisher(mqtt_host, mqtt_port, mqtt_username, mqtt_password)
        custom_published = False
        baseline_clusters = ()
        await publisher.start()
        try:
            preflight_cleanup = await cleanup_live_custom(publisher)
            if not preflight_cleanup.success:
                raise RuntimeError("preflight cleanup publish failed")
            native_grid = await wait_for_state("native clock before baseline", browser_auth, native_clock_like)
            baseline_clusters = active_color_clusters(native_grid)
            if not baseline_clusters:
                raise RuntimeError("native baseline palette is empty")

            cleanup = await cleanup_live_custom(publisher)
            if not cleanup.success:
                raise RuntimeError("cleanup before final pattern failed")
            await wait_for_native_restore(
                browser_auth,
                baseline_clusters,
                label="native restore before final all-pixel pattern",
                timeout_seconds=40,
                stable_frames=2,
            )

            payload = build_pattern_payload(all_pixel_pattern_10x8(), datetime.now())
            await publish_live_custom(publisher, payload)
            await publish_live_switch(publisher)
            custom_published = True
            print("final: published 10x8 all-pixel pattern and switched to test app")

            await observe_final_pattern(browser_auth)
            cleanup = await cleanup_live_custom(publisher)
            if not cleanup.success:
                raise RuntimeError("restore cleanup publish failed")
            await wait_for_native_restore(
                browser_auth,
                baseline_clusters,
                label="native restore with baseline-compatible palette",
                timeout_seconds=45,
                stable_frames=3,
            )
            custom_published = False
            print("OK bedroom-clock live test passed: final 10x8 pattern visible, all 80 pixels confirmed, restore baseline-compatible")
            return 0
        finally:
            if custom_published:
                cleanup = await cleanup_live_custom(publisher)
                status = "verified unavailable"
                if cleanup.success and baseline_clusters:
                    try:
                        await wait_for_native_restore(
                            browser_auth,
                            baseline_clusters,
                            label="cleanup verification",
                            timeout_seconds=12,
                            stable_frames=2,
                        )
                        status = "verified"
                    except Exception:
                        status = "publish done, visual verification unavailable"
                print(f"cleanup: {status}")
            await publisher.stop()


def main() -> int:
    try:
        return asyncio.run(run_live_test())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
