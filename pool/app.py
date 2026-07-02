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

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

POOL_API_KEY = os.getenv("POOL_API_KEY", "").strip()
POOL_DB = os.getenv("POOL_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool.db"))
LEASE_TTL_SECONDS = int(os.getenv("POOL_LEASE_TTL", "1200"))  # crashed-job lease auto-expires -> stale
THUNDERBIRD_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
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
    password = parts[1] if len(parts) > 1 else ""
    refresh_token, client_id = "", THUNDERBIRD_CLIENT_ID
    for tok in (p for p in parts[2:] if p):
        if _UUID_RE.match(tok):
            client_id = tok
        elif len(tok) > len(refresh_token):
            refresh_token = tok
    return {"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token}


def _redact(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    rt = d.get("refresh_token") or ""
    d["refresh_token"] = (rt[:6] + "…" + str(len(rt))) if rt else ""
    d["has_refresh_token"] = bool(rt)
    pw = d.get("password") or ""
    d["password"] = ("•" * min(len(pw), 6)) if pw else ""
    return d


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


@app.get("/api/accounts", dependencies=[Depends(require_key)])
def list_accounts(status: str = Query(default=""), limit: int = Query(default=500, le=5000)) -> dict[str, Any]:
    with _tx() as conn:
        _reap_stale(conn)
        if status:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM accounts ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return {"accounts": [_redact(r) for r in rows]}


@app.post("/api/accounts", dependencies=[Depends(require_key)])
def add_accounts(payload: dict = Body(...)) -> dict[str, Any]:
    """Bulk add. Body: {"lines": "email----password----cid----rt\\n..."}."""
    lines = str(payload.get("lines") or "").splitlines()
    parsed = [p for p in (parse_line(ln) for ln in lines) if p]
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
    out = []
    with _tx() as conn:
        _reap_stale(conn)
        rows = conn.execute(
            "SELECT * FROM accounts WHERE status='available' AND refresh_token<>'' "
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
    if status not in ("success", "failed"):
        raise HTTPException(400, "status must be 'success' or 'failed'")
    lease_token = str(payload.get("lease_token") or "")
    with _tx() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "account not found")
        if lease_token and row["lease_token"] and lease_token != row["lease_token"]:
            raise HTTPException(409, "lease_token mismatch (account re-leased?)")
        new_rt = str(payload.get("refresh_token") or "")  # runner writes back rotated MSA RT
        conn.execute(
            "UPDATE accounts SET status=?, reason=?, sub2api_account_id=?, workspace_id=?, "
            "refresh_token=CASE WHEN ?<>'' THEN ? ELSE refresh_token END, "
            "lease_token='', updated_at=? WHERE id=?",
            (status, str(payload.get("reason") or "")[:500], str(payload.get("sub2api_account_id") or ""),
             str(payload.get("workspace_id") or ""), new_rt, new_rt, _now(), acct_id),
        )
    return {"ok": True, "id": acct_id, "status": status}


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
</style></head><body>
<header>
  <h1>Outlook 账号池</h1>
  <input id="key" type="password" placeholder="X-API-Key" size="26">
  <span id="msg" class="muted"></span>
</header>
<main>
  <div class="stats" id="stats"></div>
  <details style="margin-bottom:14px"><summary style="cursor:pointer;font-weight:600">➕ 批量添加账号</summary>
    <p class="muted">一行一个：<code>email----password----client_id----refresh_token</code>（字段顺序自动识别）</p>
    <textarea id="bulk" placeholder="foo@outlook.com----pw----9e5f94bc-...----M.C5..."></textarea>
    <div style="margin-top:8px"><button class="primary" onclick="addAccounts()">添加</button></div>
  </details>
  <table><thead><tr>
    <th>ID</th><th>邮箱</th><th>状态</th><th>次数</th><th>RT</th><th>sub2api</th><th>原因</th><th>更新</th><th>操作</th>
  </tr></thead><tbody id="rows"></tbody></table>
</main>
<script>
 const $=s=>document.querySelector(s);
 let filter="";
 const key=()=>$("#key").value.trim();
 $("#key").value=localStorage.getItem("poolKey")||"";
 $("#key").addEventListener("change",()=>{localStorage.setItem("poolKey",key());refresh();});
 async function api(path,opts={}){
   opts.headers=Object.assign({"X-API-Key":key(),"Content-Type":"application/json"},opts.headers||{});
   const r=await fetch(path,opts);
   if(!r.ok){$("#msg").textContent="错误 "+r.status+" "+(await r.text()).slice(0,120);throw new Error(r.status);}
   $("#msg").textContent="";return r.json();
 }
 function fmtTime(t){if(!t)return"";const d=new Date(t*1000);return d.toLocaleString();}
 async function refresh(){
   if(!key())return;
   try{
     const st=await api("/api/stats");
     const order=["total","available","leased","success","failed","stale","disabled"];
     $("#stats").innerHTML=order.map(s=>`<span class="chip ${filter===(s==='total'?'':s)?'active':''}" onclick="setFilter('${s==='total'?'':s}')">${s}<b>${st[s]??0}</b></span>`).join("");
     const {accounts}=await api("/api/accounts"+(filter?`?status=${filter}`:""));
     $("#rows").innerHTML=accounts.map(a=>`<tr>
       <td>${a.id}</td><td>${a.email}</td>
       <td><span class="s ${a.status}">${a.status}</span></td>
       <td>${a.attempts}</td>
       <td class="muted">${a.has_refresh_token?"✓":"—"}</td>
       <td class="muted">${a.sub2api_account_id||""}</td>
       <td class="reason">${a.reason||""}</td>
       <td class="muted">${fmtTime(a.updated_at)}</td>
       <td>
         ${(a.status==='failed'||a.status==='stale')?`<button onclick="act(${a.id},'retry')">重试</button>`:""}
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
 async function del(id){if(!confirm("删除 #"+id+"?"))return;await api(`/api/accounts/${id}`,{method:"DELETE"});refresh();}
 refresh();setInterval(refresh,8000);
</script>
</body></html>"""
