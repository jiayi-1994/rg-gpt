from __future__ import annotations

import json
from typing import Optional
from urllib.parse import unquote, urlsplit, urlunsplit

_TRUTHY_CONFIG_VALUES = {"1", "true", "yes", "on", "enabled"}


def _is_auth_socks_proxy(scheme: str, username: str, password: str) -> bool:
    normalized = (scheme or "").lower()
    return normalized in {"socks5", "socks5h"} and bool(username or password)


def is_truthy_config_value(value) -> bool:
    return str(value or "").strip().lower() in _TRUTHY_CONFIG_VALUES


def is_authenticated_socks5_proxy(proxy_url: Optional[str]) -> bool:
    if not proxy_url:
        return False

    value = str(proxy_url).strip()
    if not value:
        return False

    if value.startswith("{"):
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                server = str(data.get("server") or "").strip()
                if not server:
                    return False
                scheme = (urlsplit(server).scheme or "").lower()
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "").strip()
                return _is_auth_socks_proxy(scheme, username, password)
        except Exception:
            return False

    parts = urlsplit(value)
    return _is_auth_socks_proxy(
        parts.scheme or "",
        unquote(parts.username or ""),
        unquote(parts.password or ""),
    )


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """将 socks5:// 规范化为 socks5h://，避免本地 DNS 泄漏。"""
    if proxy_url is None:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None

    parts = urlsplit(value)
    if (parts.scheme or "").lower() == "socks5":
        parts = parts._replace(scheme="socks5h")
        return urlunsplit(parts)
    return value


def get_default_proxy_url(extra: Optional[dict] = None) -> Optional[str]:
    """Read the global default proxy from settings; returns None when disabled."""
    source = extra
    if source is None:
        try:
            from backend.core.settings import settings

            source = settings.get_all()
        except Exception:
            source = {}

    if not is_truthy_config_value((source or {}).get("default_proxy_enabled")):
        return None
    return normalize_proxy_url((source or {}).get("default_proxy_url"))


def resolve_effective_proxy(
    explicit_proxy: Optional[str] = None,
    *,
    extra: Optional[dict] = None,
    allow_default: bool = True,
) -> Optional[str]:
    explicit = normalize_proxy_url(explicit_proxy)
    if explicit:
        return explicit
    if allow_default:
        return get_default_proxy_url(extra)
    return None


def build_requests_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None
    normalized_proxy = proxy_url
    if proxy_url.startswith("socks5://"):
        normalized_proxy = "socks5h://" + proxy_url[len("socks5://"):]
    return {"http": normalized_proxy, "https": normalized_proxy}


def build_playwright_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname or parts.port is None:
        server = value
        if server.startswith("socks5h://"):
            server = "socks5://" + server[len("socks5h://") :]
        return {"server": server}

    scheme = (parts.scheme or "").lower()
    if _is_auth_socks_proxy(scheme, parts.username or "", parts.password or ""):
        return None
    if scheme == "socks5h":
        scheme = "socks5"

    config = {"server": f"{scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        config["username"] = unquote(parts.username)
    if parts.password:
        config["password"] = unquote(parts.password)
    return config
