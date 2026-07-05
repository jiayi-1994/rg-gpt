"""Outlook -> ChatGPT signup -> join k12 workspace -> import session (CPA) into sub2api.

Session/CPA closed loop (short-lived credential, chosen deliberately):

  1) OutlookEmailService claims a pooled mailbox
  2) Camoufox signs up ChatGPT with it (OTP read from Outlook), lands on chatgpt.com
  3) in the SAME browser, POST /backend-api/accounts/{k12}/invites/request  (join)
  4) re-read /api/auth/session -> access_token
  5) import a CPA account into sub2api:
        credentials = { access_token, chatgpt_account_id=k12, auth_mode=personalAccessToken }

sub2api ignores session_token and has no refresh_token here, so the imported
account is pinned to the access_token's expiry and auto-pauses when it lapses
(hours~days). Re-run to refresh. The DURABLE alternative is the OAuth
refresh_token flow in register_sub2api_oauth.py.

Usage:
    MAIL_PROVIDER=outlook python register_workspace.py [count]

Env:
    OUTLOOK_ACCOUNTS / OUTLOOK_ACCOUNTS_FILE   Outlook pool (email----password----rt----cid)
    WORKSPACE_ID                               母号/k12 workspace (default below)
    SUB2API_URL / SUB2API_EMAIL / SUB2API_PASSWORD / SUB2API_GROUP
    VERIFY_PROXY                               browser proxy; empty = direct
    OTP_TIMEOUT                                seconds to wait for the Outlook OTP
    JOB_BUDGET_SECONDS                         wall-clock budget (stop opening new accts)
"""
import logging
import os
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from backend.integrations.chatgpt.camoufox_register import AccountDeactivated, browser_register
from backend.integrations.mail.outlook import OutlookEmailService
from backend.integrations.sub2api import Sub2ApiClient

_logger = logging.getLogger("workspace-runner")


def log(msg):
    _logger.info(msg)


PROXY = os.getenv("VERIFY_PROXY", "").strip()
WORKSPACE_ID = os.getenv("WORKSPACE_ID", "ff598c4d-ccaf-40c1-bfaa-cb94565764b1,b49cd6d8-b52d-4c21-93d7-89cc19b5e18e,83bec9de-395a-44e6-9a30-189508c22b99").strip()  # k12


class Cfg:
    proxy = PROXY


class MailAdapter:
    """OutlookEmailService adapted to browser_register's mail_provider API."""

    def __init__(self, svc):
        self.svc = svc
        self.last_persona = None
        self.email = ""

    def create_mailbox(self):
        data = self.svc.create_email()
        self.email = data.get("email") if isinstance(data, dict) else str(data)
        return self.email

    def wait_for_otp(self, email, timeout=180, issued_after=None):
        return self.svc.get_verification_code(email=email, timeout=timeout, otp_sent_at=issued_after)


def build_cpa_payload(email: str, access_token: str, workspace_id: str, *,
                      name: str = "", id_token: str = "", concurrency: int = 10) -> dict:
    """sub2api account-data import body for one session/CPA OpenAI account."""
    creds = {
        "access_token": access_token,
        "chatgpt_account_id": workspace_id,   # gateway -> chatgpt-account-id header (workspace select)
        "auth_mode": "personalAccessToken",   # CPA marker
        "token_type": "Bearer",
        "email": email,
    }
    if id_token:
        creds["id_token"] = id_token          # sub2api auto-fills email/plan_type/account_id from it
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": [],
        "accounts": [{
            "name": name or email,
            "platform": "openai",
            "type": "oauth",
            "credentials": creds,
            "concurrency": int(concurrency),
            "priority": 0,
        }],
    }


def _find_account_id(client: Sub2ApiClient, email: str) -> str:
    """Look up an existing sub2api OpenAI account id by email, or '' if none."""
    try:
        listing = client.list_accounts(platform="openai", search=email, page_size=5)
        rows = (listing.get("data") or {}) if isinstance(listing, dict) else {}
        items = rows.get("accounts") or rows.get("items") or rows.get("list") or (
            listing.get("accounts") if isinstance(listing, dict) else None
        ) or []
        for it in items:
            if str(it.get("name") or "").lower() == email.lower() or str(it.get("email") or "").lower() == email.lower():
                return str(it.get("id") or "")
        if items:
            return str(items[0].get("id") or "")
    except Exception as exc:  # noqa: BLE001
        log(f"  WARN 查账号失败 {email}: {str(exc)[:120]}")
    return ""


def _bind_group(client: Sub2ApiClient, acct_id: str, email: str, group_id: int) -> None:
    if acct_id and group_id:
        try:
            client.move_openai_account_to_group(acct_id, group_id)
            log(f"  bound {email} -> group {group_id} (acct {acct_id})")
        except Exception as exc:  # noqa: BLE001
            log(f"  WARN 绑组失败 {email}: {str(exc)[:120]}")


def run_one(client: Sub2ApiClient, svc: OutlookEmailService, group_id: int) -> dict:
    mail = MailAdapter(svc)
    try:
        otp_timeout = max(60, int(os.getenv("OTP_TIMEOUT", "180")))
    except ValueError:
        otp_timeout = 180
    os.environ["OTP_TIMEOUT"] = str(otp_timeout)  # browser_register reads it internally

    res = browser_register(Cfg(), mail, join_workspace_id=WORKSPACE_ID)
    email = res.get("email", "") or mail.email
    at = res.get("access_token", "")
    if not at:
        svc.report_result("failed", reason="no_access_token (注册未到达 chatgpt.com)")
        return {"ok": False, "stage": "browser", "email": email, "reason": "no_access_token"}
    # 一个号可加入多个 workspace，每个切成功的 workspace 导一个独立 sub2api 账号。
    scoped = {ws: d for ws, d in (res.get("workspaces") or {}).items()
              if d.get("scoped") and d.get("access_token")}
    if not scoped:
        svc.report_result("failed", reason="no workspace scoped (join/switch 全失败, 导入会 401)")
        return {"ok": False, "stage": "switch", "email": email, "reason": "no_workspace_scoped"}

    imported = []
    for ws, d in scoped.items():
        name = f"{email}#{ws[:8]}"  # 每个 workspace 一个独立账号(同一邮箱多号)
        payload = build_cpa_payload(email, d["access_token"], ws, name=name,
                                    id_token=d.get("id_token", ""), concurrency=client.account_concurrency)
        creds = payload["accounts"][0]["credentials"]
        existing_id = _find_account_id(client, name)  # 续期识别(按 name)：已存在则更新, 否则导入
        try:
            if existing_id:
                client.update_openai_account(existing_id, {"credentials": creds})
                acct_id, stage = existing_id, "renewed"
            else:
                client.import_account_data(payload)
                acct_id, stage = _find_account_id(client, name), "imported"
        except Exception as exc:  # noqa: BLE001
            log(f"  ws {ws[:8]} {'update' if existing_id else 'import'} 失败: {str(exc)[:120]}")
            continue
        _bind_group(client, acct_id, name, group_id)
        imported.append({"ws": ws, "stage": stage, "id": acct_id})
        log(f"  ✓ {stage} {name} -> sub2api acct {acct_id}")

    if not imported:
        svc.report_result("failed", reason="所有 workspace 导入均失败")
        return {"ok": False, "stage": "import", "email": email, "scoped": list(scoped.keys())}
    svc.report_result("success", sub2api_account_id=",".join(i["id"] for i in imported if i["id"]),
                      workspace_id=",".join(scoped.keys()))
    return {"ok": True, "stage": "imported", "email": email,
            "count": len(imported), "total_ws": len(res.get("workspaces") or {}), "accounts": imported}


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    svc = OutlookEmailService()
    avail = svc.available_for_lease()
    log(f"mail source: {'pool ' + os.getenv('OUTLOOK_POOL_URL', '') if svc.pool_mode else 'local accounts.txt'}")
    if avail == 0:
        log("==== 无可用 Outlook 账号(池空 / 未 bootstrap / 均已使用), 跳过 ====")
        return
    if avail < count:
        log(f"可用 {avail} 个, count {count}->{avail}")
        count = avail

    client = Sub2ApiClient()
    client.ensure_configured()
    log(f"sub2api = {client.base_url}  workspace(k12) = {WORKSPACE_ID}  proxy = {PROXY or '(direct)'}")
    group_id = client.resolve_sold_group_id()
    log(f"group '{client.sold_group_spec}' -> id {group_id}")

    try:
        budget = int(os.getenv("JOB_BUDGET_SECONDS", "0"))
    except ValueError:
        budget = 0
    start = time.monotonic()
    durations: list[float] = []

    ok = 0
    for i in range(count):
        if budget and i > 0:
            avg = (sum(durations) / len(durations)) if durations else 90.0
            reserve = max(45.0, avg * 1.3)
            if (time.monotonic() - start) > (budget - reserve):
                log(f"==== 预算收尾, 已完成 {i} 个 ====")
                break
        log(f"==== {i + 1}/{count} ====")
        t0 = time.monotonic()
        try:
            r = run_one(client, svc, group_id)
        except AccountDeactivated as exc:
            # 号被 OpenAI 封禁 → 从池删除(不再租/重试)
            r = {"ok": False, "stage": "banned", "error": str(exc)}
            try:
                svc.report_result("banned", reason=str(exc)[:150])
                log("  号被封禁(account_deactivated) → 已从池删除")
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "stage": "exception", "error": str(exc)}
            try:
                svc.report_result("failed", reason=f"exception: {str(exc)[:150]}")
            except Exception:  # noqa: BLE001
                pass
            if "池已空" in str(exc) or "available=0" in str(exc):
                log("==== 池已空, 收尾 ====")
                break
        durations.append(time.monotonic() - t0)
        log(f"result: {r}")
        if r.get("ok"):
            ok += 1
    log(f"==== done {ok}/{count} ====")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
