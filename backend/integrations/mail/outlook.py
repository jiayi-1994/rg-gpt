"""Outlook / Hotmail personal-account email service via Microsoft OAuth2.

Personal Microsoft accounts (``@outlook.com`` / ``@hotmail.com`` / ``@live.com``)
have **basic-auth IMAP and ROPC both disabled** since Sept 2024, so an
``email----password`` pair alone cannot read the mailbox:

  - IMAP basic auth   -> ``AUTHENTICATE failed``
  - ROPC (grant_type=password) -> ``AADSTS9001023`` (blocked on /consumers)

Instead we read the mailbox with an OAuth2 **access token** over IMAP XOAUTH2.
The long-lived **refresh_token** is minted once per account via the *device-code*
flow (see ``outlook/bootstrap.py``) — the human signs in from a normal browser,
never from a datacenter/CI IP, which avoids the "unusual sign-in" locks that
bulk Outlook accounts hit when a headless login is attempted.

Account line format (``OUTLOOK_ACCOUNTS`` env — newline or ``;`` separated — or a
file at ``OUTLOOK_ACCOUNTS_FILE`` / ``outlook/accounts.txt``)::

    email----password----refresh_token[----client_id]

``password`` is kept only for reference / re-bootstrap; the ``refresh_token`` is
what actually reads mail. ``client_id`` defaults to the Thunderbird public
client, which personal MSA accounts accept for the IMAP scope.

Duck-typed surface consumed by the registration engine / OAuth adapter (same as
:class:`CloudMailEmailService`)::

    service_type.value
    claimed_email
    create_email() -> {"email": ...}
    get_verification_code(email, *, keyword, timeout, code_pattern,
                          otp_sent_at, exclude_codes, **kwargs) -> str | None

Unlike CloudMail there is **no infinite address minting**: each personal mailbox
can back exactly ONE ChatGPT signup, so ``create_email()`` *claims* a distinct
unused account from the finite pool (recorded in a local ``.used`` marker so
re-runs and same-process parallel signups never reuse one).
"""
from __future__ import annotations

import base64
import email as email_pkg
import html as html_lib
import imaplib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from typing import Any, Callable

import requests

from backend.core.settings import settings

logger = logging.getLogger(__name__)

# Thunderbird's registered public client — personal MSA accounts accept it for
# the IMAP scope + device-code flow. No client secret (public client).
THUNDERBIRD_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
# Personal accounts live under /consumers; /common and /organizations reject them.
TOKEN_ENDPOINT = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
DEVICECODE_ENDPOINT = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993
# Folders OpenAI OTP mail can land in on a fresh mailbox.
IMAP_FOLDERS = ("INBOX", "Junk")

OTP_REQUEST_GRACE_SECONDS = 60
DEFAULT_POLL_INTERVAL = 5.0
ACCESS_TOKEN_SKEW_SECONDS = 60  # refresh a bit before actual expiry

VERIFICATION_CODE_PATTERNS = (
    r"(?is)(?:temporary\s+(?:openai|chatgpt)\s+login\s+code(?:\s+is)?|"
    r"verification\s+code(?:\s+is)?|one[-\s]*time\s+(?:password|code)|"
    r"security\s+code|login\s+code(?:\s+is)?|code(?:\s+is)?|"
    r"验证码(?:为|是)?|校验码|动态码)\D{0,24}(\d{4,8})",
    r"\b(\d{6})\b",
)


# ---- token helpers (used by bootstrap CLI + the service) ----------------------


class OutlookAuthError(RuntimeError):
    """Raised when a device-code login or refresh cannot complete."""


def request_device_code(client_id: str = THUNDERBIRD_CLIENT_ID, scope: str = IMAP_SCOPE) -> dict[str, Any]:
    resp = requests.post(DEVICECODE_ENDPOINT, data={"client_id": client_id, "scope": scope}, timeout=30)
    data = resp.json()
    if "device_code" not in data:
        raise OutlookAuthError(f"device code request failed: {data.get('error')} {data.get('error_description')}")
    return data


def poll_device_token(
    device_code: str,
    *,
    client_id: str = THUNDERBIRD_CLIENT_ID,
    interval: int = 5,
    expires_in: int = 900,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll the token endpoint until the user completes the device-code login."""
    deadline = time.time() + max(30, int(expires_in or 900))
    wait = max(1, int(interval or 5))
    while time.time() < deadline:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                  "client_id": client_id, "device_code": device_code},
            timeout=30,
        )
        data = resp.json()
        if "access_token" in data:
            return data
        err = data.get("error")
        if err == "authorization_pending":
            sleep(wait)
            continue
        if err == "slow_down":
            wait += 5
            sleep(wait)
            continue
        raise OutlookAuthError(f"device login failed: {err} {data.get('error_description')}")
    raise OutlookAuthError("device login timed out (code expired before sign-in)")


def device_code_login(
    email: str,
    *,
    client_id: str = THUNDERBIRD_CLIENT_ID,
    on_prompt: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Full device-code login. ``on_prompt`` receives the human sign-in message.

    Returns the raw token dict (contains ``refresh_token`` + ``access_token``).
    """
    dc = request_device_code(client_id)
    msg = dc.get("message") or (
        f"Open {dc.get('verification_uri')} and enter code {dc.get('user_code')} "
        f"(sign in as {email})"
    )
    (on_prompt or logger.info)(msg)
    return poll_device_token(
        dc["device_code"], client_id=client_id,
        interval=int(dc.get("interval") or 5), expires_in=int(dc.get("expires_in") or 900),
    )


def refresh_access_token(refresh_token: str, *, client_id: str = THUNDERBIRD_CLIENT_ID) -> dict[str, Any]:
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={"grant_type": "refresh_token", "client_id": client_id,
              "refresh_token": refresh_token, "scope": IMAP_SCOPE},
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        raise OutlookAuthError(
            f"refresh failed: {data.get('error')} {data.get('error_description')}"
        )
    return data


def _xoauth2_bytes(email: str, access_token: str) -> bytes:
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode()


def imap_read_messages(email: str, access_token: str, *, per_folder: int = 20) -> list[dict[str, Any]]:
    """Return recent messages (newest first) across INBOX + Junk via XOAUTH2."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    out: list[dict[str, Any]] = []
    try:
        try:
            conn.authenticate("XOAUTH2", lambda _challenge: _xoauth2_bytes(email, access_token))
        except imaplib.IMAP4.error as exc:
            raise OutlookAuthError(f"XOAUTH2 login failed for {email}: {exc}") from exc
        for folder in IMAP_FOLDERS:
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                continue
            typ, data = conn.search(None, "ALL")
            if typ != "OK" or not data or not data[0]:
                continue
            ids = data[0].split()
            for msg_id in reversed(ids[-per_folder:]):
                typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email_pkg.message_from_bytes(raw)
                subject, text_body, html_body, from_addr = _parse_message(msg)
                out.append({
                    "folder": folder,
                    "subject": subject,
                    "text": text_body,
                    "html": html_body,
                    "sender": from_addr,
                    "received_at": _parse_dt(msg.get("Date")),
                })
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    out.sort(key=lambda m: m["received_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


# ---- account pool -------------------------------------------------------------


@dataclass
class OutlookAccount:
    email: str
    password: str = ""
    refresh_token: str = ""
    client_id: str = THUNDERBIRD_CLIENT_ID
    _access_token: str = field(default="", repr=False)
    _access_expiry: float = field(default=0.0, repr=False)

    @property
    def bootstrapped(self) -> bool:
        return bool(self.refresh_token)

    def access_token(self) -> str:
        """Return a valid access token, refreshing (and rotating RT) as needed."""
        if not self.refresh_token:
            raise OutlookAuthError(f"{self.email} has no refresh_token; run outlook/bootstrap.py first")
        if self._access_token and time.time() < self._access_expiry - ACCESS_TOKEN_SKEW_SECONDS:
            return self._access_token
        tok = refresh_access_token(self.refresh_token, client_id=self.client_id)
        self._access_token = tok["access_token"]
        self._access_expiry = time.time() + float(tok.get("expires_in") or 3600)
        # MSA rotates refresh tokens; keep the newest so a persisted store stays valid.
        new_rt = tok.get("refresh_token")
        if new_rt:
            self.refresh_token = new_rt
        return self._access_token


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def parse_account_line(line: str) -> OutlookAccount | None:
    """Parse ``email----password[----client_id----refresh_token]`` in any field order.

    Seller lists come in both orders (``...----rt----cid`` and ``...----cid----rt``),
    so the client_id (a UUID) and refresh_token (a long non-UUID blob) are told
    apart by shape rather than position.
    """
    line = (line or "").strip()
    if not line or line.startswith("#") or "----" not in line:
        return None
    parts = [p.strip() for p in line.split("----")]
    email = parts[0].lower()
    password = parts[1] if len(parts) > 1 else ""
    refresh_token = ""
    client_id = THUNDERBIRD_CLIENT_ID
    for tok in (p for p in parts[2:] if p):
        if _UUID_RE.match(tok):
            client_id = tok
        elif len(tok) > len(refresh_token):
            refresh_token = tok  # longest non-UUID field is the refresh_token
    if not email:
        return None
    return OutlookAccount(email=email, password=password, refresh_token=refresh_token, client_id=client_id)


def load_accounts() -> list[OutlookAccount]:
    """Load the pool from OUTLOOK_ACCOUNTS (env) or a file, preserving order."""
    raw = _cfg("OUTLOOK_ACCOUNTS")
    if raw:
        lines = raw.replace(";", "\n").splitlines()
    else:
        path = _cfg("OUTLOOK_ACCOUNTS_FILE") or _default_accounts_path()
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    accounts: list[OutlookAccount] = []
    seen: set[str] = set()
    for line in lines:
        acct = parse_account_line(line)
        if acct and acct.email not in seen:
            seen.add(acct.email)
            accounts.append(acct)
    return accounts


def _partition_for_job(accounts: list[OutlookAccount]) -> list[OutlookAccount]:
    """Give each CI matrix job a disjoint slice of the pool so parallel jobs
    (which don't share the on-disk ``.used`` marker) never claim the same
    mailbox. No-op when JOB_INDEX/JOB_TOTAL are unset (local single-process run).
    """
    try:
        idx = int(_cfg("JOB_INDEX") or "0")
        total = int(_cfg("JOB_TOTAL") or _cfg("OUTLOOK_JOB_TOTAL") or "0")
    except ValueError:
        return accounts
    if idx >= 1 and total > 1:
        return [a for i, a in enumerate(accounts) if i % total == (idx - 1)]
    return accounts


def _default_accounts_path() -> str:
    # backend/integrations/mail/outlook.py -> repo root -> outlook/accounts.txt
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(root, "outlook", "accounts.txt")


def _used_marker_path() -> str:
    override = _cfg("OUTLOOK_USED_FILE")
    if override:
        return override
    return os.path.join(os.path.dirname(_default_accounts_path()), ".used")


# ---- service ------------------------------------------------------------------


@dataclass
class _ServiceType:
    value: str = "outlook"


class OutlookEmailService:
    service_type = _ServiceType()

    # Process-wide claim set so parallel signups in one run never grab the same
    # mailbox (the on-disk .used marker guards across runs).
    _proc_claimed: set[str] = set()
    _proc_lock = threading.Lock()

    def __init__(self, *, extra_config: dict[str, Any] | None = None) -> None:
        self._extra = dict(extra_config or {})
        self._lock = threading.Lock()
        self._claimed_email: str | None = None
        self._fixed_email = str(self._extra.get("fixed_email") or "").strip().lower()
        self._accounts = {a.email: a for a in _partition_for_job(load_accounts())}
        self._poll_interval = float(settings.get_int("email_poll_interval_seconds", 5)) or DEFAULT_POLL_INTERVAL

    @property
    def claimed_email(self) -> str | None:
        return self._claimed_email

    # -- API expected by the registration engine / OAuth adapter -----------

    def create_email(self) -> dict[str, str]:
        with self._lock:
            if self._fixed_email:
                self._claimed_email = self._fixed_email
                return {"email": self._fixed_email}
            acct = self._claim_unused()
            self._claimed_email = acct.email
            logger.info("[Outlook] claimed account: %s", acct.email)
            return {"email": acct.email}

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
        target = str(email or "").strip().lower()
        acct = self._accounts.get(target)
        if acct is None or not acct.bootstrapped:
            logger.warning("[Outlook] no bootstrapped account for %s", target)
            return None

        since = _since_datetime(otp_sent_at)
        if since is not None:
            since = since - timedelta(seconds=OTP_REQUEST_GRACE_SECONDS)
        keyword_lower = (keyword or "").lower()
        excluded = {str(c or "").strip() for c in (exclude_codes or ()) if str(c or "").strip()}

        deadline = time.time() + max(1, int(timeout or 300))
        while time.time() < deadline:
            try:
                token = acct.access_token()
                mails = imap_read_messages(target, token)
            except OutlookAuthError as exc:
                logger.warning("[Outlook] read error for %s: %s", target, exc)
                time.sleep(self._poll_interval)
                continue
            for mail in mails:
                if since is not None and mail["received_at"] is not None and mail["received_at"] < since:
                    continue
                haystack = "\n".join(
                    part for part in (mail["subject"], mail["text"], _html_to_text(mail["html"])) if part
                )
                if keyword_lower and keyword_lower not in haystack.lower():
                    continue
                code = _extract_otp(haystack, code_pattern)
                if code and code not in excluded:
                    logger.info("[Outlook] OTP for %s: %s (from %s/%s)",
                                target, code, mail["sender"], mail["folder"])
                    return code
            time.sleep(self._poll_interval)
        return None

    # -- pool claim --------------------------------------------------------

    def _claim_unused(self) -> OutlookAccount:
        used = self._load_used()
        for email, acct in self._accounts.items():
            if not acct.bootstrapped:
                continue
            with OutlookEmailService._proc_lock:
                if email in used or email in OutlookEmailService._proc_claimed:
                    continue
                OutlookEmailService._proc_claimed.add(email)
            self._mark_used(email)
            return acct
        raise RuntimeError(
            "Outlook 账号池已用尽：没有可用(已 bootstrap 且未使用)的账号。"
            " 每个 Outlook 邮箱只能注册一个 ChatGPT。"
        )

    def _load_used(self) -> set[str]:
        path = _used_marker_path()
        if not os.path.exists(path):
            return set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {ln.strip().lower() for ln in f if ln.strip()}
        except OSError:
            return set()

    def _mark_used(self, email: str) -> None:
        path = _used_marker_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(email.lower() + "\n")
        except OSError as exc:
            logger.warning("[Outlook] could not persist used marker %s: %s", path, exc)


# ---- module helpers -----------------------------------------------------------


def _cfg(key: str) -> str:
    return str(settings.get(key, "") or "").strip()


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
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
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


def _parse_message(msg: Any) -> tuple[str, str, str, str]:
    subject = _decode_mime_header(msg.get("Subject", ""))
    from_addr = _decode_mime_header(msg.get("From", ""))
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
    return subject, text_body, html_body, from_addr


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
