from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageSequence


WIDTH = 32
HEIGHT = 8
ASSET_WIDTH = 10
CLOCK_WIDTH = 22


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
    return Image.new("RGB", (ASSET_WIDTH, HEIGHT), (0, 0, 0))


def load_asset(assets_dir: Path, name: str | None) -> AssetAnimation:
    if not name:
        return AssetAnimation((blank_asset(),), loop=True)
    candidate = (assets_dir / name).resolve()
    root = assets_dir.resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("asset path escapes assets_dir")
    with Image.open(candidate) as image:
        frames = tuple(_normalize_frame(frame) for frame in ImageSequence.Iterator(image))
        loop = image.info.get("loop") == 0
    return AssetAnimation(frames or (blank_asset(),), loop=loop)


def render_frame(asset: Image.Image, now: datetime) -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    canvas.paste(_normalize_frame(asset), (0, 0))
    _draw_clock(canvas, now)
    return canvas


def build_awtrix_payload(asset: Image.Image, now: datetime) -> str:
    frame = render_frame(asset, now)
    return json.dumps(
        {"draw": [{"db": [0, 0, WIDTH, HEIGHT, image_to_uint32_bitmap(frame)]}], "duration": 1},
        separators=(",", ":"),
    )


def image_to_uint32_bitmap(image: Image.Image) -> list[int]:
    rgb = image.convert("RGB")
    return [
        (red << 16) | (green << 8) | blue
        for red, green, blue in (rgb.getpixel((x, y)) for y in range(rgb.height) for x in range(rgb.width))
    ]


def _normalize_frame(frame: Image.Image) -> Image.Image:
    return frame.convert("RGBA").resize((ASSET_WIDTH, HEIGHT), Image.Resampling.NEAREST).convert("RGB")


def _draw_clock(canvas: Image.Image, now: datetime) -> None:
    text = now.strftime("%H:%M")
    colon_on = now.second % 2 == 0
    x = ASSET_WIDTH + 2
    y = 1
    for ch in text:
        if ch == ":":
            if colon_on:
                canvas.putpixel((x, y + 1), (255, 255, 255))
                canvas.putpixel((x, y + 3), (255, 255, 255))
            x += 2
            continue
        glyph = DIGITS[ch]
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    canvas.putpixel((x + col, y + row), (255, 255, 255))
        x += 4
