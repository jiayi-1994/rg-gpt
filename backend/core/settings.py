"""Pure-env settings shim for the CI runner (no SQLite/DB).

The full app uses a SQLite-backed settings table with env fallback; on CI we
only ever read config from environment variables (GitHub Secrets), so this
drop-in reads os.environ directly. Lookups are case-insensitive so callers
querying `cloudmail_base_url` still match a `CLOUDMAIL_BASE_URL` secret on
case-sensitive Linux runners.
"""
from __future__ import annotations

import os
from typing import Iterable


class Settings:
    def get(self, key: str, default: str = "") -> str:
        for k in (key, key.upper(), key.lower()):
            v = os.getenv(k)
            if v is not None and v != "":
                return v
        return default

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = self.get(key, "1" if default else "0")
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def get_all(self) -> dict[str, str]:
        # No DB layer on CI; sub2api-led flow doesn't need a full dump.
        return {}

    # no-op writers (CI never persists settings)
    def set(self, key: str, value: str) -> None:  # noqa: D401
        pass

    def set_many(self, items: dict[str, str]) -> None:
        pass

    def delete_many(self, keys: Iterable[str]) -> None:
        pass


settings = Settings()
