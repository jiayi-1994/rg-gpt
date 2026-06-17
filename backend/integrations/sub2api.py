"""Configurable sub2api client used as the OpenAI account pool backend.

Auth supports two modes, selected by which config is present:

  * **admin login** (preferred): ``SUB2API_EMAIL`` + ``SUB2API_PASSWORD`` ->
    ``POST {login_path}`` returns a Bearer ``access_token`` (cached, auto
    re-login on 401).  Matches the sub2api gateway admin panel auth.
  * **api key** (fallback): ``SUB2API_API_KEY`` sent as ``x-api-key``.

Base URL accepts ``SUB2API_URL`` / ``SUB2API_BASE_URL`` (the gateway root; the
``/api/v1/...`` admin paths are appended).  Optional ``SUB2API_GROUP`` (group
name or id) is resolved to an id so synced accounts can be bound to a group.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import requests

from backend.core.settings import settings


class Sub2ApiNotConfigured(RuntimeError):
    pass


class Sub2ApiClient:
    def __init__(self) -> None:
        self.base_url = _setting_multi(
            ("sub2api_base_url", "SUB2API_BASE_URL", "sub2api_url", "SUB2API_URL")
        ).rstrip("/")
        self.email = _setting_multi(("sub2api_email", "SUB2API_EMAIL"))
        self.password = _setting_multi(("sub2api_password", "SUB2API_PASSWORD"))
        self.api_key = _setting_multi(("sub2api_api_key", "SUB2API_API_KEY"))
        self.login_path = _setting(
            "sub2api_login_path", "SUB2API_LOGIN_PATH", "/api/v1/auth/login"
        )
        self.account_import_path = _setting(
            "sub2api_openai_import_path",
            "SUB2API_OPENAI_IMPORT_PATH",
            "/api/v1/admin/accounts/data",
        )
        self.openai_import_path = self.account_import_path
        self.account_export_path = _setting(
            "sub2api_account_export_path",
            "SUB2API_ACCOUNT_EXPORT_PATH",
            "/api/v1/admin/accounts/data",
        )
        self.account_list_path = _setting(
            "sub2api_account_list_path",
            "SUB2API_ACCOUNT_LIST_PATH",
            "/api/v1/admin/accounts",
        )
        self.account_status_path = _setting(
            "sub2api_account_status_path",
            "SUB2API_ACCOUNT_STATUS_PATH",
            "/api/v1/admin/accounts/{account_id}",
        )
        self.account_update_path = _setting(
            "sub2api_account_update_path",
            "SUB2API_ACCOUNT_UPDATE_PATH",
            "/api/v1/admin/accounts/{account_id}",
        )
        self.account_bulk_update_path = _setting(
            "sub2api_account_bulk_update_path",
            "SUB2API_ACCOUNT_BULK_UPDATE_PATH",
            "/api/v1/admin/accounts/bulk-update",
        )
        self.group_list_path = _setting(
            "sub2api_group_list_path",
            "SUB2API_GROUP_LIST_PATH",
            "/api/v1/admin/groups/all",
        )
        self.openai_generate_auth_url_path = _setting(
            "sub2api_openai_generate_auth_url_path",
            "SUB2API_OPENAI_GENERATE_AUTH_URL_PATH",
            "/api/v1/admin/openai/generate-auth-url",
        )
        self.openai_create_from_oauth_path = _setting(
            "sub2api_openai_create_from_oauth_path",
            "SUB2API_OPENAI_CREATE_FROM_OAUTH_PATH",
            "/api/v1/admin/openai/create-from-oauth",
        )
        # Group to bind synced accounts to. Accepts a numeric id or a name
        # (resolved lazily via the groups list).
        self.sold_group_spec = _setting_multi(
            ("sub2api_group", "SUB2API_GROUP", "sub2api_sold_group_id", "SUB2API_SOLD_GROUP_ID")
        )
        self.account_concurrency = _setting_int_multi(
            ("sub2api_account_concurrency", "SUB2API_CONCURRENCY"), 10
        )
        self.timeout = _setting_int("sub2api_timeout_seconds", "SUB2API_TIMEOUT_SECONDS", 30)

        self._token: str = ""
        self._resolved_group_id: int | None = None

    # -- config / auth -----------------------------------------------------

    def ensure_configured(self) -> None:
        if not self.base_url:
            raise Sub2ApiNotConfigured("sub2api base_url is not configured")
        if not ((self.email and self.password) or self.api_key):
            raise Sub2ApiNotConfigured(
                "sub2api auth is not configured (need SUB2API_EMAIL+SUB2API_PASSWORD or SUB2API_API_KEY)"
            )

    def _login(self) -> str:
        if not (self.email and self.password):
            return ""
        url = _join_url(self.base_url, self.login_path)
        resp = requests.post(
            url,
            json={"email": self.email, "password": self.password},
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"sub2api login failed: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            body = resp.json()
        except Exception as exc:
            raise RuntimeError("sub2api login returned non-json response") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError(f"sub2api login: unexpected response {str(body)[:200]}")
        if data.get("requires_2fa"):
            raise RuntimeError("sub2api login requires 2FA; automatic sync not supported")
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("sub2api login succeeded but returned no access_token")
        return token

    def _auth_headers(self) -> dict[str, str]:
        if self.email and self.password:
            if not self._token:
                self._token = self._login()
            return {"Authorization": f"Bearer {self._token}"}
        if self.api_key:
            return {"x-api-key": self.api_key}
        return {}

    # -- account ops -------------------------------------------------------

    def import_account_data(self, payload: dict[str, Any], *, skip_default_group_bind: bool = True) -> dict[str, Any]:
        self.ensure_configured()
        return self._request(
            "POST",
            self.account_import_path,
            json={
                "data": _account_data_payload(payload),
                "skip_default_group_bind": bool(skip_default_group_bind),
            },
        )

    def import_openai_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.import_account_data(payload)

    def upsert_openai_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.import_account_data(payload)

    def export_account_data(
        self,
        *,
        ids: list[int] | None = None,
        include_proxies: bool = False,
        platform: str = "",
        account_type: str = "",
        status: str = "",
        search: str = "",
    ) -> dict[str, Any]:
        self.ensure_configured()
        params: dict[str, Any] = {"include_proxies": "true" if include_proxies else "false"}
        if ids:
            params["ids"] = ",".join(str(int(item)) for item in ids if int(item) > 0)
        if platform:
            params["platform"] = platform
        if account_type:
            params["type"] = account_type
        if status:
            params["status"] = status
        if search:
            params["search"] = search
        return self._request("GET", self.account_export_path, params=params)

    def list_accounts(
        self,
        *,
        platform: str = "",
        account_type: str = "",
        status: str = "",
        search: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        self.ensure_configured()
        params: dict[str, Any] = {"page": max(1, int(page)), "page_size": max(1, min(int(page_size), 1000))}
        if platform:
            params["platform"] = platform
        if account_type:
            params["type"] = account_type
        if status:
            params["status"] = status
        if search:
            params["search"] = search
        return self._request("GET", self.account_list_path, params=params)

    def update_openai_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_configured()
        path = self.account_update_path.format(account_id=account_id)
        return self._request("PUT", path, json=payload)

    def clear_openai_account_error(self, account_id: str) -> dict[str, Any]:
        self.ensure_configured()
        path = f"{self.account_status_path.format(account_id=account_id).rstrip('/')}/clear-error"
        return self._request("POST", path)

    def set_openai_account_schedulable(self, account_id: str, schedulable: bool) -> dict[str, Any]:
        self.ensure_configured()
        path = f"{self.account_status_path.format(account_id=account_id).rstrip('/')}/schedulable"
        return self._request("POST", path, json={"schedulable": bool(schedulable)})

    def reset_openai_account_status(self, account_id: str) -> dict[str, Any]:
        self.clear_openai_account_error(account_id)
        return self.set_openai_account_schedulable(account_id, True)

    def move_openai_account_to_group(self, account_id: str, group_id: int) -> dict[str, Any]:
        if not int(group_id or 0):
            return {}
        return self.update_openai_account(
            account_id,
            {
                "group_ids": [int(group_id)],
                "confirm_mixed_channel_risk": True,
            },
        )

    def move_openai_accounts_to_group(self, account_ids: list[str], group_id: int) -> dict[str, Any]:
        ids = [int(item) for item in account_ids if str(item or "").strip().isdigit() and int(item) > 0]
        if not ids or not int(group_id or 0):
            return {}
        return self._request(
            "POST",
            self.account_bulk_update_path,
            json={
                "account_ids": ids,
                "group_ids": [int(group_id)],
                "confirm_mixed_channel_risk": True,
            },
        )

    def get_openai_account_status(self, account_id: str) -> dict[str, Any]:
        self.ensure_configured()
        path = self.account_status_path.format(account_id=account_id)
        return self._request("GET", path)

    # -- OpenAI OAuth (sub2api 主导：生成 URL + 回写 code 由 sub2api exchange) ----

    def generate_openai_auth_url(self, *, proxy_id: int | None = None, redirect_uri: str = "") -> dict[str, Any]:
        """POST /api/v1/admin/openai/generate-auth-url -> {auth_url, session_id}.

        PKCE 的 state/code_challenge/code_verifier 全在 sub2api 侧 session 里。
        """
        self.ensure_configured()
        body: dict[str, Any] = {}
        if proxy_id:
            body["proxy_id"] = int(proxy_id)
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
        resp = self._request("POST", self.openai_generate_auth_url_path, json=body)
        data = resp.get("data") if isinstance(resp, dict) else None
        return data if isinstance(data, dict) else (resp if isinstance(resp, dict) else {})

    def create_openai_account_from_oauth(
        self,
        *,
        session_id: str,
        code: str,
        state: str,
        group_ids: list[int] | None = None,
        name: str = "",
        redirect_uri: str = "",
        proxy_id: int | None = None,
        concurrency: int | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        """POST /api/v1/admin/openai/create-from-oauth.

        sub2api 用 session 里的 code_verifier 自行 exchange code -> RT，并直接建号、
        绑定 group_ids。一步到位（RT 进 sub2api 账号库，不经过本地）。
        """
        self.ensure_configured()
        body: dict[str, Any] = {
            "session_id": session_id,
            "code": code,
            "state": state,
            "concurrency": int(concurrency if concurrency is not None else self.account_concurrency),
            "priority": int(priority or 0),
            "group_ids": [int(g) for g in (group_ids or []) if int(g or 0) > 0],
        }
        if name:
            body["name"] = name
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
        if proxy_id:
            body["proxy_id"] = int(proxy_id)
        resp = self._request("POST", self.openai_create_from_oauth_path, json=body)
        data = resp.get("data") if isinstance(resp, dict) else None
        return data if isinstance(data, dict) else (resp if isinstance(resp, dict) else {})

    # -- groups ------------------------------------------------------------

    def resolve_sold_group_id(self) -> int:
        """Resolve `SUB2API_GROUP` (id or name) to a numeric group id, or 0.

        The groups endpoint may return technically-invalid JSON (free-text
        descriptions with stray escapes), so the name lookup scans the raw
        text instead of full JSON parsing.
        """
        spec = str(self.sold_group_spec or "").strip()
        if not spec:
            return 0
        if spec.lstrip("+-").isdigit():
            return int(spec)
        if self._resolved_group_id is not None:
            return self._resolved_group_id

        self.ensure_configured()
        url = _join_url(self.base_url, self.group_list_path)
        resp = requests.get(
            url, headers=self._auth_headers(), params={"platform": "openai"}, timeout=self.timeout
        )
        if resp.status_code == 401 and self.email and self.password:
            self._token = ""
            resp = requests.get(
                url, headers=self._auth_headers(), params={"platform": "openai"}, timeout=self.timeout
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"sub2api groups/all status={resp.status_code}: {resp.text[:200]}")
        group_id = _scan_group_id_by_name(resp.text, spec)
        self._resolved_group_id = group_id
        return group_id

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        return self._request_once(method, path, _retry_on_401=True, **kwargs)

    def _request_once(self, method: str, path: str, *, _retry_on_401: bool, **kwargs) -> dict[str, Any]:
        url = _join_url(self.base_url, path)
        headers = dict(kwargs.pop("headers", {}) or {})
        for key, value in self._auth_headers().items():
            headers.setdefault(key, value)
        resp = requests.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        if resp.status_code == 401 and _retry_on_401 and self.email and self.password:
            self._token = ""  # token likely expired; force re-login and retry once
            return self._request_once(method, path, _retry_on_401=False, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"sub2api {method} {path} status={resp.status_code}: {resp.text[:300]}")
        if not resp.text.strip():
            return {}
        try:
            body = resp.json()
        except Exception as exc:
            raise RuntimeError(f"sub2api {method} {path} returned non-json response") from exc
        return body if isinstance(body, dict) else {"data": body}


def get_sub2api_client() -> Sub2ApiClient:
    return Sub2ApiClient()


def _account_data_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "exported_at": str(payload.get("exported_at") or ""),
        "proxies": list(payload.get("proxies") or []),
        "accounts": list(payload.get("accounts") or []),
    }


def _scan_group_id_by_name(raw: str, name: str) -> int:
    """Find the id of the group whose `name` equals `name` (case-insensitive).

    Tolerant of invalid JSON escapes elsewhere in the payload by scanning for
    `"id":<n>,"name":"<name>"` pairs directly.
    """
    text = str(raw or "")
    target = name.strip().lower()
    i = 0
    while True:
        k = text.find('"id":', i)
        if k < 0:
            return 0
        j = k + 5
        num = ""
        while j < len(text) and text[j].isdigit():
            num += text[j]
            j += 1
        nk = text.find('"name":"', j)
        if 0 <= nk <= j + 3:
            s = nk + 8
            value = ""
            while s < len(text) and text[s] != '"':
                value += text[s]
                s += 1
            if num and value.strip().lower() == target:
                try:
                    return int(num)
                except ValueError:
                    return 0
        i = j


def _setting(key: str, env_key: str, default: str = "") -> str:
    return str(settings.get(key, settings.get(env_key, default)) or "").strip()


def _setting_multi(keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = str(settings.get(key, "") or "").strip()
        if value:
            return value
    return default


def _setting_int(key: str, env_key: str, default: int) -> int:
    try:
        return int(_setting(key, env_key, str(default)))
    except Exception:
        return default


def _setting_int_multi(keys: tuple[str, ...], default: int) -> int:
    raw = _setting_multi(keys, str(default))
    try:
        return int(raw)
    except Exception:
        return default


def _join_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/") + "/"
    suffix = str(path or "").lstrip("/")
    return urljoin(base, suffix)
