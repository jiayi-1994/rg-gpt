"""CloudMail email service (dreamhunter2333/cloudflare_temp_email backend).

Implements the same duck-typed surface the registration engine and the OAuth
OTP adapter consume from :class:`MicrosoftEmailService`:

  - ``service_type.value``
  - ``claimed_email``
  - ``create_email() -> {"email": ...}``
  - ``get_verification_code(email, *, keyword, timeout, code_pattern,
        otp_sent_at, exclude_codes, **kwargs) -> str | None``

Auth is the cf_temp_email admin header ``x-admin-auth: {CLOUDMAIL_PASSWORD}``.
Config is read from settings (DB) with environment-variable fallback:

  - ``CLOUDMAIL_BASE_URL``   e.g. https://api.example.org  (``/admin`` suffix tolerated)
  - ``CLOUDMAIL_PASSWORD``   admin password
  - ``CLOUDMAIL_DOMAIN``     e.g. @edu.example.net (leading ``@`` tolerated)

Unlike the Microsoft variant there is no local mailbox pool: each
``create_email()`` mints a fresh random address on the cf_temp_email server,
and OTPs are read back over the admin REST API.  When ``fixed_email`` is given
(re-using an account during OAuth) the address is used as-is.
"""
from __future__ import annotations

import email as email_pkg
import html as html_lib
import logging
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from typing import Any

import requests

from backend.core.settings import settings

logger = logging.getLogger(__name__)

OTP_REQUEST_GRACE_SECONDS = 60  # tolerate clock drift between us and the server
DEFAULT_POLL_INTERVAL = 5.0

# OTP patterns, tuned for OpenAI/ChatGPT signup + login mails, then generic.
VERIFICATION_CODE_PATTERNS = (
    r"(?is)(?:temporary\s+(?:openai|chatgpt)\s+login\s+code(?:\s+is)?|"
    r"verification\s+code(?:\s+is)?|one[-\s]*time\s+(?:password|code)|"
    r"security\s+code|login\s+code(?:\s+is)?|code(?:\s+is)?|"
    r"验证码(?:为|是)?|校验码|动态码)\D{0,24}(\d{4,8})",
    r"\b(\d{6})\b",
)


@dataclass
class _ServiceType:
    value: str = "cloudmail"


class CloudMailEmailService:
    service_type = _ServiceType()

    def __init__(self, *, extra_config: dict[str, Any] | None = None) -> None:
        self._extra = dict(extra_config or {})
        self._lock = threading.Lock()
        self._claimed_email: str | None = None
        self._fixed_email = str(self._extra.get("fixed_email") or "").strip()

        self.base_url = _normalize_base_url(_cfg("CLOUDMAIL_BASE_URL"))
        self.admin_password = _cfg("CLOUDMAIL_PASSWORD")
        self.domain = _cfg("CLOUDMAIL_DOMAIN").lstrip("@").strip()
        self._poll_interval = float(settings.get_int("email_poll_interval_seconds", 5)) or DEFAULT_POLL_INTERVAL
        self._session = requests.Session()

    @property
    def claimed_email(self) -> str | None:
        return self._claimed_email

    # -- API expected by the registration engine / OAuth adapter -----------

    def create_email(self) -> dict[str, str]:
        with self._lock:
            if self._fixed_email:
                self._claimed_email = self._fixed_email
                return {"email": self._fixed_email}
            self._ensure_configured(require_domain=True)
            address = self._create_address(_random_prefix())
            self._claimed_email = address
            logger.info("[CloudMail] created temp email: %s", address)
            return {"email": address}

    def get_verification_code(
        self,
        email: str,
        *,
        keyword: str = "",
        timeout: int = 300,
        code_pattern: str | None = None,
        otp_sent_at: float | datetime | None = None,
        exclude_codes: set[str] | list[str] | tuple[str, ...] | None = None,
        **_kwargs: Any,
    ) -> str | None:
        self._ensure_configured(require_domain=False)
        target = str(email or "").strip().lower()
        if not target:
            return None

        since = _since_datetime(otp_sent_at)
        if since is not None:
            since = since - timedelta(seconds=OTP_REQUEST_GRACE_SECONDS)
        keyword_lower = (keyword or "").lower()
        excluded = {str(code or "").strip() for code in (exclude_codes or ()) if str(code or "").strip()}

        deadline = time.time() + max(1, int(timeout or 300))
        while time.time() < deadline:
            for mail in self._list_mails(target, size=20):
                if since is not None and mail["received_at"] is not None and mail["received_at"] < since:
                    continue
                haystack = "\n".join(
                    part for part in (mail["subject"], mail["text"], _html_to_text(mail["html"])) if part
                )
                if keyword_lower and keyword_lower not in haystack.lower():
                    continue
                code = _extract_otp(haystack, code_pattern)
                if code and code not in excluded:
                    logger.info("[CloudMail] OTP for %s: %s (from %s)", target, code, mail["sender"])
                    return code
            time.sleep(self._poll_interval)
        return None

    # -- HTTP helpers ------------------------------------------------------

    def _ensure_configured(self, *, require_domain: bool) -> None:
        if not self.base_url:
            raise RuntimeError("CloudMail 未配置: 缺少 CLOUDMAIL_BASE_URL")
        if not self.admin_password:
            raise RuntimeError("CloudMail 未配置: 缺少 CLOUDMAIL_PASSWORD")
        if require_domain and not self.domain:
            raise RuntimeError("CloudMail 未配置: 缺少 CLOUDMAIL_DOMAIN")

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-admin-auth": self.admin_password or ""}

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _create_address(self, prefix: str) -> str:
        resp = self._session.post(
            self._url("/admin/new_address"),
            headers=self._headers(),
            json={"name": prefix, "domain": self.domain, "enablePrefix": False},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"CloudMail 创建邮箱失败: HTTP {resp.status_code} {(resp.text or '')[:200]}")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"CloudMail 创建邮箱返回非 JSON: {(resp.text or '')[:200]}") from exc
        if not isinstance(data, dict) or "address" not in data:
            raise RuntimeError(
                "CloudMail 创建邮箱响应不像 cloudflare_temp_email"
                f" (收到 {data!r}); 请核对 CLOUDMAIL_BASE_URL 是否指向该后端"
            )
        address = str(data.get("address") or "").strip()
        if not address:
            raise RuntimeError(f"CloudMail 创建邮箱响应缺少 address: {data!r}")
        return address

    def _list_mails(self, email: str, *, size: int = 20) -> list[dict[str, Any]]:
        try:
            resp = self._session.get(
                self._url("/admin/mails"),
                headers=self._headers(),
                params={"limit": int(size), "offset": 0, "address": email},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CloudMail] list mails error for %s: %s", email, exc)
            return []
        if resp.status_code != 200:
            logger.warning("[CloudMail] list mails %s status=%s", email, resp.status_code)
            return []
        try:
            results = (resp.json() or {}).get("results") or []
        except Exception:  # noqa: BLE001
            return []

        out: list[dict[str, Any]] = []
        for row in results:
            row_addr = str(row.get("address") or "").strip().lower()
            if row_addr and row_addr != email:
                continue
            subject, text, html_body, from_addr, _to, _mid = _parse_mime(row.get("raw") or "")
            out.append(
                {
                    "id": row.get("id"),
                    "subject": subject,
                    "text": text,
                    "html": html_body,
                    "sender": str(row.get("source") or from_addr or ""),
                    "received_at": _parse_dt(row.get("created_at")),
                }
            )
        return out


# ---- module helpers -----------------------------------------------------------


def _cfg(key: str) -> str:
    return str(settings.get(key, "") or "").strip()


def _normalize_base_url(value: str | None) -> str:
    base_url = (value or "").strip().rstrip("/")
    if base_url.lower().endswith("/admin"):
        base_url = base_url[:-6].rstrip("/")
    return base_url


def _random_prefix() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "oai" + "".join(secrets.choice(alphabet) for _ in range(12))


def _since_datetime(value: float | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return str(value)


def _part_to_text(part: Any) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return str(part.get_payload())
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:  # noqa: BLE001
        try:
            return str(part.get_payload())
        except Exception:  # noqa: BLE001
            return ""


def _parse_mime(raw: str | None) -> tuple[str, str, str, str, str, str]:
    """Parse a MIME message -> (subject, text, html, from, to, message_id)."""
    if not raw:
        return "", "", "", "", "", ""
    try:
        msg = email_pkg.message_from_string(raw)
    except Exception:  # noqa: BLE001
        return "", str(raw), "", "", "", ""

    subject = _decode_mime_header(msg.get("Subject", ""))
    from_addr = _decode_mime_header(msg.get("From", ""))
    to_addr = _decode_mime_header(msg.get("To", ""))
    message_id = (msg.get("Message-ID") or "").strip()

    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            if ctype == "text/plain" and not text_body:
                text_body = _part_to_text(part)
            elif ctype == "text/html" and not html_body:
                html_body = _part_to_text(part)
    else:
        decoded = _part_to_text(msg)
        if msg.get_content_type() == "text/html":
            html_body = decoded
        else:
            text_body = decoded
    return subject, text_body, html_body, from_addr, to_addr, message_id


def _html_to_text(value: Any) -> str:
    content = str(value or "")
    if not content:
        return ""
    content = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", content)
    content = re.sub(r"(?is)<!--.*?-->", " ", content)
    content = re.sub(r"(?i)<br\s*/?>", "\n", content)
    content = re.sub(r"(?i)</(?:p|div|tr|table|h[1-6]|li|td|section|article)>", "\n", content)
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    content = html_lib.unescape(content)
    content = re.sub(r"[\t\r\f\v ]+", " ", content)
    content = re.sub(r"\n\s+", "\n", content)
    return content.strip()


def _extract_otp(text: str, pattern: str | None) -> str:
    if not text:
        return ""
    patterns: list[str] = []
    if pattern:
        patterns.append(pattern)
    patterns.extend(VERIFICATION_CODE_PATTERNS)
    for regex in patterns:
        match = re.search(regex, text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return ""
