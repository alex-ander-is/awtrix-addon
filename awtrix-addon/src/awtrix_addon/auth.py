from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AuthManager:
    data_dir: Path
    option_token: str | None = None

    @property
    def token_path(self) -> Path:
        return self.data_dir / "auth.json"

    def active_token(self) -> str:
        if self.option_token:
            return self.option_token
        return self._load_or_create_generated()

    def verify(self, token: str) -> bool:
        return secrets.compare_digest(token, self.active_token())

    def regenerate(self) -> str:
        if self.option_token:
            raise TokenManagedByOptions
        token = self._new_token()
        self._write_generated(token)
        return token

    def _load_or_create_generated(self) -> str:
        try:
            raw = json.loads(self.token_path.read_text(encoding="utf-8"))
            token = raw.get("token")
            if isinstance(token, str) and token:
                return token
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        token = self._new_token()
        self._write_generated(token)
        return token

    def _write_generated(self, token: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"token": token}, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.token_path)
        os.chmod(self.token_path, 0o600)

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(32)


class TokenManagedByOptions(Exception):
    pass
