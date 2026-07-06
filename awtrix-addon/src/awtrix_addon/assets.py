from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .renderer import AssetAnimation, load_asset


ASSET_NAME_RE = re.compile(r"^(?!\.)(?=.{5,68}$)[\w.-]+\.(?:png|gif)$", re.UNICODE)


class AssetError(ValueError):
    pass


class AssetNotFound(AssetError):
    pass


@dataclass(frozen=True)
class AssetReference:
    namespace: str
    filename: str


class AssetLibrary:
    """Resolve packaged Default PNG/GIF assets."""

    def __init__(self, *, default_root: Path | None = None):
        self.default_root = default_root or Path(__file__).with_name("library") / "assets" / "Default"

    def resolve(self, value: str) -> AssetAnimation:
        reference = self.parse_reference(value)
        try:
            return load_asset(self.default_root, reference.filename)
        except FileNotFoundError as exc:
            raise AssetNotFound(reference_name(reference)) from exc
        except (ValueError, OSError) as exc:
            raise AssetError("asset could not be loaded") from exc

    @staticmethod
    def parse_reference(value: str) -> AssetReference:
        namespace, separator, filename = value.partition("/")
        if not separator or "/" in filename or "\\" in filename or namespace != "Default" or not ASSET_NAME_RE.fullmatch(filename):
            raise AssetError("asset must use Default/<file>.png or Default/<file>.gif")
        return AssetReference(namespace, filename)


def reference_name(reference: AssetReference) -> str:
    return f"{reference.namespace}/{reference.filename}"
