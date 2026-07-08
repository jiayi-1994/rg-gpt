"""Personal ChatGPT usage-check runner (CI).

Parallel to register_workspace.py, but WITHOUT any team-workspace join or sub2api
import. For each pooled mailbox:

  1) OutlookEmailService claims a pooled mailbox
  2) Camoufox signs up / OTP-logs-in ChatGPT with it -> lands on personal chatgpt.com
  3) in the SAME browser, GET /backend-api/wham/usage (in-browser fetch, passes CF)
  4) parse rate_limit.primary_window.reset_after_seconds
  5) write it back to the pool (pool computes absolute reset time)

A signup irreversibly consumes the mailbox, so a usage read that fails is NOT a
failure — the account registered fine; we just report success with an empty usage
field (reason=usage_fetch_failed).

Usage:
    MAIL_KIND=gmail python register_usage.py [count]

Env:
    OUTLOOK_POOL_URL / POOL_API_KEY   pool (required for pool mode)
    MAIL_KIND                         gmail|outlook (which kind to lease)
    VERIFY_PROXY                      browser proxy; empty = direct
    OTP_TIMEOUT                       seconds to wait for the mailbox OTP
    JOB_BUDGET_SECONDS                wall-clock budget (stop opening new accts)
    JOB_INDEX                         leased_by tag
"""
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from backend.integrations.chatgpt.camoufox_register import AccountDeactivated, browser_register
from backend.integrations.mail.outlook import OutlookEmailService

_logger = logging.getLogger("usage-runner")


def log(msg):
    _logger.info(msg)


PROXY = os.getenv("VERIFY_PROXY", "").strip()


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


def run_one(svc: OutlookEmailService) -> dict:
    mail = MailAdapter(svc)
    try:
        otp_timeout = max(60, int(os.getenv("OTP_TIMEOUT", "180")))
    except ValueError:
        otp_timeout = 180
    os.environ["OTP_TIMEOUT"] = str(otp_timeout)  # browser_register reads it internally

    # personal only —— 无 join_workspace_id；fetch_usage=True 读 wham/usage 复位窗口。
    res = browser_register(Cfg(), mail, fetch_usage=True)
    email = res.get("email", "") or mail.email
    at = res.get("access_token", "")
    if not at:
        svc.report_result("failed", reason="no_access_token (注册未到达 chatgpt.com)")
        return {"ok": False, "stage": "browser", "email": email, "reason": "no_access_token"}

    usage = res.get("usage") or {}
    if usage.get("ok") and usage.get("reset_after_seconds") is not None:
        secs = int(usage["reset_after_seconds"])
        svc.report_result("success", usage_reset_seconds=secs)
        log(f"  ✓ {email} usage reset_after_seconds={secs}")
        return {"ok": True, "stage": "usage", "email": email, "reset_after_seconds": secs}
    # 注册成功但 usage 没读到 —— 邮箱已消耗, 判 success(usage 空), 别浪费.
    svc.report_result("success", reason=f"usage_fetch_failed (status={usage.get('status')})")
    log(f"  ✓ {email} 注册成功但 usage 未读到 (status={usage.get('status')})")
    return {"ok": True, "stage": "usage_failed", "email": email, "usage_status": usage.get("status")}


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    svc = OutlookEmailService()
    avail = svc.available_for_lease()
    log(f"mail source: {'pool ' + os.getenv('OUTLOOK_POOL_URL', '') if svc.pool_mode else 'local accounts.txt'}")
    if avail == 0:
        log("==== 无可用账号(池空 / 未 bootstrap / 均已使用), 跳过 ====")
        return
    if avail < count:
        log(f"可用 {avail} 个, count {count}->{avail}")
        count = avail

    log(f"usage-check  proxy = {PROXY or '(direct)'}  kind = {os.getenv('MAIL_KIND', '(any)')}")

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
            r = run_one(svc)
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
