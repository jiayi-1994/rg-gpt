"""
基于 Camoufox 真浏览器的 ChatGPT 注册流程。
目的：让 Turnstile/反欺诈指纹通过真实浏览器执行，避免账号被内部风控标记
（导致注册 OK 但后续 Team 邀请功能被禁用）。

流程：
  1. Camoufox 启动 → goto https://chatgpt.com/
  2. 点击 Sign up → 跳转到 auth.openai.com
  3. 填邮箱 → Continue
  4. 填密码 → Continue（可能触发 Turnstile，Camoufox 指纹可通过）
  5. IMAP 取 OTP → 填入 → Continue
  6. 填姓名/生日 → Continue
  7. 回到 chatgpt.com → 从 /api/auth/session 拿 access_token
  8. 从 Cookie 拿 session_token / oai-did

返回：{email, password, session_token, access_token, device_id, cookie_header}
"""
import os
import random
import string
import time
import logging
import tempfile
import shutil
import json
import re
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _gen_name() -> tuple[str, str]:
    first_names = ["James", "John", "Emily", "Sophia", "Michael", "Oliver", "Emma",
                   "William", "Amelia", "Lucas", "Mia", "Ethan"]
    last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
                  "Miller", "Davis", "Rodriguez", "Martinez"]
    return random.choice(first_names), random.choice(last_names)


def _gen_birthday() -> tuple[str, str, str]:
    # 成年，1980-2000 随机
    year = random.randint(1980, 2000)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return str(month).zfill(2), str(day).zfill(2), str(year)


def _parse_proxy(proxy_url: str):
    """构建 Camoufox/Playwright proxy dict。

    - HTTP/HTTPS + basic auth: username/password 正确 unquote 后传入
    - SOCKS5 + auth: Camoufox 不支持，走 gost 中继
    """
    from backend.core.proxy import build_playwright_proxy_config, is_authenticated_socks5_proxy

    if not proxy_url:
        return None
    if is_authenticated_socks5_proxy(proxy_url):
        import socket as _sock
        relay_port = 18899
        try:
            with _sock.create_connection(("127.0.0.1", relay_port), timeout=2):
                pass
            return {"server": f"socks5://127.0.0.1:{relay_port}"}
        except Exception:
            raise RuntimeError(
                f"需要 gost 中继: gost -L=socks5://:{relay_port} -F={proxy_url}"
            )
    return build_playwright_proxy_config(proxy_url)


def _codex_authorize_capture(ctx, page, oauth_session, proxy, log) -> dict:
    """注册完成后在同一浏览器上下文里跑 codex authorize，抓 localhost:1455 回调 code。

    复刻 sub2api「OAuth 添加账号」手动流程：同会话内紧接授权，避免二次登录触发 add_phone。
    返回 {code, url, add_phone}。
    """
    from urllib.parse import urlparse, parse_qs

    captured = {"code": "", "url": "", "add_phone": False}

    def _extract(url: str) -> bool:
        if url and "localhost:1455" in url and "code=" in url:
            captured["url"] = url
            captured["code"] = (parse_qs(urlparse(url).query).get("code") or [""])[0]
            return bool(captured["code"])
        return False

    def _on_route(route):
        try:
            _extract(route.request.url)
        except Exception:
            pass
        try:
            route.fulfill(status=200, content_type="text/html", body="ok")
        except Exception:
            try:
                route.abort()
            except Exception:
                pass

    try:
        ctx.route("http://localhost:1455/**", _on_route)
    except Exception as ex:
        log(f"[browser-reg] codex route 注册失败: {ex}")
    log("[browser-reg] codex 授权: 导航 authorize URL ...")
    try:
        page.goto(oauth_session.auth_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"[browser-reg] codex authorize 导航返回(可能已重定向): {str(e)[:80]}")

    deadline = time.time() + 90
    while time.time() < deadline and not captured["code"]:
        cur = ""
        try:
            cur = page.url
        except Exception:
            pass
        if _extract(cur):
            break
        low = cur.lower()
        if "add-phone" in low or "/phone" in low:
            log(f"[browser-reg] codex 授权撞到 add-phone: {cur[:100]}")
            captured["add_phone"] = True
            break
        # 同意 / 选择工作区 / 继续
        for sel in ['button:has-text("Authorize")', 'button:has-text("Allow")',
                    'button:has-text("Allow access")', 'button:has-text("Continue")',
                    'button:has-text("Confirm")', 'button:has-text("Agree")',
                    'button[type="submit"]']:
            try:
                b = page.query_selector(sel)
                if b and b.is_visible():
                    b.click(timeout=4000)
                    log(f"[browser-reg] codex 授权点击: {sel}")
                    break
            except Exception:
                pass
        time.sleep(1.5)

    try:
        ctx.unroute("http://localhost:1455/**")
    except Exception:
        pass
    return captured


def _join_workspace(page, workspace_id, access_token, device_id, log, *, route="request", max_retries=3) -> bool:
    """在浏览器上下文内向母号 workspace 发加入申请（复刻已测通的油猴 sendOne 逻辑）。

    必须在浏览器里 page.evaluate 跑 —— chatgpt.com/backend-api 有 Cloudflare 防护，
    浏览器外的 Python 请求会被挡；浏览器里带真实指纹+cookie 才过。
    route: "request"(主动申请, 母号自动批准) 或 "accept"(接受已有邀请)。
    """
    at = access_token
    for attempt in range(max_retries + 1):
        try:
            res = page.evaluate(
                """async (a) => {
                    const url = `/backend-api/accounts/${a.ws}/invites/${a.route}`;
                    const r = await fetch(url, {
                        method: "POST",
                        headers: {
                            accept: "*/*",
                            authorization: "Bearer " + a.at,
                            "content-type": "application/json",
                            "oai-device-id": a.dev || crypto.randomUUID(),
                            "oai-language": navigator.language || "en-US",
                        },
                        body: "", mode: "cors", credentials: "include",
                    });
                    let t = ""; try { t = await r.text(); } catch (_) {}
                    return { status: r.status, ok: r.ok, text: (t || "").slice(0, 300) };
                }""",
                {"ws": workspace_id, "at": at, "dev": device_id or "", "route": route},
            )
        except Exception as e:  # noqa: BLE001
            log(f"[join] evaluate 异常: {str(e)[:80]}")
            res = None
        if res and res.get("ok"):
            log(f"[join] ✓ {workspace_id[:8]} HTTP {res.get('status')}: {str(res.get('text'))[:120]}")
            return True
        code = (res or {}).get("status")
        log(f"[join] ✗ {workspace_id[:8]} HTTP {code}: {str((res or {}).get('text'))[:150]}")
        # 401/403 → 子号 AT 失效，重取 session 再试（同油猴逻辑）
        if code in (401, 403):
            try:
                s = page.evaluate(
                    """async () => { const r = await fetch("/api/auth/session", {credentials:"include"});
                        return await r.json(); }"""
                )
                if isinstance(s, dict) and s.get("accessToken"):
                    at = s["accessToken"]
            except Exception:  # noqa: BLE001
                pass
        if attempt < max_retries:
            time.sleep(3 * (attempt + 1))
    return False


def _jwt_account_id(access_token: str) -> str:
    """Decode the chatgpt_account_id claim from a ChatGPT /api/auth/session JWT."""
    try:
        seg = access_token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(__import__("base64").urlsafe_b64decode(seg))
        return str((claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or "")
    except Exception:  # noqa: BLE001
        return ""


def _fetch_session(page):
    try:
        return page.evaluate(
            """async () => { try { const r = await fetch('/api/auth/session', {credentials:'include'});
                return await r.json(); } catch(e){ return null; } }"""
        )
    except Exception:  # noqa: BLE001
        return None


def _switch_workspace_and_get_token(page, workspace_id, access_token, log, *, timeout=45) -> dict:
    """Switch active ChatGPT workspace to workspace_id; return a workspace-scoped
    {access_token, id_token, account_id}, verified via the token's chatgpt_account_id claim.

    Mechanism (live-captured): GET /api/auth/session?exchange_workspace_token=true&
    workspace_id=<id>&reason=setCurrentAccount performs the workspace token exchange
    (flips the _account cookie + session token to the workspace); its response — and any
    subsequent /api/auth/session — then carry the workspace-scoped accessToken. Pure
    in-browser fetch, so it inherits the CF-cleared session (no upstream 403/redirect).
    """
    deadline = time.time() + max(10, int(timeout))
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            resp = page.evaluate(
                """async (ws) => {
                    const u = '/api/auth/session?exchange_workspace_token=true&workspace_id='
                              + encodeURIComponent(ws) + '&reason=setCurrentAccount';
                    try {
                        const r = await fetch(u, {credentials:'include', headers:{accept:'*/*'}});
                        let j = null; try { j = await r.json(); } catch(_){}
                        return {status:r.status, body:j};
                    } catch(e){ return {status:-1, err:String(e)}; }
                }""",
                workspace_id,
            )
        except Exception as e:  # noqa: BLE001
            resp = {"status": -2, "err": str(e)[:80]}
        body = (resp or {}).get("body") or {}
        at = body.get("accessToken") or ""
        # exchange 响应直接带 k12 token；否则再普通取一次 session 兜底
        if not (at and _jwt_account_id(at) == workspace_id):
            sess = _fetch_session(page)
            if isinstance(sess, dict) and sess.get("accessToken"):
                at, body = sess["accessToken"], sess
        if at and _jwt_account_id(at) == workspace_id:
            log(f"[switch] ✓ workspace token exchanged -> k12 (attempt {attempt})")
            return {"access_token": at, "id_token": body.get("idToken", ""), "account_id": workspace_id}
        log(f"[switch] attempt {attempt}: status={(resp or {}).get('status')} "
            f"scope={_jwt_account_id(at) or '-'} (want {workspace_id[:8]})")
        time.sleep(3)

    sess = _fetch_session(page)
    at = (sess or {}).get("accessToken") or access_token
    got = _jwt_account_id(at)
    log(f"[switch] ✗ timeout, account_id={got or '?'} (wanted {workspace_id[:8]})")
    return {"access_token": at, "id_token": (sess or {}).get("idToken", ""), "account_id": got}


def _select_auth_workspace(page, workspace_id, log, *, timeout=60) -> bool:
    """On auth.openai.com/workspace (already-registered accounts land here after email OTP),
    pick a workspace so the flow continues to chatgpt.com.

    Click the entry ONCE and let the SPA POST /api/accounts/workspace/select + navigate
    itself — do NOT inject a page.goto while it navigates; that races the SPA and can crash
    the Firefox driver on an uncaught pageError (pageError.location.url undefined). Which
    entry we pick doesn't matter; the downstream exchange-switch re-scopes to workspace_id.
    """
    clicked = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            low = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            low = ""
        if "/workspace" not in low:
            return True  # SPA navigated off the select page — done
        if not clicked:
            for sel in ['text=/schools\\.nyc\\.gov/i', 'text=/workspace #\\d/i',
                        '[data-testid*="workspace"]', 'button:has-text("Workspace")',
                        'a:has-text("Workspace")']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        _robust_click(page, el, log, "ws-select")
                        log(f"[ws-select] 点击 workspace 条目: {sel}")
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
        time.sleep(2)  # 等 SPA 自己导航，不插 goto
    # 仅点击彻底没生效时的兜底：POST 选定 workspace，绝不 goto（避免撞 SPA 导航崩 driver）
    if workspace_id and "/workspace" in ((page.url or "").lower() if True else ""):
        try:
            page.evaluate(
                """async (ws) => { try { await fetch('/api/accounts/workspace/select',
                    {method:'POST', credentials:'include',
                     headers:{'content-type':'application/json','accept':'application/json'},
                     body: JSON.stringify({workspace_id: ws})}); } catch(e){} }""",
                workspace_id,
            )
            log("[ws-select] POST /api/accounts/workspace/select 兜底(无 goto)")
            time.sleep(5)
        except Exception:  # noqa: BLE001
            pass
    try:
        return "/workspace" not in (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return False


def browser_register(cfg, mail_provider, oauth_session=None, join_workspace_id: str = "") -> dict:
    """
    用真实浏览器走注册流程。
    cfg: Config 实例（需要 proxy 字段）
    mail_provider: MailProvider 实例（调 create_mailbox + wait_for_otp）
    oauth_session: 可选 OAuthSession；提供则注册完成后在同会话里跑 codex 授权拿 refresh_token。
    join_workspace_id: 可选 workspace id；提供则到达 chatgpt.com 后在同会话里申请加入该 workspace，
                       并把 chatgpt_account_id 写进返回结果（供 CPA/session 导入选定工作空间）。
    返回 dict：与 AuthResult.to_dict() 格式兼容
    """
    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen

    email = mail_provider.create_mailbox()
    # 优先复用 mail_provider 算法生成的同源 persona（邮箱前缀与 first/last 一致 + 密码=local 倒序）
    persona = getattr(mail_provider, "last_persona", None)
    if persona is not None:
        password = persona.password
        first_name = persona.first
        last_name = persona.last
        logger.info(f"[browser-reg] 使用 mail_provider 同源 persona")
    else:
        # 兼容 resume / 老路径：邮箱去 @ 当密码 + 独立挑名字
        password = email.replace("@", "")
        if len(password) < 8:
            password = f"{password}2026OpenAI"
        first_name, last_name = _gen_name()
    bmonth, bday, byear = _gen_birthday()
    logger.info(f"[browser-reg] 创建账号: {email}")
    logger.info(f"[browser-reg] 密码: {password}  姓名: {first_name} {last_name}")

    cf_proxy = _parse_proxy(cfg.proxy)
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    tmp_profile = tempfile.mkdtemp(prefix="chatgpt_reg_")
    logger.info(f"[browser-reg] 临时 profile: {tmp_profile}")

    result = {
        "email": email,
        "password": password,
        "session_token": "",
        "access_token": "",
        "device_id": "",
        "csrf_token": "",
        "id_token": "",
        "refresh_token": "",
        "cookie_header": "",
    }

    try:
        with Camoufox(
            headless=not has_display,
            humanize=True,
            persistent_context=True,
            user_data_dir=tmp_profile,
            os="windows",
            screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy,
            geoip=True,
            locale="en-US",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # [1] 打开 ChatGPT 首页，点 "Sign up for free"
            logger.info("[browser-reg] 打开 ChatGPT 首页 ...")
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            # 等 React 渲染完成 + Sign up 按钮可交互
            try:
                page.wait_for_selector('button[data-testid="signup-button"], a[data-testid="signup-button"]',
                                       state='visible', timeout=20000)
            except Exception:
                pass
            time.sleep(3)

            # 点击 Sign up 按钮 — 找右上角的 "Sign up for free"
            clicked_signup = False
            for sel in ['a[data-testid="signup-button"]',
                        'button[data-testid="signup-button"]',
                        'button:has-text("Sign up for free")',
                        'a:has-text("Sign up for free")',
                        'button:has-text("Sign up")',
                        'a:has-text("Sign up")']:
                try:
                    btns = page.query_selector_all(sel)
                except Exception:
                    continue
                for btn in btns:
                    try:
                        if not btn.is_visible():
                            continue
                        text = btn.inner_text().lower()
                        if "sign up" not in text:
                            continue
                        # 用 5s 超时的 click，防止卡 30s
                        try:
                            btn.click(timeout=5000)
                        except Exception:
                            # click 卡住就用 JS 触发
                            btn.evaluate("el => el.click()")
                        clicked_signup = True
                        logger.info(f"[browser-reg] 点击 Sign up ({sel}): {text[:40]}")
                        break
                    except Exception as e_click:
                        if "attached to the DOM" in str(e_click) or "detached" in str(e_click).lower():
                            continue
                        logger.warning(f"[browser-reg] click 异常: {e_click}")
                if clicked_signup:
                    break
            if not clicked_signup:
                page.screenshot(path="/tmp/browser_reg_no_signup.png")
                raise RuntimeError(f"未找到 Sign up 按钮, URL={page.url[:120]}")

            # 等待跳转到 auth.openai.com 或 modal 加载（含重试点击）
            pre_url = page.url
            for i in range(20):
                time.sleep(1)
                if "auth.openai.com" in page.url or page.query_selector('input[type="email"]'):
                    break
                # 如果 5s 后还没变化，重试点击 Sign up
                if i == 5 and page.url == pre_url:
                    logger.info("[browser-reg] Sign up 点击未生效，重试")
                    try:
                        btn = page.query_selector('button[data-testid="signup-button"], a[data-testid="signup-button"]')
                        if btn:
                            btn.click(timeout=3000)
                    except Exception:
                        try:
                            btn.evaluate("el => el.click()")
                        except Exception:
                            pass
            logger.info(f"[browser-reg] 当前 URL: {page.url[:120]}")
            page.screenshot(path="/tmp/browser_reg_before_email.png")

            # [2] 填邮箱（chatgpt.com 新版 Sign up 是 modal 覆盖层，click 易超时 → 直接 fill + JS 兜底）
            logger.info("[browser-reg] 填邮箱 ...")
            page.wait_for_selector('input[type="email"], input[name="email"]', state="visible", timeout=30000)

            def _visible_email_input():
                for el in (page.query_selector_all('input[type="email"]')
                           + page.query_selector_all('input[name="email"]')):
                    try:
                        if el.is_visible():
                            return el
                    except Exception:
                        continue
                return None

            email_ok = False
            for _try in range(5):
                try:
                    ei = _visible_email_input()
                    if not ei:
                        time.sleep(0.6)
                        continue
                    try:
                        ei.fill(email, timeout=8000)          # fill 自带 focus，比 click 稳
                    except Exception:
                        ei.evaluate(                          # JS 兜底：直接写值 + 触发 React onChange
                            """(el, v) => {
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(el, v);
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                            }""",
                            email,
                        )
                    cur = ""
                    try:
                        cur = ei.input_value()
                    except Exception:
                        pass
                    if (cur or "").strip().lower() == email.lower():
                        email_ok = True
                        break
                    time.sleep(0.5)
                except Exception as e:
                    if "not attached" in str(e).lower() or "detached" in str(e).lower():
                        logger.info(f"[browser-reg] email input 脱链 重试 {_try+1}/5")
                        time.sleep(0.6)
                        continue
                    logger.warning(f"[browser-reg] 填邮箱异常 {_try+1}/5: {str(e)[:80]}")
                    time.sleep(0.5)
            if not email_ok:
                page.screenshot(path="/tmp/browser_reg_email_fail.png")
                logger.warning("[browser-reg] 邮箱可能未填入，继续尝试提交")
            time.sleep(random.uniform(0.5, 1.2))
            # Continue（force + JS 兜底点击）
            clicked_email = False
            for sel in ['button[type="submit"]', 'button:has-text("Continue")',
                        'button:has-text("Next")']:
                b = page.query_selector(sel)
                if b and b.is_visible():
                    try:
                        b.click(timeout=5000)
                    except Exception:
                        try:
                            b.click(timeout=3000, force=True)
                        except Exception:
                            try:
                                b.evaluate("el => el.click()")
                            except Exception:
                                continue
                    clicked_email = True
                    logger.info(f"[browser-reg] 点击 email 继续: {sel}")
                    break
            if not clicked_email:
                try:
                    page.keyboard.press("Enter")
                    logger.info("[browser-reg] email 继续: 回车兜底")
                except Exception:
                    pass
            time.sleep(3)

            # [3] 填密码（新账号会看到密码框）
            logger.info("[browser-reg] 等待密码框 ...")
            try:
                page.wait_for_selector(
                    'input[type="password"], input[name="password"]',
                    state="visible", timeout=30000,
                )
                pwd_input = page.query_selector('input[type="password"]:visible') or \
                            page.query_selector('input[name="password"]:visible') or \
                            page.query_selector('input[type="password"]')
                try:
                    pwd_input.fill(password, timeout=8000)
                except Exception:
                    pwd_input.evaluate(
                        """(el, v) => {
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                            setter.call(el, v);
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }""",
                        password,
                    )
                time.sleep(random.uniform(0.5, 1.2))
                clicked_pwd = False
                for sel in ['button[type="submit"]', 'button:has-text("Continue")',
                            'button:has-text("Create")', 'button:has-text("Next")']:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        try:
                            b.click(timeout=5000)
                        except Exception:
                            try:
                                b.click(timeout=3000, force=True)
                            except Exception:
                                try:
                                    b.evaluate("el => el.click()")
                                except Exception:
                                    continue
                        clicked_pwd = True
                        logger.info(f"[browser-reg] 点击 password 继续: {sel}")
                        break
                if not clicked_pwd:
                    try:
                        page.keyboard.press("Enter")
                        logger.info("[browser-reg] password 继续: 回车兜底")
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[browser-reg] 密码框异常: {e}，可能走无密码 OTP 路径")

            time.sleep(3)
            logger.info(f"[browser-reg] 密码后 URL: {page.url[:120]}")

            # [4] Turnstile / hCaptcha 等待（Camoufox 指纹通常可自动通过）
            logger.info("[browser-reg] 等待反欺诈检查 ...")
            for wait_i in range(30):
                time.sleep(1)
                cur = page.url
                # 到达 OTP 输入或继续步骤 → 通过
                if page.query_selector('input[autocomplete="one-time-code"]') or \
                   page.query_selector('input[name="code"]') or \
                   page.query_selector('input[inputmode="numeric"]'):
                    logger.info(f"[browser-reg] 已到达 OTP 页面")
                    break
                if "chatgpt.com" in cur and "auth.openai.com" not in cur:
                    logger.info(f"[browser-reg] 已直接登录到 chatgpt.com")
                    break
                if wait_i == 15:
                    page.screenshot(path="/tmp/browser_reg_wait15.png")
                    logger.info(f"[browser-reg] 15s 等待中: {cur[:80]}")

            # [5] OTP 步骤
            if page.query_selector('input[autocomplete="one-time-code"]') or \
               page.query_selector('input[inputmode="numeric"]'):
                logger.info("[browser-reg] 等待 IMAP OTP ...")
                otp_sent_at = time.time()
                try:
                    otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "180")))
                except Exception:
                    otp_timeout = 180
                otp_code = mail_provider.wait_for_otp(email, timeout=otp_timeout, issued_after=otp_sent_at)
                logger.info(f"[browser-reg] 收到 OTP: {otp_code}")
                # 填 OTP
                otp_filled = False
                # 可能是单框 / 多框两种
                single = page.query_selector('input[autocomplete="one-time-code"]') or \
                         page.query_selector('input[name="code"]') or \
                         page.query_selector('input[inputmode="numeric"]:not([maxlength="1"])')
                if single:
                    _robust_click(page, single, logger.info, "otp-input")
                    time.sleep(0.3)
                    _robust_fill(single, otp_code)
                    otp_filled = True
                else:
                    digits = page.query_selector_all('input[maxlength="1"][inputmode="numeric"]') or \
                             page.query_selector_all('input[maxlength="1"]')
                    if len(digits) >= 6:
                        for i, ch in enumerate(otp_code[:6]):
                            try:
                                digits[i].click(timeout=4000)
                            except Exception:
                                try:
                                    digits[i].evaluate("el => el.focus()")
                                except Exception:
                                    pass
                            time.sleep(0.1)
                            _robust_fill(digits[i], ch)
                        otp_filled = True
                if not otp_filled:
                    page.screenshot(path="/tmp/browser_reg_otp_fail.png")
                    raise RuntimeError("OTP 输入框未找到")
                time.sleep(0.8)
                # Continue（robust：短超时 + force + JS + 回车兜底，避免默认 30s click 卡死）
                for sel in ['button[type="submit"]', 'button:has-text("Continue")',
                            'button:has-text("Verify")', 'button:has-text("Next")']:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        _robust_click(page, b, logger.info, "otp")
                        logger.info(f"[browser-reg] 点击 OTP 继续: {sel}")
                        break
                time.sleep(4)

                # OpenAI 在 OTP 错误时会显示 "Incorrect code" 红字，反复点
                # Continue 会触发 max_check_attempts 风控（永久卡死）。早退。
                try:
                    err = page.query_selector(
                        'text=/incorrect code|invalid code|wrong code|验证码不正确|验证码错误/i'
                    )
                    if err and err.is_visible():
                        page.screenshot(path="/tmp/browser_reg_otp_rejected.png")
                        raise RuntimeError(
                            f"OpenAI 拒绝 OTP {otp_code}（OTP 抽取错误，可能是 hex 颜色/tracking id 假阳性）"
                        )
                except RuntimeError:
                    raise
                except Exception:
                    pass

            # [5.5] OTP 后重定向到 /workspace(已注册账号) 或 /about-you(新号)。重定向可能晚到
            #        ~30s，所以轮询等待；命中 /workspace 就选择 workspace 继续，并标记已是成员。
            for _ in range(45):
                time.sleep(1)
                try:
                    _u = (page.url or "").lower()
                except Exception:  # noqa: BLE001
                    _u = ""
                if "/workspace" in _u:
                    logger.info("[browser-reg] 命中 workspace 选择页(已注册账号)")
                    _select_auth_workspace(page, join_workspace_id or "", logger.info)
                    result["via_workspace_select"] = True
                    break
                if "about-you" in _u or ("chatgpt.com" in _u and "auth.openai.com" not in _u):
                    break

            # [6] /about-you：Full name + Age（单框）
            logger.info(f"[browser-reg] OTP 后 URL: {page.url[:120]}")
            time.sleep(5)  # 等重定向到 /about-you
            logger.info(f"[browser-reg] 稳定后 URL: {page.url[:120]}")

            # 等 /about-you 表单加载完成。先等 URL 稳定
            for _ in range(20):
                time.sleep(1)
                if "about-you" in page.url or "chatgpt.com" in page.url:
                    break

            # OpenAI about-you 变种：
            #   老版：Full name + Age（数字框）
            #   新版（2026-04 起）：Full name + Birthday（日期框，预填今日）
            # 用 JS 一次性把所有 input 的元数据导出，避免 visibility 检测不一致
            def _enum_inputs():
                try:
                    return page.evaluate('''() => {
                        return Array.from(document.querySelectorAll('input')).map((el, idx) => {
                            const r = el.getBoundingClientRect();
                            const cs = getComputedStyle(el);
                            return {
                                idx,
                                type: (el.type || '').toLowerCase(),
                                name: el.name || '',
                                placeholder: el.placeholder || '',
                                ariaLabel: el.getAttribute('aria-label') || '',
                                label: (el.labels && el.labels[0] && el.labels[0].innerText) || '',
                                value: el.value || '',
                                visible: (r.width > 0 && r.height > 0 &&
                                          cs.visibility !== 'hidden' && cs.display !== 'none'),
                            };
                        });
                    }''') or []
                except Exception:
                    return []

            def _is_birthday(meta: dict) -> bool:
                blob = " ".join([meta.get("type",""), meta.get("name",""),
                                  meta.get("placeholder",""), meta.get("ariaLabel",""),
                                  meta.get("label","")]).lower()
                if meta.get("type") == "date":
                    return True
                return any(kw in blob for kw in ("birth", "birthday", "dob",
                                                  "mm/dd/yyyy", "mm / dd / yyyy",
                                                  "year of birth", "year"))

            def _is_year_of_birth(meta: dict) -> bool:
                # 新版 (2026-07) "What year were you born?" 只要 4 位年份，不是完整日期
                blob = " ".join([meta.get("name",""), meta.get("placeholder",""),
                                  meta.get("ariaLabel",""), meta.get("label","")]).lower()
                if meta.get("type") == "date":
                    return False
                return "year of birth" in blob or ("year" in blob and "birth" in blob)

            full_name_input = None
            birthday_input = None
            birthday_meta = None
            for attempt in range(30):
                if result.get("via_workspace_select"):
                    break  # 已注册账号走了 workspace 选择, 没有 about-you 表单
                metas = _enum_inputs()
                visible_metas = [m for m in metas if m["visible"]
                                  and m["type"] not in ("hidden","submit","button",
                                                         "checkbox","radio","password")]
                # 先挑 Birthday，剩下的看作 name
                bd = next((m for m in visible_metas if _is_birthday(m)), None)
                name_m = next((m for m in visible_metas
                                if m is not bd
                                and not _is_birthday(m)), None)
                if bd and name_m:
                    all_inputs_el = page.query_selector_all('input')
                    full_name_input = all_inputs_el[name_m["idx"]]
                    birthday_input = all_inputs_el[bd["idx"]]
                    birthday_meta = bd
                    logger.info(f"[browser-reg] 表单: name.idx={name_m['idx']} "
                                f"birthday.idx={bd['idx']} type={bd['type']} "
                                f"placeholder={bd['placeholder'][:30]!r}")
                    break
                # 兼容老版 age：2 个 input 且都不匹配 birthday
                if not bd and len(visible_metas) >= 2:
                    all_inputs_el = page.query_selector_all('input')
                    full_name_input = all_inputs_el[visible_metas[0]["idx"]]
                    birthday_input = all_inputs_el[visible_metas[1]["idx"]]
                    birthday_meta = visible_metas[1]
                    logger.info(f"[browser-reg] 表单 (legacy age): {len(visible_metas)} inputs")
                    break
                if "chatgpt.com" in page.url and "auth" not in page.url:
                    break
                if attempt == 5:
                    page.screenshot(path="/tmp/browser_reg_about_you_wait.png")
                    logger.info(f"[browser-reg] 等待 about-you 输入框 5s, URL={page.url[:100]} "
                                f"inputs visible={len(visible_metas)}")
                time.sleep(1)

            if full_name_input and birthday_input:
                page.screenshot(path="/tmp/browser_reg_about_you.png")
                full_name = f"{first_name} {last_name}"
                # Birthday：26-40 岁之间的 1 月 15 日（足够>18，固定日期便于一致指纹）
                import datetime as _dt
                year = _dt.datetime.now().year - random.randint(26, 40)
                mm, dd = "01", "15"
                # native date input 用 YYYY-MM-DD，文本框大多是 MM/DD/YYYY
                bd_type = (birthday_meta or {}).get("type", "")
                bd_is_year = _is_year_of_birth(birthday_meta or {})
                if bd_type == "date":
                    birthday_str = f"{year}-{mm}-{dd}"
                elif bd_is_year:
                    birthday_str = str(year)          # "Year of birth" 只要年份
                else:
                    birthday_str = f"{mm}/{dd}/{year}"
                legacy_age = str(random.randint(26, 40))
                logger.info(f"[browser-reg] 填 Full name={full_name}  "
                            f"Birthday={birthday_str} (year_field={bd_is_year} legacy_age={legacy_age})")
                try:
                    full_name_input.focus(); time.sleep(0.3)
                    page.keyboard.type(full_name, delay=random.randint(30, 80))
                    time.sleep(random.uniform(0.4, 0.9))
                    birthday_input.focus(); time.sleep(0.3)
                    # 先清空（预填可能有今日日期）
                    try:
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Delete")
                    except Exception:
                        pass
                    # 对 native date input 用 fill 直接写 ISO；文本框用 keyboard.type
                    if bd_type == "date":
                        try:
                            birthday_input.fill(birthday_str)
                        except Exception:
                            page.keyboard.type(birthday_str, delay=random.randint(30, 70))
                    else:
                        # year-of-birth 只打年份；MM/DD/YYYY birthday 打整串；否则老版 age 数字
                        if bd_is_year or _is_birthday(birthday_meta or {}):
                            page.keyboard.type(birthday_str, delay=random.randint(30, 70))
                        else:
                            page.keyboard.type(legacy_age, delay=random.randint(40, 100))
                    time.sleep(random.uniform(0.4, 0.9))
                    clicked = False
                    for sel in ['button:has-text("Finish")', 'button:has-text("Create")',
                                'button:has-text("Agree")', 'button[type="submit"]',
                                'button:has-text("Continue")']:
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            b.click()
                            clicked = True
                            logger.info(f"[browser-reg] 点击 about-you 继续: {sel}")
                            break
                    if not clicked:
                        page.screenshot(path="/tmp/browser_reg_no_finish_btn.png")
                except Exception as e:
                    logger.warning(f"[browser-reg] about-you 填写异常: {e}")
                    page.screenshot(path="/tmp/browser_reg_name_fail.png")
            else:
                page.screenshot(path="/tmp/browser_reg_no_name_form.png")
                logger.warning(f"[browser-reg] 未找到 about-you 表单，URL={page.url[:120]}")

            # [7] 等待回到 chatgpt.com (可能有中间页如 email-verification / success-page)
            logger.info("[browser-reg] 等待跳转回 chatgpt.com ...")
            arrived = False
            last_url = ""
            for i in range(120):
                time.sleep(1)
                cur = page.url
                if cur != last_url:
                    logger.info(f"[browser-reg] URL@{i}s: {cur[:120]}")
                    last_url = cur
                # 到 chatgpt.com 且已加载 React 主界面
                if "chatgpt.com" in cur and "auth.openai.com" not in cur:
                    # 等 /api/auth/session 能正常返回 accessToken 才算完成
                    try:
                        info = page.evaluate('''async () => {
                            try {
                                const r = await fetch("/api/auth/session", {credentials: "include"});
                                const d = await r.json();
                                return d.accessToken ? d.accessToken.length : 0;
                            } catch(e){ return -1; }
                        }''')
                        if info and info > 100:
                            arrived = True
                            logger.info(f"[browser-reg] 到达 + session accessToken 长度={info}")
                            break
                    except Exception:
                        pass
                # 如果仍在 auth.openai.com，可能还有 /email-verification 或其他中转，继续点 continue
                if "auth.openai.com" in cur and i % 10 == 5:
                    for sel in ['button:has-text("Continue")', 'button:has-text("Next")',
                                'button[type="submit"]']:
                        try:
                            b = page.query_selector(sel)
                            if b and b.is_visible():
                                b.click()
                                logger.info(f"[browser-reg] 中转点击: {sel}")
                                break
                        except Exception:
                            # 页面导航时 context destroyed，忽略
                            pass
            if not arrived:
                page.screenshot(path="/tmp/browser_reg_no_chatgpt.png")
                raise RuntimeError(f"未跳转回 chatgpt.com，当前: {page.url[:120]}")

            # [8] 等 JS 初始化完成，取 access_token
            time.sleep(5)
            logger.info("[browser-reg] 拉取 /api/auth/session ...")
            session_info = page.evaluate('''async () => {
                const r = await fetch("/api/auth/session", {credentials: "include"});
                return await r.json();
            }''')
            result["access_token"] = session_info.get("accessToken", "")
            result["id_token"] = session_info.get("idToken", "") if isinstance(session_info, dict) else ""
            logger.info(f"[browser-reg] access_token 长度: {len(result['access_token'])}")

            # [9] 提取 cookies
            all_cookies = ctx.cookies()
            chatgpt_cookies = [c for c in all_cookies if "chatgpt.com" in c.get("domain", "")]
            for c in chatgpt_cookies:
                n = c["name"]
                if n == "__Secure-next-auth.session-token":
                    result["session_token"] = c["value"]
                if n in ("oai-did", "oai-device-id"):
                    result["device_id"] = c["value"]
                if n == "__Host-next-auth.csrf-token":
                    result["csrf_token"] = c["value"].split("|")[0] if "|" in c["value"] else c["value"]
            result["cookie_header"] = "; ".join(
                f"{c['name']}={c['value']}" for c in chatgpt_cookies
            )
            logger.info(
                f"[browser-reg] session_token={'yes' if result['session_token'] else 'no'} "
                f"device_id={result['device_id'][:16]}..."
            )

            # [9.5] 可选：申请加入母号 workspace（session/CPA 模型靠这拿到 k12 套餐）。
            #        浏览器内执行以过 Cloudflare；join 后重取 session（AT 可能刷新）。
            if join_workspace_id:
                if result.get("via_workspace_select"):
                    # 已注册账号走了 /workspace 选择页 → 已是该 workspace 成员，不用再申请加入
                    result["workspace_joined"] = True
                    logger.info("[browser-reg] 已注册账号(已是成员), 跳过 join")
                else:
                    result["workspace_joined"] = _join_workspace(
                        page, join_workspace_id, result["access_token"], result["device_id"], logger.info
                    )
                # step 5：切到 k12 workspace 并取 k12-scoped token（个人空间 token 会被 Codex 端 401）
                scoped = _switch_workspace_and_get_token(
                    page, join_workspace_id, result["access_token"], logger.info
                )
                if scoped.get("access_token"):
                    result["access_token"] = scoped["access_token"]
                    result["id_token"] = scoped.get("id_token") or result["id_token"]
                result["chatgpt_account_id"] = scoped.get("account_id") or ""
                result["workspace_scoped"] = (scoped.get("account_id") == join_workspace_id)
                logger.info(
                    f"[browser-reg] workspace_scoped={result['workspace_scoped']} "
                    f"account_id={result['chatgpt_account_id'][:8]}"
                )

            # [10] 可选：同会话跑 codex 授权拿 refresh_token（复刻 sub2api OAuth 添加账号）
            if oauth_session is not None:
                cap = _codex_authorize_capture(ctx, page, oauth_session, cfg.proxy, logger.info)
                result["add_phone"] = bool(cap.get("add_phone"))
                code = cap.get("code") or ""
                if code:
                    try:
                        from .oauth import exchange_code
                        callback_url = cap.get("url") or (
                            f"{oauth_session.redirect_uri}?code={code}&state={oauth_session.state}"
                        )
                        td = exchange_code(
                            oauth_session, callback_url,
                            user_agent="", proxy=cfg.proxy or "",
                        )
                        result["refresh_token"] = td.get("refresh_token", "") or ""
                        result["oauth_access_token"] = td.get("access_token", "") or ""
                        result["oauth_id_token"] = td.get("id_token", "") or ""
                        logger.info(
                            f"[browser-reg] codex RT: {'OK' if result['refresh_token'] else 'X(无RT)'}"
                        )
                    except Exception as ex:
                        logger.warning(f"[browser-reg] codex code 换 RT 失败: {ex}")
                else:
                    logger.warning(
                        f"[browser-reg] codex 授权未拿到 code (add_phone={result['add_phone']})"
                    )

            # CPA/session 导入只需要 access_token（sub2api 忽略 session_token）；
            # 仅在非 join 流程(需要 session cookie 的老路径)才强制要求 session_token。
            need_session_token = not join_workspace_id
            if not result["access_token"] or (need_session_token and not result["session_token"]):
                try:
                    page.screenshot(path="/tmp/browser_reg_missing_token.png")
                except Exception:
                    pass
                raise RuntimeError(
                    f"缺少凭证: access_token={bool(result['access_token'])} "
                    f"session_token={bool(result['session_token'])}"
                )
    finally:
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

    return result


def _first_visible(page, selectors):
    for sel in selectors:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    return el
        except Exception:
            continue
    return None


def _robust_fill(el, value):
    try:
        el.fill(value, timeout=8000)
        return
    except Exception:
        pass
    el.evaluate(
        """(node, v) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(node, v);
            node.dispatchEvent(new Event('input', {bubbles: true}));
            node.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        value,
    )


def _robust_click(page, el, log, label=""):
    try:
        el.click(timeout=5000)
        return True
    except Exception:
        pass
    try:
        el.click(timeout=3000, force=True)
        return True
    except Exception:
        pass
    try:
        el.evaluate("node => node.click()")
        return True
    except Exception:
        pass
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def browser_oauth_signup(cfg, mail_provider, oauth_session=None, auth_url=None, exchange=True) -> dict:
    """真浏览器从 codex authorize URL 直接签注 —— 复刻 sub2api「OAuth 添加账号」。

    在 auth.openai.com 内完成 邮箱→密码(Turnstile 真浏览器过)→邮箱 OTP→姓名/年龄→
    consent，拦截 localhost:1455 回调，抓 code + state。

    两种用法：
      * 本地自换 (exchange=True, 传 oauth_session)：用 oauth_session.auth_url，浏览器拿到
        code 后本地 exchange_code 换 refresh_token。
      * sub2api 主导 (传 auth_url, exchange=False)：用 sub2api 生成的 auth_url(PKCE 在
        sub2api 侧)，只回 code + state，由调用方贴回 sub2api 的 create-from-oauth。

    cfg.proxy / mail_provider(create_mailbox + wait_for_otp [+ last_persona])。
    返回 {email,password,code,state,refresh_token,oauth_access_token,add_phone,...}。
    """
    from urllib.parse import urlparse, parse_qs

    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen

    email = mail_provider.create_mailbox()
    persona = getattr(mail_provider, "last_persona", None)
    if persona is not None:
        password, first_name, last_name = persona.password, persona.first, persona.last
    else:
        base = email.split("@")[0]
        password = (base[::-1] + "Aa9!")[:24]
        if len(password) < 10:
            password = f"{base}Aa9!2026"
        first_name, last_name = _gen_name()
    import datetime as _dt
    _age = random.randint(26, 40)
    byear = _dt.datetime.now().year - _age
    birthday_iso = f"{byear}-01-15"
    birthday_us = f"01/15/{byear}"
    age_str = str(_age)
    full_name = f"{first_name} {last_name}"
    logger.info(f"[oauth-signup] 账号: {email} / {password} | {full_name} {birthday_iso}")

    cf_proxy = _parse_proxy(cfg.proxy)
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    tmp_profile = tempfile.mkdtemp(prefix="chatgpt_oauth_")

    result = {
        "email": email, "password": password, "first_name": first_name, "last_name": last_name,
        "code": "", "state": "",
        "refresh_token": "", "oauth_access_token": "", "oauth_id_token": "",
        "add_phone": False, "device_id": "", "cookie_header": "",
    }
    captured = {"code": "", "url": "", "state": ""}

    # 目标授权 URL：优先调用方传入的 auth_url(sub2api 生成)，否则用本地 oauth_session。
    target_url = str(auth_url or (oauth_session.auth_url if oauth_session is not None else "") or "")
    if target_url and "screen_hint=" not in target_url:
        target_url += ("&" if "?" in target_url else "?") + "screen_hint=signup"
    if not target_url:
        raise RuntimeError("browser_oauth_signup: 需要 auth_url 或 oauth_session")

    def _extract(url: str) -> bool:
        if url and "localhost:1455" in url and "code=" in url:
            q = parse_qs(urlparse(url).query)
            captured["url"] = url
            captured["code"] = (q.get("code") or [""])[0]
            captured["state"] = (q.get("state") or [""])[0]
            return bool(captured["code"])
        return False

    def _on_route(route):
        try:
            _extract(route.request.url)
        except Exception:
            pass
        try:
            route.fulfill(status=200, content_type="text/html", body="ok")
        except Exception:
            try:
                route.abort()
            except Exception:
                pass

    try:
        with Camoufox(
            headless=not has_display, humanize=True, persistent_context=True,
            user_data_dir=tmp_profile, os="windows",
            screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy, geoip=True, locale="en-US",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                ctx.route("http://localhost:1455/**", _on_route)
            except Exception as ex:
                logger.warning(f"[oauth-signup] route 注册失败: {ex}")

            logger.info("[oauth-signup] 导航 codex authorize ...")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.info(f"[oauth-signup] authorize 导航返回: {str(e)[:80]}")

            email_done = pwd_done = otp_done = about_done = False
            otp_sent_at = time.time()
            try:
                _budget = max(60, int(os.getenv("OAUTH_DEADLINE_SECONDS", "150")))
            except ValueError:
                _budget = 150
            deadline = time.time() + _budget
            last_state = ""
            while time.time() < deadline and not captured["code"]:
                cur = ""
                try:
                    cur = page.url
                except Exception:
                    pass
                if _extract(cur):
                    break
                low = cur.lower()
                if "add-phone" in low or "/phone" in low:
                    logger.warning(f"[oauth-signup] 撞到 add-phone: {cur[:100]}")
                    result["add_phone"] = True
                    break
                if cur != last_state:
                    logger.info(f"[oauth-signup] URL: {cur[:110]}")
                    last_state = cur

                # 账号创建被拒(域名/限速配额)会在密码页就地报错且不前进；早退,别空等满超时。
                if pwd_done and not otp_done and "create-account" in low:
                    try:
                        body_txt = (page.inner_text("body") or "")[:3000].lower()
                    except Exception:
                        body_txt = ""
                    if ("failed to create account" in body_txt or "创建帐户失败" in body_txt
                            or "创建账户失败" in body_txt or "account_creation_failed" in body_txt):
                        logger.warning("[oauth-signup] 页面报 account_creation_failed(域名/限速配额),早退")
                        result["error"] = "account_creation_failed"
                        break

                # 1) 邮箱
                if not email_done:
                    ein = _first_visible(page, ['input[type="email"]', 'input[name="email"]',
                                                'input[autocomplete="username"]'])
                    if ein:
                        _robust_fill(ein, email)
                        time.sleep(0.5)
                        btn = _first_visible(page, ['button[type="submit"]', 'button:has-text("Continue")',
                                                    'button:has-text("Next")'])
                        if btn:
                            _robust_click(page, btn, logger.info, "email")
                        else:
                            page.keyboard.press("Enter")
                        logger.info("[oauth-signup] 邮箱已提交")
                        email_done = True
                        time.sleep(3)
                        continue

                # 2) 密码（create-account/password，Turnstile 真浏览器过）
                if not pwd_done:
                    pin = _first_visible(page, ['input[type="password"]', 'input[name="password"]',
                                                'input[name="new-password"]'])
                    if pin:
                        _robust_fill(pin, password)
                        time.sleep(0.6)
                        btn = _first_visible(page, ['button[type="submit"]', 'button:has-text("Continue")',
                                                    'button:has-text("Create")', 'button:has-text("Next")'])
                        if btn:
                            _robust_click(page, btn, logger.info, "password")
                        else:
                            page.keyboard.press("Enter")
                        logger.info("[oauth-signup] 密码已提交，等 Turnstile/OTP ...")
                        pwd_done = True
                        otp_sent_at = time.time()
                        time.sleep(4)
                        continue

                # 3) 邮箱 OTP
                if not otp_done:
                    otp_single = _first_visible(page, ['input[autocomplete="one-time-code"]',
                                                       'input[name="code"]',
                                                       'input[inputmode="numeric"]:not([maxlength="1"])'])
                    otp_digits = page.query_selector_all('input[maxlength="1"][inputmode="numeric"]')
                    if otp_single or (otp_digits and len(otp_digits) >= 6):
                        logger.info("[oauth-signup] 等 CloudMail OTP ...")
                        try:
                            otp_timeout = max(60, int(os.getenv("OTP_TIMEOUT", "180")))
                        except Exception:
                            otp_timeout = 180
                        code = mail_provider.wait_for_otp(email, timeout=otp_timeout, issued_after=otp_sent_at)
                        if not code:
                            logger.warning("[oauth-signup] 未收到 OTP")
                            time.sleep(3)
                            continue
                        logger.info(f"[oauth-signup] OTP={code}")
                        if otp_single:
                            _robust_fill(otp_single, code)
                        else:
                            for i, ch in enumerate(code[:6]):
                                try:
                                    otp_digits[i].click(); otp_digits[i].fill(ch)
                                except Exception:
                                    pass
                        time.sleep(0.8)
                        btn = _first_visible(page, ['button[type="submit"]', 'button:has-text("Continue")',
                                                    'button:has-text("Verify")', 'button:has-text("Next")'])
                        if btn:
                            _robust_click(page, btn, logger.info, "otp")
                        otp_done = True
                        time.sleep(4)
                        continue

                # 4) about-you：姓名 + 生日
                if not about_done and ("about-you" in low or "about_you" in low):
                    metas = []
                    try:
                        metas = page.evaluate("""() => Array.from(document.querySelectorAll('input')).map((el,idx)=>{
                            const r=el.getBoundingClientRect(); const cs=getComputedStyle(el);
                            return {idx, type:(el.type||'').toLowerCase(), name:el.name||'',
                                    ph:el.placeholder||'', al:el.getAttribute('aria-label')||'',
                                    vis:(r.width>0&&r.height>0&&cs.visibility!=='hidden'&&cs.display!=='none')};
                        })""") or []
                    except Exception:
                        pass
                    vis = [m for m in metas if m["vis"] and m["type"] not in
                           ("hidden", "submit", "button", "checkbox", "radio", "password")]

                    def _blob(m):
                        return " ".join([m.get("type", ""), m.get("name", ""),
                                         m.get("ph", ""), m.get("al", "")]).lower()

                    def _is_bd(m):
                        b = _blob(m)
                        return m.get("type") == "date" or any(k in b for k in ("birth", "dob", "mm/dd", "mm / dd"))

                    def _is_name(m):
                        return "name" in _blob(m)

                    def _is_age(m):
                        b = _blob(m)
                        return m.get("type") == "number" or "age" in b

                    # 姓名框：优先 name 关键字，否则第一个可见框
                    name_m = next((m for m in vis if _is_name(m)), None) or (vis[0] if vis else None)
                    # 第二字段：生日(date) 或 年龄(number/age)。新版 OpenAI 多为 "Age" 数字框。
                    bd_m = next((m for m in vis if m is not name_m and _is_bd(m)), None)
                    age_m = next((m for m in vis if m is not name_m and _is_age(m)), None)
                    if bd_m is None and age_m is None:
                        # 兜底：除姓名外的下一个可见框，默认按年龄数字填
                        age_m = next((m for m in vis if m is not name_m), None)

                    els = page.query_selector_all('input')

                    def _fill_idx(meta, text, typed=True):
                        if meta is None:
                            return
                        try:
                            el = els[meta["idx"]]
                            el.focus()
                            try:
                                page.keyboard.press("Control+A"); page.keyboard.press("Delete")
                            except Exception:
                                pass
                            if typed:
                                page.keyboard.type(text, delay=random.randint(30, 70))
                            else:
                                _robust_fill(el, text)
                        except Exception:
                            try:
                                _robust_fill(els[meta["idx"]], text)
                            except Exception:
                                pass

                    _fill_idx(name_m, full_name)
                    if bd_m is not None:
                        if bd_m.get("type") == "date":
                            _fill_idx(bd_m, birthday_iso, typed=False)
                        else:
                            _fill_idx(bd_m, birthday_us)
                    elif age_m is not None:
                        _fill_idx(age_m, age_str)   # 关键修复：年龄数字框之前没填，被卡 "Enter a valid age"
                    logger.info(
                        f"[oauth-signup] about-you: name={'Y' if name_m else 'N'} "
                        f"bd={'Y' if bd_m else 'N'} age={'Y' if (age_m and not bd_m) else 'N'}"
                    )
                    time.sleep(0.8)
                    btn = _first_visible(page, ['button:has-text("Finish")', 'button:has-text("Create")',
                                                'button:has-text("Agree")', 'button:has-text("Continue")',
                                                'button[type="submit"]'])
                    if btn:
                        _robust_click(page, btn, logger.info, "about-you")
                        logger.info("[oauth-signup] about-you 已提交")
                    about_done = True
                    time.sleep(4)
                    continue

                # 5) consent / 工作区 / 其它继续
                btn = _first_visible(page, ['button:has-text("Authorize")', 'button:has-text("Allow")',
                                            'button:has-text("Allow access")', 'button:has-text("Confirm")',
                                            'button:has-text("Continue")', 'button:has-text("Agree")',
                                            'button[type="submit"]'])
                if btn:
                    _robust_click(page, btn, logger.info, "consent")
                time.sleep(2)

            # 始终回传 code + state（sub2api 主导模式靠它贴回 create-from-oauth）
            result["code"] = captured["code"]
            result["state"] = captured["state"]

            if captured["code"]:
                logger.info("[oauth-signup] 抓到 callback code")
                if exchange and oauth_session is not None:
                    logger.info("[oauth-signup] 本地 exchange 换 refresh_token ...")
                    try:
                        from .oauth import exchange_code
                        td = exchange_code(oauth_session, captured["url"], user_agent="", proxy=cfg.proxy or "")
                        result["refresh_token"] = td.get("refresh_token", "") or ""
                        result["oauth_access_token"] = td.get("access_token", "") or ""
                        result["oauth_id_token"] = td.get("id_token", "") or ""
                        logger.info(f"[oauth-signup] codex RT: {'OK' if result['refresh_token'] else 'X(无RT)'}")
                    except Exception as ex:
                        logger.warning(f"[oauth-signup] code 换 RT 失败: {ex}")
            else:
                logger.warning(f"[oauth-signup] 未拿到 code (add_phone={result['add_phone']})")
                try:
                    page.screenshot(path="/tmp/oauth_signup_no_code.png")
                except Exception:
                    pass

            try:
                for c in ctx.cookies():
                    if c.get("name") in ("oai-did", "oai-device-id"):
                        result["device_id"] = c.get("value", "")
            except Exception:
                pass
    finally:
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

    return result
