#!/usr/bin/env python3
from __future__ import annotations

import compileall
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_IMPORTS = {
    "aiohttp": "aiohttp>=3.9",
    "PIL": "Pillow>=10",
    "yaml": "PyYAML>=6",
}
OPTIONAL_RUNTIME_IMPORTS = {
    "paho.mqtt.client": "paho-mqtt>=2",
}


def missing_imports(imports: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for module, package in imports.items():
        try:
            found = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            found = None
        if found is None:
            missing.append(package)
    return missing


def main() -> int:
    required_missing = missing_imports(REQUIRED_IMPORTS)
    if required_missing:
        print("Missing dependencies: " + ", ".join(required_missing), file=sys.stderr)
        print("Install them with:", file=sys.stderr)
        print("  python3 -m venv .venv", file=sys.stderr)
        print("  . .venv/bin/activate", file=sys.stderr)
        print("  python3 -m pip install -e '.[test]'", file=sys.stderr)
        return 2

    optional_missing = missing_imports(OPTIONAL_RUNTIME_IMPORTS)
    if optional_missing:
        print(
            "Note: live MQTT runtime dependency not installed: " + ", ".join(optional_missing),
            file=sys.stderr,
        )

    sys.path.insert(0, str(ROOT / "src"))
    with tempfile.TemporaryDirectory(prefix="awtrix-smoke-pyc-") as pycache:
        sys.pycache_prefix = pycache
        if not compileall.compile_dir(str(ROOT / "src"), quiet=1):
            return 1
        if not compileall.compile_dir(str(ROOT / "tests"), quiet=1):
            return 1
        if not compileall.compile_dir(str(ROOT / "scripts"), quiet=1):
            return 1

        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
