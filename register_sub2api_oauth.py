"""sub2api-led OAuth registration closed loop (CI runner).

  1) sub2api  generate-auth-url   -> {auth_url, session_id}   (PKCE lives in sub2api)
  2) real browser (Camoufox) signs up via auth_url            -> capture {code, state}
  3) sub2api  create-from-oauth   -> sub2api exchanges code w/ its code_verifier,
                                     creates an active account + binds group         (RT never touches us)

Usage:
    python register_sub2api_oauth.py [count]

Env (GitHub Secrets on CI):
    CLOUDMAIL_BASE_URL / CLOUDMAIL_PASSWORD / CLOUDMAIL_DOMAIN
    SUB2API_URL / SUB2API_EMAIL / SUB2API_PASSWORD / SUB2API_GROUP
    VERIFY_PROXY   browser proxy; empty/unset = direct (bare runner IP)
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from backend.integrations.chatgpt.camoufox_register import browser_oauth_signup
from backend.integrations.mail.cloudmail import CloudMailEmailService
from backend.integrations.sub2api import Sub2ApiClient

log = logging.getLogger("runner")
PROXY = os.getenv("VERIFY_PROXY", "").strip()  # empty = direct


class Cfg:
    proxy = PROXY


class MailAdapter:
    """CloudMailEmailService adapted to browser_oauth_signup's mail_provider API."""

    def __init__(self):
        self.svc = CloudMailEmailService()
        self.last_persona = None
        self.email = ""

    def create_mailbox(self):
        data = self.svc.create_email()
        self.email = data.get("email") if isinstance(data, dict) else str(data)
        return self.email

    def wait_for_otp(self, email, timeout=180, issued_after=None):
        return self.svc.get_verification_code(email=email, timeout=timeout, otp_sent_at=issued_after)


def run_one(client: Sub2ApiClient, group_id: int) -> dict:
    gen = client.generate_openai_auth_url()
    auth_url = str(gen.get("auth_url") or "")
    session_id = str(gen.get("session_id") or "")
    if not auth_url or not session_id:
        return {"ok": False, "stage": "generate", "detail": gen}
    log.info(f"sub2api auth_url/session ready (session={session_id[:12]}...)")

    mail = MailAdapter()
    res = browser_oauth_signup(Cfg(), mail, auth_url=auth_url, exchange=False)
    email = res.get("email", "")
    if not res.get("code"):
        return {"ok": False, "stage": "browser", "email": email,
                "add_phone": res.get("add_phone"), "reason": res.get("error") or "no_code_timeout"}

    acct = client.create_openai_account_from_oauth(
        session_id=session_id,
        code=res["code"],
        state=res["state"],
        group_ids=[group_id] if group_id else [],
        name=email,
    )
    acct_id = acct.get("id") if isinstance(acct, dict) else None
    status = acct.get("status") if isinstance(acct, dict) else None
    return {"ok": bool(acct_id), "stage": "created", "email": email,
            "account_id": acct_id, "status": status}


def _pick_domain():
    """OpenAI caps ~20 signups per email domain per burst. Spread jobs across a
    domain list (CLOUDMAIL_DOMAINS, comma-separated) by JOB_INDEX so each domain
    stays under the cap. Falls back to CLOUDMAIL_DOMAIN."""
    raw = os.getenv("CLOUDMAIL_DOMAINS") or os.getenv("CLOUDMAIL_DOMAIN") or ""
    domains = [d.strip().lstrip("@") for d in raw.replace(";", ",").split(",") if d.strip()]
    if not domains:
        return None
    idx = 0
    try:
        idx = max(0, int(os.getenv("JOB_INDEX") or "1") - 1)
    except ValueError:
        idx = 0
    chosen = domains[idx % len(domains)]
    os.environ["CLOUDMAIL_DOMAIN"] = chosen  # CloudMailEmailService reads this
    return chosen


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    chosen_domain = _pick_domain()
    client = Sub2ApiClient()
    client.ensure_configured()
    if chosen_domain:
        log.info(f"JOB_INDEX={os.getenv('JOB_INDEX', '?')} -> domain {chosen_domain}")
    log.info(f"sub2api = {client.base_url}  browser_proxy = {PROXY or '(direct)'}")
    group_id = client.resolve_sold_group_id()
    log.info(f"group '{client.sold_group_spec}' -> id {group_id}")

    # 墙钟预算：在 Register 步硬超时前主动收尾，避免被 kill 在写号中途、误判 failure。
    import time as _t
    try:
        budget = int(os.getenv("JOB_BUDGET_SECONDS", "0"))
    except ValueError:
        budget = 0
    per_acct_reserve = 120  # 单个号最坏耗时(含浏览器启动)，预留这么多才敢再开一个
    start = _t.monotonic()

    ok = 0
    for i in range(count):
        if budget and i > 0 and (_t.monotonic() - start) > (budget - per_acct_reserve):
            log.info(f"==== 预算将尽({int(_t.monotonic()-start)}s/{budget}s)，提前收尾，已完成 {i} 个 ====")
            break
        log.info(f"==== {i + 1}/{count} ====")
        try:
            r = run_one(client, group_id)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "stage": "exception", "error": str(exc)}
        log.info(f"result: {r}")
        if r.get("ok"):
            ok += 1
    log.info(f"==== done {ok}/{count} ====")
    # Non-zero exit if nothing succeeded, so the CI job surfaces failure.
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
