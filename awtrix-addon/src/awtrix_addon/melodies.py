from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


MELODY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RTTTL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
RTTTL_NOTE_RE = re.compile(r"^(?:1|2|4|8|16|32)?(?:p|[acdfg]#?|[be])(?:\.?(?:4|5|6|7))?\.?$")
MAX_RTTTL_LENGTH = 4096
NAMESPACES = frozenset({"Default", "Personal"})
VALID_DURATIONS = frozenset({1, 2, 4, 8, 16, 32})
MIN_TEMPO = 25
MAX_TEMPO = 900


class MelodyError(ValueError):
    pass


class MelodyNotFound(MelodyError):
    pass


@dataclass(frozen=True)
class MelodyReference:
    namespace: str
    name: str


class MelodyLibrary:
    """Resolve packaged Default and persistent Personal RTTTL melodies."""

    def __init__(self, data_dir: Path, *, default_root: Path | None = None):
        self.default_root = default_root or Path(__file__).with_name("library") / "melodies" / "Default"
        self.personal_root = data_dir / "library" / "melodies" / "Personal"

    def resolve(self, value: str) -> str:
        reference = self.parse_reference(value)
        root = self.default_root if reference.namespace == "Default" else self.personal_root
        path = root / f"{reference.name}.rtttl"
        try:
            contents = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise MelodyNotFound(reference_name(reference)) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise MelodyError("melody could not be read") from exc
        return validate_rtttl(contents)

    def list_names(self) -> list[str]:
        entries: list[str] = []
        for namespace, root in (("Default", self.default_root), ("Personal", self.personal_root)):
            if not root.is_dir():
                continue
            entries.extend(f"{namespace}/{path.stem}" for path in sorted(root.glob("*.rtttl")) if MELODY_NAME_RE.fullmatch(path.stem))
        return entries

    @staticmethod
    def parse_reference(value: str) -> MelodyReference:
        if not isinstance(value, str):
            raise MelodyError("melody must be a string")
        namespace, separator, name = value.partition("/")
        if not separator or "/" in name or namespace not in NAMESPACES or not MELODY_NAME_RE.fullmatch(name):
            raise MelodyError("melody must use Default/<name> or Personal/<name>")
        return MelodyReference(namespace, name)


def reference_name(reference: MelodyReference) -> str:
    return f"{reference.namespace}/{reference.name}"


def validate_rtttl(value: str) -> str:
    if not isinstance(value, str):
        raise MelodyError("RTTTL must be a string")
    rtttl = value.strip()
    if not rtttl or len(rtttl) > MAX_RTTTL_LENGTH or "\x00" in rtttl or "\n" in rtttl or "\r" in rtttl:
        raise MelodyError("invalid RTTTL")
    name, separator, remainder = rtttl.partition(":")
    defaults, separator2, notes = remainder.partition(":")
    if not separator or not separator2 or not RTTTL_NAME_RE.fullmatch(name) or not notes:
        raise MelodyError("invalid RTTTL")
    values = {}
    default_items = defaults.split(",")
    if len(default_items) != 3:
        raise MelodyError("invalid RTTTL")
    for item in default_items:
        key, equals, setting = item.partition("=")
        if not equals or key not in {"d", "o", "b"} or not setting.isdigit() or key in values:
            raise MelodyError("invalid RTTTL")
        values[key] = int(setting)
    if set(values) != {"d", "o", "b"}:
        raise MelodyError("invalid RTTTL")
    if values["d"] not in VALID_DURATIONS or not 4 <= values["o"] <= 7 or not MIN_TEMPO <= values["b"] <= MAX_TEMPO:
        raise MelodyError("invalid RTTTL")
    if any(not RTTTL_NOTE_RE.fullmatch(note) for note in notes.split(",")):
        raise MelodyError("invalid RTTTL")
    return rtttl
