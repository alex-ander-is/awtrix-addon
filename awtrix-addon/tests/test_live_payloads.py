from __future__ import annotations

import asyncio
import importlib.util
import importlib
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

from awtrix_addon.live_payload_helpers import (
    ASSET_WIDTH,
    ASSET_X,
    WIDTH,
    LIVE_CUSTOM_TOPIC,
    LIVE_SWITCH_TOPIC,
    active_color_clusters,
    all_pixel_pattern_10x8,
    build_awtrix_payload,
    build_pattern_payload,
    build_live_test_payload,
    challenge_pattern_10x8,
    cleanup_live_custom,
    expected_frame_grid,
    final_pattern_match,
    has_distinct_80_pixels,
    left_zone_matches_pattern,
    match_current_fingerprint,
    pattern_to_image,
    pattern_absent,
    publish_live_custom,
    publish_live_switch,
    live_switch_payload,
    right_zone_has_clock_pixels,
    restore_state_check,
    sample_detected_canvas_grid,
    validate_live_publish,
)


class FakePublisher:
    def __init__(self, *, fail_first: bool = False):
        self.fail_first = fail_first
        self.published: list[tuple[str, str | bytes]] = []

    async def publish(self, topic: str, payload: str | bytes) -> None:
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("temporary failure")
        self.published.append((topic, payload))


class LivePayloadHelperTests(unittest.TestCase):
    def test_pattern_has_80_distinct_non_black_pixels(self):
        pattern = all_pixel_pattern_10x8()
        self.assertEqual(len(pattern), 8)
        self.assertTrue(all(len(row) == 10 for row in pattern))
        self.assertTrue(has_distinct_80_pixels(pattern))

    def test_expected_frame_places_pattern_in_left_zone(self):
        now = datetime(2026, 6, 28, 12, 0, 0)
        frame = expected_frame_grid(now)
        self.assertEqual(len(frame), 8)
        self.assertTrue(all(len(row) == 32 for row in frame))
        self.assertTrue(left_zone_matches_pattern(frame, all_pixel_pattern_10x8()))

    def test_payload_uses_production_draw_buffer_shape(self):
        payload = json.loads(build_live_test_payload(datetime(2026, 6, 28, 12, 0, 0)))
        self.assertEqual(payload["duration"], 1)
        draw = payload["draw"][0]["db"]
        self.assertEqual(draw[:4], [0, 0, 32, 8])
        self.assertIsInstance(draw[4], list)
        self.assertEqual(len(draw[4]), 32 * 8)
        self.assertTrue(all(isinstance(value, int) for value in draw[4]))

    def test_payload_duration_can_follow_event_duration(self):
        payload = json.loads(
            build_awtrix_payload(
                pattern_to_image(all_pixel_pattern_10x8()),
                datetime(2026, 6, 28, 12, 0, 0),
                duration=30,
            )
        )

        self.assertEqual(payload["duration"], 30)

    def test_payload_rejects_old_hex_string_db_regression(self):
        payload = json.loads(build_live_test_payload(datetime(2026, 6, 28, 12, 0, 0)))
        bmp = payload["draw"][0]["db"][4]

        self.assertNotIsInstance(bmp, str)
        self.assertEqual(len(bmp), 256)

    def test_live_payload_is_custom_draw_only(self):
        now = datetime(2026, 6, 28, 12, 0, 0)
        pattern = challenge_pattern_10x8("diag")
        payload = json.loads(build_pattern_payload(pattern, now))
        self.assertIn("draw", payload)
        self.assertTrue(payload["noScroll"])
        self.assertEqual(payload["duration"], 30)
        self.assertEqual(payload["lifetime"], 90)
        self.assertNotIn("settings", payload)
        self.assertNotIn("palette", payload)
        self.assertNotIn("brightness", payload)
        self.assertIsInstance(payload["draw"][0]["db"][4], list)

    def test_topic_allowlist_only_permits_bedroom_clock_test_custom_app(self):
        validate_live_publish(LIVE_CUSTOM_TOPIC, "{}")
        validate_live_publish(LIVE_SWITCH_TOPIC, live_switch_payload())
        for topic in (
            "bedroom-clock/custom/awtrix_addon",
            "bedroom-clock/switch/awtrix_addon_live_test",
            "bedroom-clock/custom/app",
            "bedroom-clock/settings",
            "bedroom-clock/palette",
            "bedroom-clock/brightness",
            "bedroom-clock/moodlight",
            "kitchen-clock/custom/awtrix_addon_live_test",
            "bedroom-clock/custom/+",
            "bedroom-clock/#",
        ):
            with self.subTest(topic=topic):
                with self.assertRaises(ValueError):
                    validate_live_publish(topic, "")

    def test_cleanup_publishes_only_empty_payload_to_custom_topic(self):
        publisher = FakePublisher()
        result = asyncio.run(cleanup_live_custom(publisher))
        self.assertTrue(result.success)
        self.assertEqual(publisher.published, [(LIVE_CUSTOM_TOPIC, "")])

    def test_cleanup_retries_after_exception(self):
        publisher = FakePublisher(fail_first=True)
        result = asyncio.run(cleanup_live_custom(publisher, attempts=2, delay_seconds=0))
        self.assertTrue(result.success)
        self.assertEqual(publisher.published, [(LIVE_CUSTOM_TOPIC, "")])

    def test_publish_live_custom_rejects_forbidden_topic_through_validator(self):
        publisher = FakePublisher()
        asyncio.run(publish_live_custom(publisher, "payload"))
        self.assertEqual(publisher.published, [(LIVE_CUSTOM_TOPIC, "payload")])

    def test_switch_publish_is_exactly_allowlisted_to_test_app(self):
        publisher = FakePublisher()
        asyncio.run(publish_live_switch(publisher))
        self.assertEqual(publisher.published, [(LIVE_SWITCH_TOPIC, '{"name":"awtrix_addon_live_test","fast":true}')])

        for payload in (
            '{"name":"Clock","fast":true}',
            '{"name":"awtrix_addon_live_test"}',
            '{"name":"awtrix_addon_live_test","fast":false}',
            '{"name":"awtrix_addon_live_test","fast":true,"extra":1}',
            b'{"name":"awtrix_addon_live_test","fast":true}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_live_publish(LIVE_SWITCH_TOPIC, payload)

    def test_helpers_import_without_live_dependencies_or_env(self):
        module = importlib.import_module("awtrix_addon.live_payload_helpers")
        self.assertTrue(module.has_distinct_80_pixels())
        self.assertEqual(pattern_to_image(module.all_pixel_pattern_10x8()).size, (10, 8))

    def test_detected_sampler_recovers_10x8_pattern_from_1052_canvas_blocks(self):
        logical = blank_logical_grid()
        pattern = all_pixel_pattern_10x8()
        for y, row in enumerate(pattern):
            for x, color in enumerate(row):
                logical[y][ASSET_X + x] = color
        add_right_zone_anchor(logical)
        width, height, rgba = synthetic_canvas(logical)

        sample = sample_detected_canvas_grid(width, height, rgba)

        self.assertTrue(sample.valid, sample.diagnostics)
        self.assertTrue(left_zone_matches_pattern(sample.grid, pattern))

    def test_detected_sampler_recovers_sparse_native_grid(self):
        logical = blank_logical_grid()
        for y in range(8):
            for x in (0, 4, 9, 14, 20, 27, 31):
                logical[y][x] = (235, 238, 239)
        width, height, rgba = synthetic_canvas(logical)

        sample = sample_detected_canvas_grid(width, height, rgba)

        self.assertTrue(sample.valid, sample.diagnostics)
        self.assertTrue(right_zone_has_clock_pixels(sample.grid))
        self.assertTrue(pattern_absent(sample.grid))

    def test_broad_gradient_region_is_rejected_fail_closed(self):
        width, height = 1052, 260
        rgba = [0, 0, 0, 255] * width * height
        for y in range(20, 190):
            for x in range(80, 860):
                value = 50 + ((x + y) % 80)
                put_pixel(rgba, width, x, y, (value, value, value))

        sample = sample_detected_canvas_grid(width, height, rgba)

        self.assertFalse(sample.valid)
        self.assertFalse(pattern_absent_when_geometry_unknown(sample))

    def test_naive_equal_cell_sampler_is_wrong_but_detected_sampler_passes(self):
        logical = blank_logical_grid()
        pattern = all_pixel_pattern_10x8()
        for y, row in enumerate(pattern):
            for x, color in enumerate(row):
                logical[y][ASSET_X + x] = color
        add_right_zone_anchor(logical)
        width, height, rgba = synthetic_canvas(logical, left=17, top=11, block=22, pitch=30)

        naive = naive_equal_cell_sample(width, height, rgba)
        detected = sample_detected_canvas_grid(width, height, rgba)

        self.assertFalse(left_zone_matches_pattern(naive, pattern))
        self.assertTrue(detected.valid, detected.diagnostics)
        self.assertTrue(left_zone_matches_pattern(detected.grid, pattern))

    def test_invalid_geometry_is_not_restored_or_pattern_absent(self):
        sample = sample_detected_canvas_grid(1052, 260, [0, 0, 0, 255] * 1052 * 260)

        self.assertFalse(sample.valid)
        self.assertFalse(restored_when_geometry_valid(sample))
        self.assertFalse(pattern_absent_when_geometry_unknown(sample))

    def test_restore_requires_consecutive_valid_grids(self):
        valid_grid = blank_logical_grid()
        for y in range(8):
            for x in (12, 13, 20, 21):
                valid_grid[y][x] = (240, 240, 240)
        width, height, rgba = synthetic_canvas(valid_grid)
        valid = sample_detected_canvas_grid(width, height, rgba)
        invalid = sample_detected_canvas_grid(1052, 260, [0, 0, 0, 255] * 1052 * 260)

        self.assertFalse(stable_restore([valid, invalid, valid, valid], required=3))
        self.assertTrue(stable_restore([valid, valid, valid], required=3))

    def test_current_attempt_fingerprint_rejects_prior_stale_fingerprint(self):
        observed = grid_with_left_pattern(challenge_pattern_10x8("fingerprint_a"))

        match = match_current_fingerprint(observed, "fingerprint_b", ("fingerprint_a",))

        self.assertFalse(match.success)
        self.assertEqual(match.stale_id, "fingerprint_a")

    def test_variant_a_visible_during_variant_b_wait_does_not_pass_b(self):
        frame_a = grid_with_left_pattern(challenge_pattern_10x8("variant_a"))
        stable = 0
        for _frame in range(4):
            match = match_current_fingerprint(frame_a, "variant_b", ("variant_a",))
            if match.success:
                stable += 1
            else:
                stable = 0

        self.assertEqual(stable, 0)

    def test_restore_semantics_fail_closed(self):
        native = blank_logical_grid()
        for y in range(8):
            for x in (12, 13, 20, 21):
                native[y][x] = (240, 240, 240)
        native_grid = tuple(tuple(row) for row in native)
        baseline = active_color_clusters(native_grid)

        self.assertTrue(
            restore_state_check(
                valid_geometry=True,
                grid=native_grid,
                baseline_clusters=baseline,
                diagnostic_ids=("diag_a",),
                pattern_state_known=True,
            ).success
        )
        self.assertFalse(
            restore_state_check(
                valid_geometry=False,
                grid=native_grid,
                baseline_clusters=baseline,
                diagnostic_ids=(),
                pattern_state_known=True,
            ).success
        )
        self.assertFalse(
            restore_state_check(
                valid_geometry=True,
                grid=native_grid,
                baseline_clusters=baseline,
                diagnostic_ids=(),
                pattern_state_known=False,
            ).success
        )
        self.assertFalse(
            restore_state_check(
                valid_geometry=True,
                grid=native_grid,
                baseline_clusters=((255, 0, 0),),
                diagnostic_ids=(),
                pattern_state_known=True,
            ).success
        )
        self.assertFalse(
            restore_state_check(
                valid_geometry=True,
                grid=grid_with_left_pattern(challenge_pattern_10x8("diag_a"), include_native=True),
                baseline_clusters=baseline,
                diagnostic_ids=("diag_a",),
                pattern_state_known=True,
            ).success
        )
        self.assertFalse(
            restore_state_check(
                valid_geometry=True,
                grid=grid_with_left_pattern(all_pixel_pattern_10x8(), include_native=True),
                baseline_clusters=baseline,
                diagnostic_ids=(),
                pattern_state_known=True,
            ).success
        )
        self.assertFalse(
            restore_state_check(
                valid_geometry=True,
                grid=tuple(tuple(row) for row in blank_logical_grid()),
                baseline_clusters=baseline,
                diagnostic_ids=(),
                pattern_state_known=True,
            ).success
        )

    def test_final_pattern_requires_all_80_left_pixels(self):
        complete = grid_with_left_pattern(all_pixel_pattern_10x8(), include_native=True)
        missing = [list(row) for row in complete]
        missing[7][ASSET_X + ASSET_WIDTH - 1] = (0, 0, 0)

        self.assertTrue(final_pattern_match(complete).success)
        self.assertFalse(final_pattern_match(tuple(tuple(row) for row in missing)).success)

    def test_final_pattern_rejects_all_active_wrong_colors(self):
        wrong = grid_with_left_pattern(tuple(tuple((0, 255, 0) for _x in range(10)) for _y in range(8)), include_native=True)

        match = final_pattern_match(wrong)

        self.assertFalse(match.success)
        self.assertEqual(match.active_cells, 80)
        self.assertLess(match.matched_cells, 80)

    def test_wrong_all_active_grid_does_not_accumulate_stable_final_frames(self):
        wrong = grid_with_left_pattern(tuple(tuple((0, 255, 0) for _x in range(10)) for _y in range(8)), include_native=True)
        stable = 0
        for _frame in range(4):
            if final_pattern_match(wrong).success and right_zone_has_clock_pixels(wrong):
                stable += 1
            else:
                stable = 0

        self.assertEqual(stable, 0)


class LiveProbeScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "live_bedroom_clock.py"
        spec = importlib.util.spec_from_file_location("live_bedroom_clock_probe_contract", script_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.probe_script = module.build_probe_script()

    def test_probe_installs_draw_hook_before_navigation(self):
        self.assertIn("await page.addInitScript", self.probe_script)
        self.assertIn("__awtrixCanvasRenderState", self.probe_script)
        self.assertLess(
            self.probe_script.index("await page.addInitScript"),
            self.probe_script.index("await page.goto"),
        )

    def test_probe_waits_for_animation_frame_before_snapshot(self):
        self.assertIn("requestAnimationFrame", self.probe_script)
        self.assertLess(
            self.probe_script.index("requestAnimationFrame"),
            self.probe_script.index("return { width: canvas.width, height: canvas.height, rgba: Array.from(image) }"),
        )

    def test_probe_rejects_all_black_frames(self):
        self.assertIn("lastActive > 0", self.probe_script)
        self.assertIn("stable = 0", self.probe_script)
        self.assertIn("canvas did not render a non-empty frame before timeout", self.probe_script)

    def test_probe_requires_consecutive_non_empty_frames(self):
        self.assertIn("stable += 1", self.probe_script)
        self.assertIn("stable >= 2", self.probe_script)


def blank_logical_grid() -> list[list[tuple[int, int, int]]]:
    return [[(0, 0, 0) for _x in range(WIDTH)] for _y in range(8)]


def add_right_zone_anchor(grid: list[list[tuple[int, int, int]]]) -> None:
    for y in range(8):
        for x in (12, 16, 21, 27, 31):
            grid[y][x] = (235, 238, 239)


def grid_with_left_pattern(pattern, *, include_native: bool = False):
    grid = blank_logical_grid()
    for y, row in enumerate(pattern):
        for x, color in enumerate(row):
            grid[y][ASSET_X + x] = color
    if include_native:
        for y in range(8):
            for x in (12, 13, 20, 21):
                grid[y][x] = (240, 240, 240)
    return tuple(tuple(row) for row in grid)


def synthetic_canvas(
    logical: list[list[tuple[int, int, int]]],
    *,
    width: int = 1052,
    height: int = 260,
    left: int = 13,
    top: int = 7,
    block: int = 24,
    pitch: int = 31,
) -> tuple[int, int, list[int]]:
    rgba = [0, 0, 0, 255] * width * height
    for y, row in enumerate(logical):
        for x, color in enumerate(row):
            if max(color) < 24:
                continue
            x0 = left + x * pitch
            y0 = top + y * pitch
            for py in range(y0, y0 + block):
                for px in range(x0, x0 + block):
                    put_pixel(rgba, width, px, py, color)
    return width, height, rgba


def put_pixel(rgba: list[int], width: int, x: int, y: int, color: tuple[int, int, int]) -> None:
    offset = (y * width + x) * 4
    rgba[offset : offset + 4] = [color[0], color[1], color[2], 255]


def naive_equal_cell_sample(width: int, height: int, rgba: list[int]):
    rows = []
    cell_w = width / 32
    cell_h = height / 8
    for y in range(8):
        row = []
        for x in range(32):
            px = min(width - 1, int((x + 0.5) * cell_w))
            py = min(height - 1, int((y + 0.5) * cell_h))
            offset = (py * width + px) * 4
            row.append(tuple(rgba[offset : offset + 3]))
        rows.append(tuple(row))
    return tuple(rows)


def pattern_absent_when_geometry_unknown(sample) -> bool:
    return sample.valid and pattern_absent(sample.grid)


def restored_when_geometry_valid(sample) -> bool:
    return sample.valid and right_zone_has_clock_pixels(sample.grid) and pattern_absent(sample.grid)


def stable_restore(samples, *, required: int) -> bool:
    stable = 0
    for sample in samples:
        if restored_when_geometry_valid(sample):
            stable += 1
            if stable >= required:
                return True
        else:
            stable = 0
    return False
