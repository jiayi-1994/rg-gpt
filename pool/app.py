#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Outlook account pool — FastAPI + SQLite.

A small self-contained web service that holds a pool of Outlook accounts and
hands them out to CI runners with an atomic lease, then records which succeeded
/ failed so they can be retried deliberately. Solves the multi-job / cross-run
coordination the local accounts.txt cannot (every CI job would otherwise grab
the same mailbox and half of them collide).

State machine (a signup irreversibly consumes a mailbox, so a leased account a
job touched is NEVER auto-recycled):

    available --lease--> leased --success--> success   (terminal, stores sub2api id)
                          |     --failure--> failed     (stores reason; NOT recycled)
                          |     --TTL expiry--> stale   (job died mid-run; needs review)
    failed/stale --retry(user)--> available
    any --disable--> disabled

Auth: every /api/* endpoint requires header  X-API-Key: {POOL_API_KEY}.
Reads (list/UI) redact password + refresh_token; full creds only via /api/lease.

Run:
    pip install -r pool/requirements.txt
    POOL_API_KEY=... POOL_DB=/var/lib/outlook-pool/pool.db \
      uvicorn pool.app:app --host 0.0.0.0 --port 8080
Open http://HOST:8080/  (enter the API key once; stored in localStorage).
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import base64
import email as email_pkg
import html as html_lib
import imaplib
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

import requests

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

POOL_API_KEY = os.getenv("POOL_API_KEY", "").strip()
POOL_DB = os.getenv("POOL_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool.db"))
LEASE_TTL_SECONDS = int(os.getenv("POOL_LEASE_TTL", "1200"))  # crashed-job lease auto-expires -> stale
THUNDERBIRD_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")

# --- OTP relay: read the mailbox server-side (this VPS can reach office365, unlike a
# datacenter consumer such as gpt2api on HF) and hand back the OpenAI/ChatGPT code. ---
TOKEN_ENDPOINT = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
IMAP_HOST = "outlook.office365.com"
GMAIL_IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
IMAP_FOLDERS = ("INBOX", "Junk")
GMAIL_FOLDERS = ("INBOX", "[Gmail]/Spam")
IMAP_CONNECT_TIMEOUT = 20
ACCESS_TOKEN_SKEW = 60
OTP_PATTERNS = (
    r"(?is)(?:temporary\s+(?:openai|chatgpt)\s+login\s+code(?:\s+is)?|"
    r"verification\s+code(?:\s+is)?|one[-\s]*time\s+(?:password|code)|"
    r"security\s+code|login\s+code(?:\s+is)?|code(?:\s+is)?|"
    r"验证码(?:为|是)?|校验码|动态码)\D{0,24}(\d{4,8})",
    r"\b(\d{6})\b",
)
_otp_token_cache: dict[str, tuple[str, float]] = {}
_otp_token_lock = threading.Lock()
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

TERMINAL_OK = "success"
STATUSES = ("available", "leased", "success", "failed", "stale", "disabled")

_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(POOL_DB)) or ".", exist_ok=True)
        _conn = sqlite3.connect(POOL_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT DEFAULT '',
                client_id TEXT DEFAULT '',
                refresh_token TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'available',
                reason TEXT DEFAULT '',
                sub2api_account_id TEXT DEFAULT '',
                workspace_id TEXT DEFAULT '',
                attempts INTEGER DEFAULT 0,
                lease_token TEXT DEFAULT '',
                leased_by TEXT DEFAULT '',
                leased_at REAL,
                created_at REAL,
                updated_at REAL
            )"""
        )
        # 老库平滑升级：新增 personal usage(wham/usage) 复位窗口字段。ADD COLUMN 幂等——
        # 列已存在则 OperationalError, 吞掉即可。
        for ddl in (
            "ALTER TABLE accounts ADD COLUMN usage_reset_seconds INTEGER",
            "ALTER TABLE accounts ADD COLUMN usage_reset_at REAL",
            "ALTER TABLE accounts ADD COLUMN usage_checked_at REAL",
        ):
            try:
                _conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        _conn.commit()
    return _conn


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """Serialize writes (single-process) so lease SELECT+UPDATE is atomic."""
    with _db_lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _now() -> float:
    return time.time()


def _lease_where(kind: str) -> str:
    """SQL predicate (on top of status='available') for leasable accounts of a kind.
    gmail -> gmail domain + app password; outlook -> non-gmail + refresh_token; else any."""
    gmail = "(instr(lower(email),'@gmail.com')>0 OR instr(lower(email),'@googlemail.com')>0)"
    k = (kind or "").strip().lower()
    if k == "gmail":
        return f"{gmail} AND password<>''"
    if k == "outlook":
        return f"NOT {gmail} AND refresh_token<>''"
    return f"(refresh_token<>'' OR ({gmail} AND password<>''))"


def _reap_stale(conn: sqlite3.Connection) -> None:
    cutoff = _now() - LEASE_TTL_SECONDS
    conn.execute(
        "UPDATE accounts SET status='stale', reason='lease expired (job died / timed out)', "
        "lease_token='', updated_at=? WHERE status='leased' AND leased_at < ?",
        (_now(), cutoff),
    )


def parse_line(line: str) -> dict[str, str] | None:
    """email----password[----client_id----refresh_token] in any field order."""
    line = (line or "").strip()
    if not line or line.startswith("#") or "----" not in line:
        return None
    parts = [p.strip() for p in line.split("----")]
    email = parts[0].lower()
    if not email or "@" not in email:
        return None
    # Gmail: email----app_password (no client_id/refresh_token).
    if email.split("@")[-1] in GMAIL_DOMAINS:
        return {"email": email, "password": (parts[1] if len(parts) > 1 else "").replace(" ", ""),
                "client_id": "", "refresh_token": ""}
    password = parts[1] if len(parts) > 1 else ""
    refresh_token, client_id = "", THUNDERBIRD_CLIENT_ID
    for tok in (p for p in parts[2:] if p):
        if _UUID_RE.match(tok):
            client_id = tok
        elif len(tok) > len(refresh_token):
            refresh_token = tok
    return {"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token}


def _expand_plus(acct: dict[str, str], n: int = 5) -> list[dict[str, str]]:
    """One base account -> base + user+1..user+n (plus-addressing). All share the
    same password/client_id/refresh_token (same physical mailbox). Skips if already
    a +tag alias."""
    email = acct["email"]
    local, sep, domain = email.partition("@")
    if not sep or "+" in local:
        return [acct]
    out = [acct]
    for i in range(1, max(0, n) + 1):
        out.append({**acct, "email": f"{local}+{i}@{domain}"})
    return out


def _redact(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    rt = d.get("refresh_token") or ""
    d["refresh_token"] = (rt[:6] + "…" + str(len(rt))) if rt else ""
    d["has_refresh_token"] = bool(rt)
    pw = d.get("password") or ""
    d["password"] = ("•" * min(len(pw), 6)) if pw else ""
    return d


def _xoauth2_bytes(email_addr: str, access_token: str) -> bytes:
    return f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01".encode()


def _otp_access_token(base: str, refresh_token: str, client_id: str) -> tuple[str, str]:
    """(access_token, rotated_refresh_token_or_''). Cached per base mailbox so repeated
    OTP polls during one signup don't keep rotating the MSA refresh token."""
    with _otp_token_lock:
        cached = _otp_token_cache.get(base)
        if cached and time.time() < cached[1] - ACCESS_TOKEN_SKEW:
            return cached[0], ""
    resp = requests.post(TOKEN_ENDPOINT, data={
        "grant_type": "refresh_token", "client_id": client_id or THUNDERBIRD_CLIENT_ID,
        "refresh_token": refresh_token, "scope": IMAP_SCOPE}, timeout=30)
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"token refresh failed: {data.get('error')} {data.get('error_description')}")
    at = str(data["access_token"])
    with _otp_token_lock:
        _otp_token_cache[base] = (at, time.time() + float(data.get("expires_in") or 3600))
    new_rt = str(data.get("refresh_token") or "")
    return at, (new_rt if new_rt and new_rt != refresh_token else "")


def _otp_decode_hdr(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _otp_part_to_text(part: Any) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return str(part.get_payload())
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        try:
            return str(part.get_payload())
        except Exception:
            return ""


def _otp_parse_message(msg: Any) -> tuple[str, str, str]:
    subject = _otp_decode_hdr(msg.get("Subject", ""))
    text_body, html_body = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and not text_body:
                text_body = _otp_part_to_text(part)
            elif ctype == "text/html" and not html_body:
                html_body = _otp_part_to_text(part)
    else:
        decoded = _otp_part_to_text(msg)
        if msg.get_content_type() == "text/html":
            html_body = decoded
        else:
            text_body = decoded
    return subject, text_body, html_body


def _otp_recipients(msg: Any) -> set[str]:
    vals: list[str] = []
    for header in ("To", "Cc"):
        vals.extend(msg.get_all(header, []) or [])
    return {addr.strip().lower() for _, addr in getaddresses(vals) if addr and "@" in addr}


def _otp_parse_dt(value: Any):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    except Exception:
        return None


def _otp_html_to_text(value: Any) -> str:
    content = str(value or "")
    if not content:
        return ""
    content = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", content)
    content = re.sub(r"(?i)<br\s*/?>", "\n", content)
    content = re.sub(r"(?i)</(?:p|div|tr|table|h[1-6]|li|td|section|article)>", "\n", content)
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    content = html_lib.unescape(content)
    return re.sub(r"[\t\r\f\v ]+", " ", content).strip()


def _otp_extract(text: str) -> str:
    if not text:
        return ""
    for regex in OTP_PATTERNS:
        match = re.search(regex, text)
        if match:
            val = match.group(1) if match.groups() else match.group(0)
            if val and val != "177010":
                return val
    return ""


def _otp_collect(conn: imaplib.IMAP4_SSL, folders: tuple[str, ...], per_folder: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for folder in folders:
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            continue
        typ, data = conn.search(None, "ALL")
        if typ != "OK" or not data or not data[0]:
            continue
        for msg_id in reversed(data[0].split()[-per_folder:]):
            typ, msg_data = conn.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email_pkg.message_from_bytes(msg_data[0][1])
            subject, text_body, html_body = _otp_parse_message(msg)
            out.append({"subject": subject, "text": text_body, "html": html_body,
                        "from": _otp_decode_hdr(msg.get("From", "")), "folder": folder,
                        "recipients": _otp_recipients(msg), "received_at": _otp_parse_dt(msg.get("Date"))})
    out.sort(key=lambda m: m["received_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def _otp_read_outlook(email_addr: str, access_token: str) -> list[dict[str, Any]]:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_CONNECT_TIMEOUT)
    try:
        conn.authenticate("XOAUTH2", lambda _c: _xoauth2_bytes(email_addr, access_token))
        return _otp_collect(conn, IMAP_FOLDERS)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _otp_read_gmail(login_email: str, app_password: str) -> list[dict[str, Any]]:
    conn = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, IMAP_PORT, timeout=IMAP_CONNECT_TIMEOUT)
    try:
        conn.login(login_email, app_password)
        return _otp_collect(conn, GMAIL_FOLDERS)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


app = FastAPI(title="Outlook Account Pool", docs_url=None, redoc_url=None)


def require_key(x_api_key: str = Header(default="")) -> None:
    if not POOL_API_KEY:
        raise HTTPException(500, "POOL_API_KEY not configured on server")
    if not secrets.compare_digest(x_api_key or "", POOL_API_KEY):
        raise HTTPException(401, "bad or missing X-API-Key")


# ---- API -------------------------------------------------------------------


@app.get("/api/stats", dependencies=[Depends(require_key)])
def stats() -> dict[str, Any]:
    with _tx() as conn:
        _reap_stale(conn)
        rows = conn.execute("SELECT status, COUNT(*) c FROM accounts GROUP BY status").fetchall()
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = r["c"]
    counts["total"] = sum(counts[s] for s in STATUSES)
    return counts


@app.get("/api/available", dependencies=[Depends(require_key)])
def available(kind: str = Query(default="")) -> dict[str, Any]:
    """Count leasable accounts of a kind (gmail|outlook|any) — runner caps count by this."""
    with _tx() as conn:
        _reap_stale(conn)
        n = conn.execute(
            f"SELECT COUNT(*) c FROM accounts WHERE status='available' AND {_lease_where(kind)}"
        ).fetchone()["c"]
    return {"available": int(n or 0), "kind": kind or "any"}


@app.get("/api/accounts", dependencies=[Depends(require_key)])
def list_accounts(status: str = Query(default=""), search: str = Query(default=""),
                  limit: int = Query(default=1000, le=5000)) -> dict[str, Any]:
    conds: list[str] = []
    params: list[Any] = []
    if status:
        conds.append("status=?")
        params.append(status)
    if search.strip():
        conds.append("email LIKE ?")
        params.append(f"%{search.strip().lower()}%")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _tx() as conn:
        _reap_stale(conn)
        rows = conn.execute(f"SELECT * FROM accounts{where} ORDER BY id LIMIT ?", (*params, limit)).fetchall()
    return {"accounts": [_redact(r) for r in rows]}


@app.post("/api/accounts", dependencies=[Depends(require_key)])
def add_accounts(payload: dict = Body(...)) -> dict[str, Any]:
    """Bulk add. Body: {"lines": "email----password----cid----rt\\n..."}."""
    lines = str(payload.get("lines") or "").splitlines()
    parsed = [p for p in (parse_line(ln) for ln in lines) if p]
    # 加 1 个号 = 生成 base + 5 个 +N 别名(共 6)。可 {"expand": false} 关闭 / {"plus": N} 调数量。
    expand = payload.get("expand", True)
    n = int(payload.get("plus") or 5)
    if expand:
        parsed = [row for a in parsed for row in _expand_plus(a, n)]
    added, updated, skipped = 0, 0, 0
    with _tx() as conn:
        for a in parsed:
            existing = conn.execute("SELECT id, status FROM accounts WHERE email=?", (a["email"],)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO accounts(email,password,client_id,refresh_token,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'available', ?, ?)",
                    (a["email"], a["password"], a["client_id"], a["refresh_token"], _now(), _now()),
                )
                added += 1
            else:
                # refresh creds on existing rows but don't disturb non-available status
                conn.execute(
                    "UPDATE accounts SET password=?, client_id=?, "
                    "refresh_token=CASE WHEN ?<>'' THEN ? ELSE refresh_token END, updated_at=? WHERE id=?",
                    (a["password"], a["client_id"], a["refresh_token"], a["refresh_token"], _now(), existing["id"]),
                )
                updated += 1
    return {"added": added, "updated": updated, "skipped": skipped, "parsed": len(parsed)}


@app.post("/api/lease", dependencies=[Depends(require_key)])
def lease(payload: dict = Body(default={})) -> dict[str, Any]:
    """Atomically lease up to `count` available (bootstrapped) accounts.
    Returns FULL credentials. Body: {"count":1, "leased_by":"job-1"}."""
    count = max(1, min(int(payload.get("count") or 1), 50))
    leased_by = str(payload.get("leased_by") or "")[:80]
    kind = str(payload.get("kind") or "")  # "gmail" | "outlook" | "" (any)
    out = []
    with _tx() as conn:
        _reap_stale(conn)
        rows = conn.execute(
            f"SELECT * FROM accounts WHERE status='available' AND {_lease_where(kind)} "
            "ORDER BY id LIMIT ?",
            (count,),
        ).fetchall()
        for r in rows:
            tok = secrets.token_urlsafe(16)
            conn.execute(
                "UPDATE accounts SET status='leased', lease_token=?, leased_by=?, leased_at=?, "
                "attempts=attempts+1, updated_at=? WHERE id=? AND status='available'",
                (tok, leased_by, _now(), _now(), r["id"]),
            )
            out.append({
                "id": r["id"], "email": r["email"], "password": r["password"],
                "client_id": r["client_id"] or THUNDERBIRD_CLIENT_ID,
                "refresh_token": r["refresh_token"], "lease_token": tok,
            })
    return {"leased": out, "count": len(out)}


@app.post("/api/accounts/{acct_id}/result", dependencies=[Depends(require_key)])
def report_result(acct_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    """Report outcome for a leased account. Body: {status: success|failed, reason,
    sub2api_account_id, refresh_token, workspace_id, lease_token}."""
    status = str(payload.get("status") or "").strip().lower()
    if status not in ("success", "failed", "banned"):
        raise HTTPException(400, "status must be 'success', 'failed' or 'banned'")
    lease_token = str(payload.get("lease_token") or "")
    with _tx() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "account not found")
        if lease_token and row["lease_token"] and lease_token != row["lease_token"]:
            raise HTTPException(409, "lease_token mismatch (account re-leased?)")
        if status == "banned":
            # OpenAI 封号(account_deactivated): 号废了, 直接删出池(不再租/不再重试)
            conn.execute("DELETE FROM accounts WHERE id=?", (acct_id,))
            return {"ok": True, "id": acct_id, "status": "banned", "deleted": True}
        new_rt = str(payload.get("refresh_token") or "")  # runner writes back rotated MSA RT
        conn.execute(
            "UPDATE accounts SET status=?, reason=?, sub2api_account_id=?, workspace_id=?, "
            "refresh_token=CASE WHEN ?<>'' THEN ? ELSE refresh_token END, "
            "lease_token='', updated_at=? WHERE id=?",
            (status, str(payload.get("reason") or "")[:500], str(payload.get("sub2api_account_id") or ""),
             str(payload.get("workspace_id") or ""), new_rt, new_rt, _now(), acct_id),
        )
        # personal usage(wham/usage) 复位窗口：runner 传了 usage_reset_seconds 才写(否则保留旧值)。
        # 相对秒数会过期 → 同时算绝对复位 epoch(读时+秒) + 记读取时刻。
        usecs = payload.get("usage_reset_seconds")
        if usecs is not None:
            try:
                usecs = int(usecs)
            except (TypeError, ValueError):
                usecs = None
            if usecs is not None:
                now = _now()
                conn.execute(
                    "UPDATE accounts SET usage_reset_seconds=?, usage_reset_at=?, "
                    "usage_checked_at=?, updated_at=? WHERE id=?",
                    (usecs, now + usecs, now, now, acct_id),
                )
    return {"ok": True, "id": acct_id, "status": status}


@app.get("/api/accounts/{acct_id}/otp", dependencies=[Depends(require_key)])
def fetch_otp(acct_id: int, lease_token: str = Query(default=""), since: float = Query(default=0.0),
              exclude: str = Query(default="")) -> dict[str, Any]:
    """Server-side OTP relay: read this account's mailbox over IMAP (this VPS can reach
    office365 / gmail) and return the newest OpenAI/ChatGPT verification code addressed to
    this exact alias. For consumers whose egress can't reach IMAP directly (gpt2api on HF).

    Query: lease_token (optional, must match if the row is leased), since (unix ts — ignore
    mail older than this, to skip stale codes from a prior attempt), exclude (csv of codes
    already used, so a second call returns a fresh one). Returns {"code": str|null, "email"}."""
    with _tx() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "account not found")
    if lease_token and row["lease_token"] and lease_token != row["lease_token"]:
        raise HTTPException(409, "lease_token mismatch (account re-leased?)")
    email_addr = (row["email"] or "").strip().lower()
    base = _strip_plus(email_addr)
    is_gmail = email_addr.split("@")[-1] in GMAIL_DOMAINS
    excluded = {c.strip() for c in str(exclude or "").split(",") if c.strip()}
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromtimestamp(float(since), tz=timezone.utc) - timedelta(seconds=90)
        except Exception:
            since_dt = None
    try:
        if is_gmail:
            if not row["password"]:
                raise HTTPException(422, "gmail account has no app password")
            mails = _otp_read_gmail(base, row["password"])
        else:
            if not row["refresh_token"]:
                raise HTTPException(422, "outlook account has no refresh_token")
            access_token, new_rt = _otp_access_token(base, row["refresh_token"], row["client_id"])
            if new_rt:  # MSA rotated the RT — persist to every alias row sharing this mailbox
                local, _, domain = base.partition("@")
                with _tx() as conn:
                    conn.execute(
                        "UPDATE accounts SET refresh_token=?, updated_at=? WHERE email=? OR email LIKE ?",
                        (new_rt, _now(), base, f"{local}+%@{domain}"))
            mails = _otp_read_outlook(base, access_token)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"imap read failed: {str(exc)[:200]}")
    for mail in mails:
        recipients = mail.get("recipients") or set()
        if recipients and email_addr not in recipients:
            continue  # +N aliases share one inbox; only accept mail addressed to this alias
        if since_dt and mail.get("received_at") and mail["received_at"] < since_dt:
            continue
        haystack = "\n".join(x for x in (mail.get("subject") or "", mail.get("text") or "",
                                         _otp_html_to_text(mail.get("html"))) if x)
        code = _otp_extract(haystack)
        if code and code not in excluded:
            return {"code": code, "email": email_addr}
    return {"code": None, "email": email_addr}


@app.get("/api/accounts/{acct_id}/mails", dependencies=[Depends(require_key)])
def fetch_mails(acct_id: int, limit: int = Query(default=15, le=50)) -> dict[str, Any]:
    """Read this account's mailbox over IMAP and return recent messages addressed to
    THIS exact alias (To/Cc match — same filter as the OTP relay; +N aliases share one
    physical inbox). Body is html->text (plain text only — email HTML is attacker-
    controllable, never rendered raw). Admin inspection UI. Query: limit (<=50)."""
    with _tx() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "account not found")
    email_addr = (row["email"] or "").strip().lower()
    base = _strip_plus(email_addr)
    is_gmail = email_addr.split("@")[-1] in GMAIL_DOMAINS
    try:
        if is_gmail:
            if not row["password"]:
                raise HTTPException(422, "gmail account has no app password")
            mails = _otp_read_gmail(base, row["password"])
        else:
            if not row["refresh_token"]:
                raise HTTPException(422, "outlook account has no refresh_token")
            access_token, new_rt = _otp_access_token(base, row["refresh_token"], row["client_id"])
            if new_rt:  # MSA rotated the RT — persist to every alias row sharing this mailbox
                local, _, domain = base.partition("@")
                with _tx() as conn:
                    conn.execute(
                        "UPDATE accounts SET refresh_token=?, updated_at=? WHERE email=? OR email LIKE ?",
                        (new_rt, _now(), base, f"{local}+%@{domain}"))
            mails = _otp_read_outlook(base, access_token)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"imap read failed: {str(exc)[:200]}")
    out: list[dict[str, Any]] = []
    for mail in mails:
        recipients = mail.get("recipients") or set()
        if recipients and email_addr not in recipients:
            continue  # only mail addressed to this exact alias
        body = mail.get("text") or _otp_html_to_text(mail.get("html"))
        ra = mail.get("received_at")
        out.append({
            "from": mail.get("from") or "",
            "subject": mail.get("subject") or "",
            "folder": mail.get("folder") or "",
            "date": ra.timestamp() if ra else None,
            "body": (body or "").strip()[:4000],
        })
        if len(out) >= limit:
            break
    return {"email": email_addr, "count": len(out), "mails": out}


@app.post("/api/accounts/{acct_id}/retry", dependencies=[Depends(require_key)])
def retry(acct_id: int) -> dict[str, Any]:
    with _tx() as conn:
        row = conn.execute("SELECT status FROM accounts WHERE id=?", (acct_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "account not found")
        conn.execute(
            "UPDATE accounts SET status='available', reason='', lease_token='', updated_at=? WHERE id=?",
            (_now(), acct_id),
        )
    return {"ok": True, "id": acct_id, "status": "available"}


@app.post("/api/reset-all", dependencies=[Depends(require_key)])
def reset_all() -> dict[str, Any]:
    """Reset every non-disabled account back to available (bulk re-import after a wipe)."""
    with _tx() as conn:
        cur = conn.execute(
            "UPDATE accounts SET status='available', reason='', lease_token='', updated_at=? "
            "WHERE status NOT IN ('available','disabled')",
            (_now(),),
        )
        n = cur.rowcount
    return {"ok": True, "reset": int(n or 0)}


@app.post("/api/expand-plus", dependencies=[Depends(require_key)])
def expand_plus(payload: dict = Body(default={})) -> dict[str, Any]:
    """Backfill +1..+n plus-addressing aliases for every existing base account
    (same creds). Idempotent — skips aliases that already exist."""
    n = int(payload.get("plus") or 5)
    added = 0
    with _tx() as conn:
        rows = conn.execute("SELECT email,password,client_id,refresh_token FROM accounts WHERE instr(email,'+')=0").fetchall()
        for r in rows:
            local, sep, domain = r["email"].partition("@")
            if not sep:
                continue
            for i in range(1, n + 1):
                alias = f"{local}+{i}@{domain}"
                if conn.execute("SELECT 1 FROM accounts WHERE email=?", (alias,)).fetchone():
                    continue
                conn.execute(
                    "INSERT INTO accounts(email,password,client_id,refresh_token,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'available', ?, ?)",
                    (alias, r["password"], r["client_id"], r["refresh_token"], _now(), _now()),
                )
                added += 1
    return {"ok": True, "added": added}


@app.post("/api/accounts/{acct_id}/disable", dependencies=[Depends(require_key)])
def disable(acct_id: int) -> dict[str, Any]:
    with _tx() as conn:
        conn.execute("UPDATE accounts SET status='disabled', lease_token='', updated_at=? WHERE id=?",
                     (_now(), acct_id))
    return {"ok": True, "id": acct_id, "status": "disabled"}


@app.delete("/api/accounts/{acct_id}", dependencies=[Depends(require_key)])
def delete(acct_id: int) -> dict[str, Any]:
    with _tx() as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (acct_id,))
    return {"ok": True, "id": acct_id, "deleted": True}


def _export_line(r: sqlite3.Row) -> str:
    """把一行还原成原始凭证格式：gmail=email----app_password；outlook=email----pw----cid----rt。"""
    email = r["email"]
    pw, cid, rt = r["password"] or "", r["client_id"] or "", r["refresh_token"] or ""
    if email.split("@")[-1] in GMAIL_DOMAINS:
        return f"{email}----{pw}"
    parts = [email, pw]
    if cid:
        parts.append(cid)
    if rt:
        parts.append(rt)
    return "----".join(parts)


def _strip_plus(email: str) -> str:
    """user+3@x.com -> user@x.com（母号邮箱）。"""
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    return f"{local.split('+', 1)[0]}@{domain}"


@app.get("/api/accounts/{acct_id}/export", response_class=PlainTextResponse, dependencies=[Depends(require_key)])
def export_one(acct_id: int) -> str:
    """导出单个账号的登录收码凭证行（完整、未脱敏）：
    outlook=email----pw----cid----rt；gmail=email----app_password。用于登录邮箱 + 收 OTP。"""
    with _tx() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "account not found")
    return _export_line(row)


@app.get("/api/export", response_class=PlainTextResponse, dependencies=[Depends(require_key)])
def export_bases(status: str = Query(default=""), mode: str = Query(default="base")) -> str:
    """导出凭证行，纯文本，方便备份/重灌。
    mode=base(默认): 从子号反推母号并去重（+N 共用母号凭证）→ 每个母号一行。
    mode=all: 每个账号(含子号)导一行，完整备份。可选 status 过滤。"""
    conds, params = [], []
    if status:
        conds.append("status=?")
        params.append(status)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _tx() as conn:
        rows = conn.execute(f"SELECT * FROM accounts{where} ORDER BY id", params).fetchall()
    if mode == "all":
        return "\n".join(_export_line(r) for r in rows) + ("\n" if rows else "")
    # mode=base: 按母号邮箱去重，真母号行优先，否则用子号凭证反推
    seen: dict[str, sqlite3.Row] = {}
    for r in rows:
        base = _strip_plus(r["email"])
        if base not in seen or "+" not in r["email"]:  # 真母号行覆盖子号反推
            seen[base] = r
    out = []
    for base, r in seen.items():
        pw, cid, rt = r["password"] or "", r["client_id"] or "", r["refresh_token"] or ""
        if base.split("@")[-1] in GMAIL_DOMAINS:
            out.append(f"{base}----{pw}")
        else:
            parts = [base, pw]
            if cid:
                parts.append(cid)
            if rt:
                parts.append(rt)
            out.append("----".join(parts))
    return "\n".join(out) + ("\n" if out else "")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


INDEX_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outlook 账号池</title>
<style>
 body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}
 header{background:#1f2328;color:#fff;padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 header h1{font-size:16px;margin:0;font-weight:600}
 header input{padding:6px 8px;border:1px solid #444;border-radius:6px;background:#2d333b;color:#fff}
 main{padding:16px;max-width:1200px;margin:0 auto}
 .stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
 .chip{padding:6px 10px;border-radius:20px;background:#fff;border:1px solid #d0d7de;cursor:pointer;font-size:13px}
 .chip.active{background:#0969da;color:#fff;border-color:#0969da}
 .chip b{margin-left:4px}
 textarea{width:100%;box-sizing:border-box;min-height:70px;font:12px monospace;padding:8px;border:1px solid #d0d7de;border-radius:6px}
 button{cursor:pointer;border:1px solid #d0d7de;background:#f6f8fa;border-radius:6px;padding:5px 10px;font-size:13px}
 button.primary{background:#1f883d;color:#fff;border-color:#1f883d}
 button.danger{color:#cf222e}
 table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden}
 th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #eaeef2;font-size:13px}
 th{background:#f6f8fa;font-weight:600}
 td.reason{max-width:280px;color:#cf222e;font-size:12px;word-break:break-word}
 .s{padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600}
 .s.available{background:#dafbe1;color:#1a7f37}.s.leased{background:#fff8c5;color:#9a6700}
 .s.success{background:#0969da;color:#fff}.s.failed{background:#ffebe9;color:#cf222e}
 .s.stale{background:#fff1e5;color:#bc4c00}.s.disabled{background:#eaeef2;color:#57606a}
 .muted{color:#57606a}
 .overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:flex-start;justify-content:center;z-index:50}
 .overlay.on{display:flex}
 .modal{background:#fff;margin-top:40px;width:min(760px,94vw);max-height:86vh;overflow:auto;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.3)}
 .modal .mhead{position:sticky;top:0;background:#1f2328;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center}
 .modal .mhead b{font-size:14px}
 .modal .mhead button{background:transparent;color:#fff;border:0;font-size:20px;cursor:pointer;line-height:1}
 .mail{border-bottom:1px solid #eaeef2;padding:10px 14px}
 .mail .meta{font-size:12px;color:#57606a;display:flex;gap:10px;flex-wrap:wrap}
 .mail .subj{font-weight:600;margin:3px 0}
 .mail pre{white-space:pre-wrap;word-break:break-word;font:12px/1.5 monospace;background:#f6f8fa;padding:8px;border-radius:6px;margin:6px 0 0;max-height:280px;overflow:auto}
 .badge{background:#eaeef2;color:#57606a;border-radius:10px;padding:1px 7px;font-size:11px}
</style></head><body>
<header>
  <h1>Outlook 账号池</h1>
  <input id="key" type="password" placeholder="X-API-Key" size="26">
  <span id="msg" class="muted"></span>
</header>
<main>
  <div class="stats" id="stats"></div>
  <div style="margin:0 0 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <input id="search" placeholder="🔍 搜邮箱..." style="padding:6px 10px;border:1px solid #d0d7de;border-radius:6px;min-width:220px;font-size:13px">
    <button class="chip" style="background:#cf222e;color:#fff;border-color:#cf222e;font-weight:600" onclick="resetAll()">↺ 全部重置为可用</button>
    <button class="chip" style="background:#6f42c1;color:#fff;border-color:#6f42c1;font-weight:600" onclick="expandPlus()">＋ 展开 +5 别名(现有号)</button>
    <button class="chip" style="background:#1a7f37;color:#fff;border-color:#1a7f37;font-weight:600" onclick="exportBases()">⬇ 导出母号</button>
  </div>
  <details style="margin-bottom:14px"><summary style="cursor:pointer;font-weight:600">➕ 批量添加账号</summary>
    <p class="muted">一行一个：<code>email----password----client_id----refresh_token</code>（字段顺序自动识别）</p>
    <textarea id="bulk" placeholder="foo@outlook.com----pw----9e5f94bc-...----M.C5..."></textarea>
    <div style="margin-top:8px"><button class="primary" onclick="addAccounts()">添加</button></div>
  </details>
  <table><thead><tr>
    <th onclick="setSort('id')" style="cursor:pointer">ID ⇅</th>
    <th onclick="setSort('email')" style="cursor:pointer">邮箱 ⇅</th>
    <th onclick="setSort('status')" style="cursor:pointer">状态 ⇅</th>
    <th onclick="setSort('attempts')" style="cursor:pointer">次数 ⇅</th>
    <th>RT</th>
    <th onclick="setSort('sub2api_account_id')" style="cursor:pointer">sub2api ⇅</th>
    <th onclick="setSort('usage_reset_at')" style="cursor:pointer" title="wham/usage 主窗口复位时间">复位 ⇅</th>
    <th>原因</th>
    <th onclick="setSort('updated_at')" style="cursor:pointer">更新 ⇅</th>
    <th>操作</th>
  </tr></thead><tbody id="rows"></tbody></table>
</main>
<div class="overlay" id="mailOverlay" onclick="if(event.target===this)closeMails()">
  <div class="modal">
    <div class="mhead"><b id="mailTitle">邮件</b><button onclick="closeMails()" title="关闭">×</button></div>
    <div id="mailBody" style="padding:4px 0"></div>
  </div>
</div>
<script>
 const $=s=>document.querySelector(s);
 let filter="", searchQ="", sortKey="id", sortDir="asc";
 const key=()=>$("#key").value.trim();
 $("#key").value=localStorage.getItem("poolKey")||"";
 $("#key").addEventListener("change",()=>{localStorage.setItem("poolKey",key());refresh();});
 let _st; $("#search").addEventListener("input",()=>{clearTimeout(_st);_st=setTimeout(()=>{searchQ=$("#search").value.trim();refresh();},300);});
 function setSort(k){if(sortKey===k)sortDir=sortDir==='asc'?'desc':'asc';else{sortKey=k;sortDir='asc';}refresh();}
 async function api(path,opts={}){
   opts.headers=Object.assign({"X-API-Key":key(),"Content-Type":"application/json"},opts.headers||{});
   const r=await fetch(path,opts);
   if(!r.ok){$("#msg").textContent="错误 "+r.status+" "+(await r.text()).slice(0,120);throw new Error(r.status);}
   $("#msg").textContent="";return r.json();
 }
 function fmtTime(t){if(!t)return"";const d=new Date(t*1000);return d.toLocaleString();}
 function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
 function closeMails(){$("#mailOverlay").classList.remove("on");$("#mailBody").innerHTML="";}
 document.addEventListener("keydown",e=>{if(e.key==="Escape")closeMails();});
 async function viewMails(id,email){
   $("#mailTitle").textContent="邮件 · "+email;
   $("#mailBody").innerHTML='<div class="mail muted">读取中…</div>';
   $("#mailOverlay").classList.add("on");
   try{
     const r=await fetch(`/api/accounts/${id}/mails?limit=20`,{headers:{"X-API-Key":key()}});
     if(!r.ok){$("#mailBody").innerHTML=`<div class="mail" style="color:#cf222e">读取失败 ${r.status}: ${esc((await r.text()).slice(0,200))}</div>`;return;}
     const {mails}=await r.json();
     if(!mails||!mails.length){$("#mailBody").innerHTML='<div class="mail muted">该地址无邮件</div>';return;}
     $("#mailBody").innerHTML=mails.map(m=>`<div class="mail">
       <div class="meta"><span>${esc(m.from)}</span><span>${m.date?fmtTime(m.date):""}</span>${m.folder?`<span class="badge">${esc(m.folder)}</span>`:""}</div>
       <div class="subj">${esc(m.subject)||"(无主题)"}</div>
       ${m.body?`<pre>${esc(m.body)}</pre>`:'<div class="muted" style="font-size:12px">(无正文)</div>'}
     </div>`).join("");
   }catch(e){$("#mailBody").innerHTML=`<div class="mail" style="color:#cf222e">${esc(e.message||e)}</div>`;}
 }
 function fmtReset(t){if(!t)return"—";const rem=Math.round(t-Date.now()/1000);const d=new Date(t*1000);
   if(rem<=0)return`<span title="${d.toLocaleString()}">已复位</span>`;
   const h=Math.floor(rem/3600),m=Math.floor(rem%3600/60);
   return`<span title="${d.toLocaleString()}">${h?h+'h':''}${m}m 后</span>`;}
 async function refresh(){
   if(!key())return;
   try{
     const st=await api("/api/stats");
     const order=["total","available","leased","success","failed","stale","disabled"];
     $("#stats").innerHTML=order.map(s=>`<span class="chip ${filter===(s==='total'?'':s)?'active':''}" onclick="setFilter('${s==='total'?'':s}')">${s}<b>${st[s]??0}</b></span>`).join("");
     const qp=new URLSearchParams(); if(filter)qp.set("status",filter); if(searchQ)qp.set("search",searchQ);
     const {accounts}=await api("/api/accounts"+(qp.toString()?"?"+qp.toString():""));
     accounts.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(typeof x==="string")x=x.toLowerCase();if(typeof y==="string")y=y.toLowerCase();if(x==null)x="";if(y==null)y="";if(x<y)return sortDir==='asc'?-1:1;if(x>y)return sortDir==='asc'?1:-1;return 0;});
     $("#rows").innerHTML=accounts.map(a=>`<tr>
       <td>${a.id}</td><td>${a.email}</td>
       <td><span class="s ${a.status}">${a.status}</span></td>
       <td>${a.attempts}</td>
       <td class="muted">${a.has_refresh_token?"✓":"—"}</td>
       <td class="muted">${a.sub2api_account_id||""}</td>
       <td class="muted">${fmtReset(a.usage_reset_at)}</td>
       <td class="reason">${a.reason||""}</td>
       <td class="muted">${fmtTime(a.updated_at)}</td>
       <td>
         <button onclick="viewMails(${a.id},'${a.email}')" title="查看该邮箱最近邮件">邮件</button>
         <button onclick="exportOne(${a.id})" title="复制登录收码凭证">导出</button>
         ${a.status!=='available'?`<button onclick="act(${a.id},'retry')">重置</button>`:""}
         ${a.status!=='disabled'?`<button onclick="act(${a.id},'disable')">停用</button>`:""}
         <button class="danger" onclick="del(${a.id})">删</button>
       </td></tr>`).join("");
   }catch(e){}
 }
 function setFilter(s){filter=s;refresh();}
 async function addAccounts(){
   const lines=$("#bulk").value;if(!lines.trim())return;
   const r=await api("/api/accounts",{method:"POST",body:JSON.stringify({lines})});
   $("#msg").textContent=`已添加 ${r.added}, 更新 ${r.updated}`;$("#bulk").value="";refresh();
 }
 async function act(id,what){await api(`/api/accounts/${id}/${what}`,{method:"POST"});refresh();}
 async function resetAll(){if(!confirm("把所有非停用账号重置为 available?"))return;const r=await api("/api/reset-all",{method:"POST"});$("#msg").textContent=`已重置 ${r.reset} 个`;refresh();}
 async function expandPlus(){if(!confirm("给每个 base 号补 +1..+5 别名(共6)?"))return;const r=await api("/api/expand-plus",{method:"POST",body:JSON.stringify({plus:5})});$("#msg").textContent=`新增 ${r.added} 个别名`;refresh();}
 async function exportBases(){const r=await fetch("/api/export",{headers:{"X-API-Key":key()}});if(!r.ok){$("#msg").textContent="导出失败("+r.status+")";return;}const t=await r.text();const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([t],{type:"text/plain"}));a.download="pool-bases.txt";a.click();URL.revokeObjectURL(a.href);$("#msg").textContent="已导出母号 "+(t.trim()?t.trim().split("\\n").length:0)+" 个";}
 async function exportOne(id){
   const r=await fetch(`/api/accounts/${id}/export`,{headers:{"X-API-Key":key()}});
   if(!r.ok){$("#msg").textContent="导出失败("+r.status+")";return;}
   const t=(await r.text()).trim();
   try{await navigator.clipboard.writeText(t);$("#msg").textContent="已复制 #"+id+" 登录收码凭证";}
   catch(e){const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([t+"\\n"],{type:"text/plain"}));a.download="acct-"+id+".txt";a.click();URL.revokeObjectURL(a.href);$("#msg").textContent="已导出 #"+id;}
 }
 async function del(id){if(!confirm("删除 #"+id+"?"))return;await api(`/api/accounts/${id}`,{method:"DELETE"});refresh();}
 refresh();setInterval(refresh,8000);
</script>
</body></html>"""
