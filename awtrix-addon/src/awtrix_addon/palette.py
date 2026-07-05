from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any


RGB = tuple[int, int, int]
SCHEMA = "awtrix-addon-palettes"
VERSION = 1


@dataclass(frozen=True)
class PaletteSnapshot:
    time_color: RGB = (255, 255, 255)
    weekday_active_color: RGB = (255, 0, 0)
    weekday_inactive_color: RGB = (102, 102, 102)
    calendar_header_color: RGB = (255, 0, 0)
    calendar_body_color: RGB = (255, 255, 255)
    calendar_text_color: RGB = (0, 0, 0)


DEFAULT_PALETTE = PaletteSnapshot()


class PaletteStore:
    def __init__(self, path: Path, configured_prefixes: tuple[str, ...]):
        self.path = path
        self.configured_prefixes = tuple(configured_prefixes)
        self._configured = frozenset(configured_prefixes)
        self._lock = RLock()
        self._snapshots: dict[str, PaletteSnapshot] = {}
        self._load()

    def snapshot(self, prefix: str) -> PaletteSnapshot:
        with self._lock:
            return self._snapshots.get(prefix, DEFAULT_PALETTE)

    def snapshots(self) -> dict[str, PaletteSnapshot]:
        with self._lock:
            return dict(self._snapshots)

    def handle_settings(self, prefix: str, payload: str | bytes) -> bool:
        if prefix not in self._configured:
            return False
        try:
            raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            settings = json.loads(raw)
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            return False

        with self._lock:
            previous = dict(self._snapshots)
            current = self._snapshots.get(prefix, DEFAULT_PALETTE)
            try:
                snapshot = parse_settings_palette(settings, base=current)
            except (ValueError, TypeError):
                return False
            self._snapshots[prefix] = snapshot
            try:
                self._save_locked()
            except OSError:
                self._snapshots = previous
                return False
        return True

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or raw.get("version") != VERSION:
            return
        prefixes = raw.get("prefixes")
        if not isinstance(prefixes, dict):
            return

        loaded: dict[str, PaletteSnapshot] = {}
        for prefix, value in prefixes.items():
            if prefix not in self._configured or not isinstance(value, dict):
                continue
            try:
                loaded[prefix] = _snapshot_from_saved(value)
            except ValueError:
                continue
        self._snapshots = loaded

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA,
            "version": VERSION,
            "prefixes": {
                prefix: _snapshot_to_saved(snapshot)
                for prefix, snapshot in sorted(self._snapshots.items())
                if prefix in self._configured
            },
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False) as handle:
                temp_name = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            temp_name = None
            _fsync_parent(self.path.parent)
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass


def parse_settings_palette(settings: Any, *, base: PaletteSnapshot = DEFAULT_PALETTE) -> PaletteSnapshot:
    if not isinstance(settings, dict):
        raise ValueError("settings payload must be an object")

    tcol = _optional_color(settings, "TCOL", allow_time_sentinel=False)
    time_col = _optional_color(settings, "TIME_COL", allow_time_sentinel=True)
    if time_col == 0:
        time_color = tcol or base.time_color
    elif time_col is not None:
        time_color = time_col
    else:
        time_color = tcol or base.time_color

    return PaletteSnapshot(
        time_color=time_color,
        weekday_active_color=_optional_color(settings, "WDCA", allow_time_sentinel=False)
        or base.weekday_active_color,
        weekday_inactive_color=_optional_color(settings, "WDCI", allow_time_sentinel=False)
        or base.weekday_inactive_color,
        calendar_header_color=_optional_color(settings, "CHCOL", allow_time_sentinel=False)
        or base.calendar_header_color,
        calendar_body_color=_optional_color(settings, "CBCOL", allow_time_sentinel=False)
        or base.calendar_body_color,
        calendar_text_color=_optional_color(settings, "CTCOL", allow_time_sentinel=False)
        or base.calendar_text_color,
    )


def _optional_color(settings: dict[str, Any], key: str, *, allow_time_sentinel: bool) -> RGB | int | None:
    if key not in settings:
        return None
    value = settings[key]
    if allow_time_sentinel and isinstance(value, int) and not isinstance(value, bool) and value == 0:
        return 0
    return parse_color(value)


def parse_color(value: Any) -> RGB:
    if isinstance(value, list) and len(value) == 3:
        if all(isinstance(component, int) and not isinstance(component, bool) and 0 <= component <= 255 for component in value):
            return (value[0], value[1], value[2])
        raise ValueError("RGB array components must be 0..255 integers")
    if isinstance(value, str):
        candidate = value[1:] if value.startswith("#") else value
        if len(candidate) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in candidate):
            return (int(candidate[0:2], 16), int(candidate[2:4], 16), int(candidate[4:6], 16))
    raise ValueError("color must be [r,g,b], #RRGGBB, or RRGGBB")


def _snapshot_from_saved(value: dict[str, Any]) -> PaletteSnapshot:
    defaults = asdict(DEFAULT_PALETTE)
    parsed: dict[str, RGB] = {}
    for key in defaults:
        raw = value.get(key)
        if raw is None:
            parsed[key] = getattr(DEFAULT_PALETTE, key)
        else:
            parsed[key] = parse_color(raw)
    return PaletteSnapshot(**parsed)


def _snapshot_to_saved(snapshot: PaletteSnapshot) -> dict[str, str]:
    return {key: _rgb_hex(value) for key, value in asdict(snapshot).items()}


def _rgb_hex(value: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*value)


def _fsync_parent(parent: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
