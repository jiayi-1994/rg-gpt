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

from backend.integrations.chatgpt.camoufox_register import browser_register
from backend.integrations.mail.outlook import OutlookEmailService
from backend.integrations.sub2api import Sub2ApiClient

_logger = logging.getLogger("workspace-runner")


def log(msg):
    _logger.info(msg)


PROXY = os.getenv("VERIFY_PROXY", "").strip()
WORKSPACE_ID = os.getenv("WORKSPACE_ID", "631e1603-06cf-4f0b-b79b-d09fbfcfe98d").strip()  # k12


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
                      id_token: str = "", concurrency: int = 10) -> dict:
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
            "name": email,
            "platform": "openai",
            "type": "oauth",
            "credentials": creds,
            "concurrency": int(concurrency),
            "priority": 0,
        }],
    }


def _bind_group(client: Sub2ApiClient, email: str, group_id: int) -> str:
    """Find the just-imported account by email, move it to the sold group, return its id."""
    acct_id = ""
    try:
        listing = client.list_accounts(platform="openai", search=email, page_size=5)
        rows = (listing.get("data") or {}) if isinstance(listing, dict) else {}
        items = rows.get("accounts") or rows.get("items") or rows.get("list") or (
            listing.get("accounts") if isinstance(listing, dict) else None
        ) or []
        for it in items:
            if str(it.get("name") or "").lower() == email.lower() or str(it.get("email") or "").lower() == email.lower():
                acct_id = str(it.get("id") or "")
                break
        if not acct_id and items:
            acct_id = str(items[0].get("id") or "")
        if acct_id and group_id:
            client.move_openai_account_to_group(acct_id, group_id)
            log(f"  bound {email} -> group {group_id} (acct {acct_id})")
        elif not acct_id:
            log(f"  WARN 未能定位账号做绑组: {email}")
    except Exception as exc:  # noqa: BLE001
        log(f"  WARN 绑组失败 {email}: {str(exc)[:120]}")
    return acct_id


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
    if not res.get("workspace_joined"):
        svc.report_result("failed", reason="workspace_join_failed")
        return {"ok": False, "stage": "join", "email": email, "reason": "workspace_join_failed"}
    if not res.get("workspace_scoped"):
        # token 非 k12-scoped（切换失败）：导进去必 401 变废号。跳过导入。
        svc.report_result("failed", reason="workspace_switch_failed: token 非 k12-scoped")
        return {"ok": False, "stage": "switch", "email": email,
                "reason": "workspace_switch_failed", "account_id": res.get("chatgpt_account_id", "")}

    payload = build_cpa_payload(
        email, at, res.get("chatgpt_account_id") or WORKSPACE_ID,
        id_token=res.get("id_token", ""), concurrency=client.account_concurrency,
    )
    try:
        client.import_account_data(payload)
    except Exception as exc:  # noqa: BLE001
        svc.report_result("failed", reason=f"import_failed: {str(exc)[:150]}")
        return {"ok": False, "stage": "import", "email": email, "error": str(exc)[:200]}
    acct_id = _bind_group(client, email, group_id)
    svc.report_result("success", sub2api_account_id=acct_id, workspace_id=WORKSPACE_ID)
    return {"ok": True, "stage": "imported", "email": email, "workspace": WORKSPACE_ID, "sub2api_id": acct_id}


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
