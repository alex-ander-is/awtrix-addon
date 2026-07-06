from __future__ import annotations

import json
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageSequence

from .palette import DEFAULT_PALETTE, PaletteSnapshot


WIDTH = 32
HEIGHT = 8
ASSET_X = 0
ASSET_Y = 0
CLOCK_WIDTH = 22
HOUR_TENS_X = 12
HOUR_ONES_X = 16
COLON_X = 20
MINUTE_TENS_X = 22
MINUTE_ONES_X = 26
CLOCK_X = HOUR_TENS_X
CLOCK_Y = 1
WEEKBAR_X = 10
WEEKBAR_Y = 7
WEEKBAR_BAR_WIDTH = 2
WEEKBAR_BAR_STRIDE = 3


DIGITS: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


@dataclass(frozen=True)
class AssetAnimation:
    frames: tuple[Image.Image, ...]
    loop: bool = True

    def frame_at(self, index: int) -> Image.Image:
        if not self.frames:
            return blank_asset()
        if self.loop:
            return self.frames[index % len(self.frames)]
        return self.frames[min(index, len(self.frames) - 1)]


def blank_asset() -> Image.Image:
    return Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))


def load_asset(assets_dir: Path, name: str | None) -> AssetAnimation:
    if not name:
        return AssetAnimation((blank_asset(),), loop=True)
    candidate = (assets_dir / name).resolve()
    root = assets_dir.resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("asset path escapes assets_dir")
    with Image.open(candidate) as image:
        return _load_animation(image)


def load_asset_bytes(data: bytes) -> AssetAnimation:
    with Image.open(BytesIO(data)) as image:
        return _load_animation(image)


def render_frame(
    asset: Image.Image,
    now: datetime,
    *,
    weekdays: bool = True,
    palette: PaletteSnapshot = DEFAULT_PALETTE,
) -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    _draw_clock(canvas, now, palette.time_color)
    if weekdays:
        _draw_weekbar(canvas, now, palette)
    _composite_asset(canvas, asset)
    return canvas


def build_awtrix_payload(
    asset: Image.Image,
    now: datetime,
    *,
    weekdays: bool = True,
    palette: PaletteSnapshot = DEFAULT_PALETTE,
    duration: int = 1,
) -> str:
    frame = render_frame(asset, now, weekdays=weekdays, palette=palette)
    return json.dumps(
        {"draw": [{"db": [0, 0, WIDTH, HEIGHT, image_to_uint32_bitmap(frame)]}], "duration": duration},
        separators=(",", ":"),
    )


def image_to_uint32_bitmap(image: Image.Image) -> list[int]:
    rgb = image.convert("RGB")
    return [
        (red << 16) | (green << 8) | blue
        for red, green, blue in (rgb.getpixel((x, y)) for y in range(rgb.height) for x in range(rgb.width))
    ]


def _prepare_frame(frame: Image.Image) -> Image.Image:
    return frame.convert("RGBA")


def _composite_asset(canvas: Image.Image, asset: Image.Image) -> None:
    frame = _prepare_frame(asset)
    visible_width = min(frame.width, WIDTH - ASSET_X)
    visible_height = min(frame.height, HEIGHT - ASSET_Y)
    if visible_width <= 0 or visible_height <= 0:
        return
    base = canvas.convert("RGBA")
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    overlay.alpha_composite(frame.crop((0, 0, visible_width, visible_height)), (ASSET_X, ASSET_Y))
    canvas.paste(Image.alpha_composite(base, overlay).convert("RGB"))


def _load_animation(image: Image.Image) -> AssetAnimation:
    frames = tuple(_prepare_frame(frame) for frame in ImageSequence.Iterator(image))
    loop = image.info.get("loop") == 0
    return AssetAnimation(frames or (blank_asset(),), loop=loop)


def _draw_clock(canvas: Image.Image, now: datetime, color: tuple[int, int, int]) -> None:
    colon_on = now.second % 2 == 0
    y = CLOCK_Y
    text = now.strftime("%H%M")
    for ch, x in zip(text, (HOUR_TENS_X, HOUR_ONES_X, MINUTE_TENS_X, MINUTE_ONES_X)):
        _draw_digit(canvas, ch, x, y, color)
    if colon_on:
        canvas.putpixel((COLON_X, y + 1), color)
        canvas.putpixel((COLON_X, y + 3), color)


def _draw_digit(canvas: Image.Image, ch: str, x: int, y: int, color: tuple[int, int, int]) -> None:
    glyph = DIGITS[ch]
    for row, bits in enumerate(glyph):
        for col, bit in enumerate(bits):
            if bit == "1":
                canvas.putpixel((x + col, y + row), color)


def _draw_weekbar(canvas: Image.Image, now: datetime, palette: PaletteSnapshot) -> None:
    active = now.date().weekday()
    for index in range(7):
        color = palette.weekday_active_color if index == active else palette.weekday_inactive_color
        x = WEEKBAR_X + index * WEEKBAR_BAR_STRIDE
        for offset in range(WEEKBAR_BAR_WIDTH):
            canvas.putpixel((x + offset, WEEKBAR_Y), color)
