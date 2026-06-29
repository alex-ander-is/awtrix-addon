from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import json
from statistics import median
from typing import Protocol, Sequence

from PIL import Image

from awtrix_addon.renderer import ASSET_WIDTH, HEIGHT, WIDTH, build_awtrix_payload, image_to_uint32_bitmap, render_frame


LIVE_CLOCK_PREFIX = "bedroom-clock"
LIVE_APP_NAME = "awtrix_addon_live_test"
LIVE_CUSTOM_TOPIC = f"{LIVE_CLOCK_PREFIX}/custom/{LIVE_APP_NAME}"
LIVE_SWITCH_TOPIC = f"{LIVE_CLOCK_PREFIX}/switch"
LIVE_SCREEN_URL = "http://bedroom-clock.ander.is/screen"
FORBIDDEN_TOPIC_PARTS = ("settings", "brightness", "palette", "moodlight")
COLOR_TOLERANCE = 12

RGB = tuple[int, int, int]
Grid = tuple[tuple[RGB, ...], ...]
RawRgba = Sequence[int]


class AsyncPublisher(Protocol):
    async def publish(self, topic: str, payload: str | bytes) -> None: ...


@dataclass(frozen=True)
class CleanupResult:
    attempts: int
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class LedComponent:
    left: int
    top: int
    right: int
    bottom: int
    pixels: int

    @property
    def width(self) -> int:
        return self.right - self.left + 1

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def fill_ratio(self) -> float:
        return self.pixels / (self.width * self.height)


@dataclass(frozen=True)
class AxisFit:
    origin: float
    pitch: float
    block_size: int
    matched: int
    residual: float


@dataclass(frozen=True)
class CanvasGridSample:
    valid: bool
    grid: Grid
    diagnostics: dict[str, float | int | str]
    x_fit: AxisFit | None = None
    y_fit: AxisFit | None = None


@dataclass(frozen=True)
class FingerprintMatch:
    success: bool
    stale_id: str | None = None
    matched_cells: int = 0
    expected_cells: int = 0
    active_cells: int = 0
    summary: str = ""


@dataclass(frozen=True)
class RestoreCheck:
    success: bool
    reason: str


def validate_live_publish(topic: str, payload: str | bytes) -> None:
    if topic not in (LIVE_CUSTOM_TOPIC, LIVE_SWITCH_TOPIC):
        raise ValueError(f"live publish topic is not allowlisted: {topic}")
    if "#" in topic or "+" in topic:
        raise ValueError("wildcard live publish topics are forbidden")
    parts = topic.split("/")
    if any(part in FORBIDDEN_TOPIC_PARTS for part in parts):
        raise ValueError("live publish to settings/palette/brightness/moodlight is forbidden")
    if not topic.startswith(f"{LIVE_CLOCK_PREFIX}/"):
        raise ValueError("live publish prefix must be bedroom-clock")
    if payload is None:
        raise ValueError("live publish payload must not be None")
    if topic == LIVE_SWITCH_TOPIC:
        if not isinstance(payload, str):
            raise ValueError("live switch payload must be JSON text")
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("live switch payload must be valid JSON") from exc
        if body != {"name": LIVE_APP_NAME, "fast": True}:
            raise ValueError("live switch payload must select only awtrix_addon_live_test")


async def publish_live_custom(publisher: AsyncPublisher, payload: str | bytes) -> None:
    validate_live_publish(LIVE_CUSTOM_TOPIC, payload)
    await publisher.publish(LIVE_CUSTOM_TOPIC, payload)


async def publish_live_switch(publisher: AsyncPublisher) -> None:
    payload = live_switch_payload()
    validate_live_publish(LIVE_SWITCH_TOPIC, payload)
    await publisher.publish(LIVE_SWITCH_TOPIC, payload)


def live_switch_payload() -> str:
    return json.dumps({"name": LIVE_APP_NAME, "fast": True}, separators=(",", ":"))


async def cleanup_live_custom(
    publisher: AsyncPublisher,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.4,
) -> CleanupResult:
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            await publish_live_custom(publisher, "")
            return CleanupResult(attempts=attempt, success=True)
        except Exception as exc:  # pragma: no cover - exact exception depends on live publisher
            last_error = exc.__class__.__name__
            if attempt < attempts:
                await asyncio.sleep(delay_seconds)
    return CleanupResult(attempts=attempts, success=False, error=last_error)


def all_pixel_pattern_10x8() -> Grid:
    rows: list[tuple[RGB, ...]] = []
    for y in range(HEIGHT):
        row: list[RGB] = []
        for x in range(ASSET_WIDTH):
            index = y * ASSET_WIDTH + x
            red = 48 + ((index * 73) % 208)
            green = 48 + ((index * 151 + 37) % 208)
            blue = 48 + ((index * 199 + 91) % 208)
            row.append((red, green, blue))
        rows.append(tuple(row))
    return tuple(rows)


def challenge_pattern_10x8(fingerprint_id: str) -> Grid:
    seed = sum((index + 1) * ord(char) for index, char in enumerate(fingerprint_id))
    rows: list[tuple[RGB, ...]] = []
    for y in range(HEIGHT):
        row: list[RGB] = []
        for x in range(ASSET_WIDTH):
            value = (seed + x * 29 + y * 47 + x * y * 11) % 7
            if value in (0, 3):
                row.append((255, 32 + (seed % 96), 32))
            elif value in (1, 5):
                row.append((32, 255, 64 + ((seed + x * 13) % 96)))
            elif value == 2:
                row.append((64 + ((seed + y * 17) % 96), 80, 255))
            else:
                row.append((0, 0, 0))
        rows.append(tuple(row))
    if not any(is_active(color) for row in rows for color in row):
        return all_pixel_pattern_10x8()
    return tuple(rows)


def pattern_to_image(pattern: Grid) -> Image.Image:
    if len(pattern) != HEIGHT or any(len(row) != ASSET_WIDTH for row in pattern):
        raise ValueError("pattern must be 10x8")
    image = Image.new("RGB", (ASSET_WIDTH, HEIGHT), (0, 0, 0))
    for y, row in enumerate(pattern):
        for x, color in enumerate(row):
            image.putpixel((x, y), color)
    return image


def build_live_test_payload(now: datetime) -> str:
    return build_awtrix_payload(pattern_to_image(all_pixel_pattern_10x8()), now)


def build_pattern_payload(pattern: Grid, now: datetime, *, variant: str = "production_draw") -> str:
    image = pattern_to_image(pattern)
    frame = render_frame(image, now)
    if variant == "production_draw":
        return json.dumps(
            {
                "draw": [{"db": [0, 0, WIDTH, HEIGHT, image_to_uint32_bitmap(frame)]}],
                "duration": 30,
                "lifetime": 90,
                "noScroll": True,
            },
            separators=(",", ":"),
        )
    raise ValueError(f"unknown live payload variant: {variant}")


def expected_frame_grid(now: datetime) -> Grid:
    return image_to_grid(render_frame(pattern_to_image(all_pixel_pattern_10x8()), now))


def image_to_grid(image: Image.Image) -> Grid:
    rgb = image.convert("RGB")
    return tuple(tuple(rgb.getpixel((x, y)) for x in range(rgb.width)) for y in range(rgb.height))


def sample_detected_canvas_grid(
    width: int,
    height: int,
    rgba: RawRgba,
    *,
    active_threshold: int = 40,
) -> CanvasGridSample:
    if width <= 0 or height <= 0:
        return invalid_canvas_sample(width, height, "empty canvas")
    if len(rgba) < width * height * 4:
        return invalid_canvas_sample(width, height, "rgba data too short")

    mask = build_active_mask(width, height, rgba, active_threshold=active_threshold)
    active_count = sum(1 for value in mask if value)
    components = connected_led_components(width, height, mask)
    accepted = [
        component
        for component in components
        if compact_led_component(component, width=width, height=height)
    ]
    diagnostics: dict[str, float | int | str] = {
        "canvas_width": width,
        "canvas_height": height,
        "active_pixels": active_count,
        "component_count": len(components),
        "accepted_block_count": len(accepted),
    }
    if len(accepted) < 8:
        diagnostics["reason"] = "not enough compact LED blocks"
        return CanvasGridSample(False, blank_grid(), diagnostics)

    x_fit = fit_grid_axis([component.center_x for component in accepted], WIDTH, width)
    y_fit = fit_grid_axis([component.center_y for component in accepted], HEIGHT, height)
    diagnostics.update(
        {
            "x_score": x_fit.matched if x_fit else 0,
            "y_score": y_fit.matched if y_fit else 0,
            "x_pitch": round(x_fit.pitch, 3) if x_fit else 0,
            "y_pitch": round(y_fit.pitch, 3) if y_fit else 0,
            "x_origin": round(x_fit.origin, 3) if x_fit else 0,
            "y_origin": round(y_fit.origin, 3) if y_fit else 0,
        }
    )
    if x_fit is None or y_fit is None:
        diagnostics["reason"] = "could not fit 32x8 LED grid"
        return CanvasGridSample(False, blank_grid(), diagnostics, x_fit=x_fit, y_fit=y_fit)
    if y_fit.matched < min(HEIGHT, len(accepted)):
        diagnostics["reason"] = "could not recover 8 LED rows"
        return CanvasGridSample(False, blank_grid(), diagnostics, x_fit=x_fit, y_fit=y_fit)
    if x_fit.matched < 4:
        diagnostics["reason"] = "not enough matched LED columns"
        return CanvasGridSample(False, blank_grid(), diagnostics, x_fit=x_fit, y_fit=y_fit)

    grid = sample_grid_from_fits(width, height, rgba, x_fit, y_fit)
    diagnostics["reason"] = "ok"
    return CanvasGridSample(True, grid, diagnostics, x_fit=x_fit, y_fit=y_fit)


def invalid_canvas_sample(width: int, height: int, reason: str) -> CanvasGridSample:
    return CanvasGridSample(
        False,
        blank_grid(),
        {"canvas_width": width, "canvas_height": height, "reason": reason},
    )


def blank_grid() -> Grid:
    return tuple(tuple((0, 0, 0) for _x in range(WIDTH)) for _y in range(HEIGHT))


def build_active_mask(width: int, height: int, rgba: RawRgba, *, active_threshold: int) -> list[bool]:
    mask: list[bool] = []
    for index in range(width * height):
        offset = index * 4
        red, green, blue, alpha = rgba[offset], rgba[offset + 1], rgba[offset + 2], rgba[offset + 3]
        brightness = max(red, green, blue)
        colorfulness = max(red, green, blue) - min(red, green, blue)
        mask.append(alpha >= 24 and (brightness >= active_threshold or (brightness >= 28 and colorfulness >= 24)))
    return mask


def connected_led_components(width: int, height: int, mask: Sequence[bool]) -> list[LedComponent]:
    seen = bytearray(width * height)
    components: list[LedComponent] = []
    for start, active in enumerate(mask):
        if not active or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        left = right = start % width
        top = bottom = start // width
        pixels = 0
        while stack:
            index = stack.pop()
            pixels += 1
            x = index % width
            y = index // width
            left = min(left, x)
            right = max(right, x)
            top = min(top, y)
            bottom = max(bottom, y)
            for neighbor in component_neighbors(index, x, y, width, height):
                if mask[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    stack.append(neighbor)
        components.append(LedComponent(left, top, right, bottom, pixels))
    return components


def component_neighbors(index: int, x: int, y: int, width: int, height: int) -> tuple[int, ...]:
    neighbors: list[int] = []
    if x > 0:
        neighbors.append(index - 1)
    if x < width - 1:
        neighbors.append(index + 1)
    if y > 0:
        neighbors.append(index - width)
    if y < height - 1:
        neighbors.append(index + width)
    return tuple(neighbors)


def compact_led_component(component: LedComponent, *, width: int, height: int) -> bool:
    if component.width < 3 or component.height < 3:
        return False
    if component.width > width / 10 or component.height > height / 3:
        return False
    if component.fill_ratio < 0.72:
        return False
    aspect = component.width / component.height
    if not 0.45 <= aspect <= 2.2:
        return False
    return True


def fit_grid_axis(centers: Sequence[float], slots: int, canvas_size: int) -> AxisFit | None:
    unique = sorted(set(round(center, 3) for center in centers))
    if len(unique) < min(slots, 3):
        return None
    pitch_candidates = axis_pitch_candidates(unique, slots, canvas_size)
    best: AxisFit | None = None
    for pitch in pitch_candidates:
        origin_candidates = []
        for center in unique:
            for index in range(slots):
                origin_candidates.append(center - index * pitch)
        for origin in origin_candidates:
            fit = score_axis_fit(unique, slots, canvas_size, origin, pitch)
            if fit is None:
                continue
            if best is None or axis_fit_key(fit) > axis_fit_key(best):
                best = fit
    min_matches = min(len(unique), max(3, slots // 2))
    if best is None or best.matched < min_matches:
        return None
    return best


def axis_pitch_candidates(centers: Sequence[float], slots: int, canvas_size: int) -> tuple[float, ...]:
    counts: Counter[float] = Counter()
    lower = max(4.0, canvas_size / (slots * 1.6))
    upper = canvas_size / max(1, slots - 1) * 1.8
    for left_index, left in enumerate(centers):
        for right in centers[left_index + 1 :]:
            diff = right - left
            if diff < lower:
                continue
            for step in range(1, slots):
                pitch = diff / step
                if lower <= pitch <= upper:
                    counts[round(pitch, 2)] += 1
    nominal = canvas_size / slots
    counts[round(nominal, 2)] += max(1, len(centers) // 2)
    return tuple(pitch for pitch, _count in counts.most_common(24))


def score_axis_fit(
    centers: Sequence[float],
    slots: int,
    canvas_size: int,
    origin: float,
    pitch: float,
) -> AxisFit | None:
    tolerance = max(3.0, pitch * 0.22)
    mapped: dict[int, float] = {}
    residuals: list[float] = []
    duplicates = 0
    for center in centers:
        index = round((center - origin) / pitch)
        if index < 0 or index >= slots:
            continue
        expected = origin + index * pitch
        residual = abs(center - expected)
        if residual > tolerance:
            continue
        if index in mapped:
            duplicates += 1
            if residual < mapped[index]:
                mapped[index] = residual
        else:
            mapped[index] = residual
    first = origin
    last = origin + (slots - 1) * pitch
    if first < -tolerance or last > canvas_size + tolerance:
        return None
    if duplicates:
        return None
    if not mapped:
        return None
    residuals = list(mapped.values())
    block_size = max(3, int(round(pitch * 0.82)))
    return AxisFit(
        origin=origin,
        pitch=pitch,
        block_size=block_size,
        matched=len(mapped),
        residual=sum(residuals) / len(residuals),
    )


def axis_fit_key(fit: AxisFit) -> tuple[int, float, float]:
    return (fit.matched, -fit.residual, -abs(fit.pitch - fit.block_size))


def sample_grid_from_fits(width: int, height: int, rgba: RawRgba, x_fit: AxisFit, y_fit: AxisFit) -> Grid:
    rows: list[tuple[RGB, ...]] = []
    sample_w = max(1, min(x_fit.block_size, int(round(x_fit.pitch * 0.68))) // 2)
    sample_h = max(1, min(y_fit.block_size, int(round(y_fit.pitch * 0.68))) // 2)
    for y_index in range(HEIGHT):
        row: list[RGB] = []
        center_y = y_fit.origin + y_index * y_fit.pitch
        for x_index in range(WIDTH):
            center_x = x_fit.origin + x_index * x_fit.pitch
            row.append(median_rgb(sample_rect(width, height, rgba, center_x, center_y, sample_w, sample_h)))
        rows.append(tuple(row))
    return tuple(rows)


def sample_rect(
    width: int,
    height: int,
    rgba: RawRgba,
    center_x: float,
    center_y: float,
    sample_w: int,
    sample_h: int,
) -> list[RGB]:
    x0 = max(0, int(round(center_x - sample_w / 2)))
    x1 = min(width - 1, int(round(center_x + sample_w / 2)))
    y0 = max(0, int(round(center_y - sample_h / 2)))
    y1 = min(height - 1, int(round(center_y + sample_h / 2)))
    samples: list[RGB] = []
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            offset = (y * width + x) * 4
            samples.append((rgba[offset], rgba[offset + 1], rgba[offset + 2]))
    return samples or [(0, 0, 0)]


def median_rgb(samples: Sequence[RGB]) -> RGB:
    return tuple(int(median(sample[channel] for sample in samples)) for channel in range(3))  # type: ignore[return-value]


def left_zone_matches_pattern(observed: Grid, pattern: Grid | None = None, *, tolerance: int = COLOR_TOLERANCE) -> bool:
    expected = pattern or all_pixel_pattern_10x8()
    if len(observed) < HEIGHT or any(len(row) < ASSET_WIDTH for row in observed[:HEIGHT]):
        return False
    for y in range(HEIGHT):
        for x in range(ASSET_WIDTH):
            if not colors_close(observed[y][x], expected[y][x], tolerance=tolerance):
                return False
    return True


def match_current_fingerprint(
    observed: Grid,
    current_id: str,
    prior_ids: Sequence[str] = (),
    *,
    tolerance: int = 48,
) -> FingerprintMatch:
    current = challenge_pattern_10x8(current_id)
    current_score, current_false_active = occupancy_match_score(observed, current)
    expected = active_cell_count(current)
    prior_scores = {
        prior_id: occupancy_match_score(observed, challenge_pattern_10x8(prior_id))
        for prior_id in prior_ids
    }
    stale_id = None
    if prior_scores:
        stale_id, (stale_score, stale_false_active) = max(prior_scores.items(), key=lambda item: item[1][0])
        stale_expected = active_cell_count(challenge_pattern_10x8(stale_id))
        if stale_score < max(1, int(stale_expected * 0.82)) or stale_false_active > 4:
            stale_id = None
    success = current_score >= max(1, int(expected * 0.82)) and current_false_active <= 4 and stale_id is None
    return FingerprintMatch(
        success=success,
        stale_id=stale_id,
        matched_cells=current_score,
        expected_cells=expected,
        active_cells=left_active_cell_count(observed),
        summary=left_zone_occupancy_summary(observed),
    )


def pattern_match_score(observed: Grid, pattern: Grid, *, tolerance: int = COLOR_TOLERANCE) -> int:
    score = 0
    if len(observed) < HEIGHT or any(len(row) < ASSET_WIDTH for row in observed[:HEIGHT]):
        return 0
    for y in range(HEIGHT):
        for x in range(ASSET_WIDTH):
            expected_active = is_active(pattern[y][x])
            observed_active = is_active(observed[y][x])
            if expected_active and observed_active and colors_close(observed[y][x], pattern[y][x], tolerance=tolerance):
                score += 1
    return score


def occupancy_match_score(observed: Grid, pattern: Grid) -> tuple[int, int]:
    score = 0
    false_active = 0
    if len(observed) < HEIGHT or any(len(row) < ASSET_WIDTH for row in observed[:HEIGHT]):
        return 0, ASSET_WIDTH * HEIGHT
    for y in range(HEIGHT):
        for x in range(ASSET_WIDTH):
            expected = is_active(pattern[y][x])
            actual = is_active(observed[y][x])
            if expected and actual:
                score += 1
            elif not expected and actual:
                false_active += 1
    return score, false_active


def active_cell_count(pattern: Grid) -> int:
    return sum(1 for row in pattern for color in row if is_active(color))


def left_active_cell_count(observed: Grid) -> int:
    return sum(
        1
        for y in range(min(HEIGHT, len(observed)))
        for x in range(min(ASSET_WIDTH, len(observed[y])))
        if is_active(observed[y][x])
    )


def left_zone_occupancy_summary(observed: Grid) -> str:
    rows: list[str] = []
    for y in range(min(HEIGHT, len(observed))):
        row = observed[y]
        rows.append("".join("#" if x < len(row) and is_active(row[x]) else "." for x in range(ASSET_WIDTH)))
    while len(rows) < HEIGHT:
        rows.append("." * ASSET_WIDTH)
    return "\n".join(rows)


def final_pattern_match(observed: Grid, *, tolerance: int = 64) -> FingerprintMatch:
    pattern = all_pixel_pattern_10x8()
    matched = pattern_match_score(observed, pattern, tolerance=tolerance)
    active_cells = left_active_cell_count(observed)
    return FingerprintMatch(
        success=matched == ASSET_WIDTH * HEIGHT,
        matched_cells=matched,
        expected_cells=ASSET_WIDTH * HEIGHT,
        active_cells=active_cells,
        summary=left_zone_occupancy_summary(observed),
    )


def right_zone_has_clock_pixels(observed: Grid) -> bool:
    active = 0
    for y in range(min(HEIGHT, len(observed))):
        row = observed[y]
        for x in range(ASSET_WIDTH, min(WIDTH, len(row))):
            if is_active(row[x]):
                active += 1
    return active >= 8


def pattern_absent(observed: Grid, *, tolerance: int = COLOR_TOLERANCE) -> bool:
    return not left_zone_matches_pattern(observed, tolerance=tolerance)


def restore_state_check(
    *,
    valid_geometry: bool,
    grid: Grid,
    baseline_clusters: Sequence[RGB],
    diagnostic_ids: Sequence[str] = (),
    pattern_state_known: bool = True,
) -> RestoreCheck:
    if not valid_geometry:
        return RestoreCheck(False, "invalid geometry")
    if not pattern_state_known:
        return RestoreCheck(False, "unknown pattern state")
    if not right_zone_has_clock_pixels(grid):
        return RestoreCheck(False, "native active pixels absent")
    candidate_clusters = active_color_clusters(grid)
    if not palette_compatible(baseline_clusters, candidate_clusters):
        return RestoreCheck(False, "palette is not baseline-compatible")
    if final_pattern_match(grid).success:
        return RestoreCheck(False, "final custom pattern still present")
    for diagnostic_id in diagnostic_ids:
        match = match_current_fingerprint(grid, diagnostic_id)
        if match.success:
            return RestoreCheck(False, f"diagnostic fingerprint still present: {diagnostic_id}")
    return RestoreCheck(True, "ok")


def active_color_clusters(grid: Grid, *, bucket: int = 16) -> tuple[RGB, ...]:
    counts: Counter[RGB] = Counter()
    for row in grid:
        for color in row:
            if is_active(color):
                counts[quantize_color(color, bucket=bucket)] += 1
    return tuple(color for color, _count in counts.most_common())


def palette_compatible(baseline: Sequence[RGB], candidate: Sequence[RGB], *, tolerance: int = 24) -> bool:
    if not baseline or not candidate:
        return False
    for color in baseline:
        if not any(colors_close(color, other, tolerance=tolerance) for other in candidate):
            return False
    for color in candidate:
        if not any(colors_close(color, other, tolerance=tolerance) for other in baseline):
            return False
    return True


def native_clock_like(grid: Grid) -> bool:
    return pattern_absent(grid) and right_zone_has_clock_pixels(grid) and bool(active_color_clusters(grid))


def has_distinct_80_pixels(pattern: Grid | None = None) -> bool:
    chosen = pattern or all_pixel_pattern_10x8()
    colors = [color for row in chosen for color in row]
    return len(colors) == 80 and len(set(colors)) == 80 and all(is_active(color) for color in colors)


def colors_close(left: RGB, right: RGB, *, tolerance: int) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right))


def is_active(color: RGB) -> bool:
    return max(color) >= 24


def quantize_color(color: RGB, *, bucket: int) -> RGB:
    return tuple(min(255, round(channel / bucket) * bucket) for channel in color)  # type: ignore[return-value]
