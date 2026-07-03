// ==UserScript==
// @name         ChatGPT Workspace -> sub2api 一键导入
// @namespace    https://chatgpt.com/
// @version      5.0.0
// @description  子号手动登录后，对每个 team workspace：申请加入(request) -> 切换到该 workspace 命名空间 -> 取 workspace-scoped session -> 以 CPA(personalAccessToken) 格式导入 sub2api。基于 register_workspace.py 的同一套逻辑。
// @author       you
// @match        https://chatgpt.com/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      api.xjy.de5.net
// ==/UserScript==

(function () {
  "use strict";

  // ===================== 配置 =====================
  const DEFAULTS = {
    // 要加入 + 导入的 team workspace（逗号/换行分隔）
    workspaceIds: "5e4c9b31-1b4e-4887-839b-607597928d7c,a0a16bc9-e1b1-45f0-b269-812b53f60121",
    // sub2api 网关 + 管理员登录 + 售卖分组(可空)
    sub2apiUrl: "https://api.xjy.de5.net",
    sub2apiEmail: "admin@sub2api.local",
    sub2apiPassword: "Xiejiayi@123",
    sub2apiGroup: "4,15",           // 分组 id 或名; 空则不绑组
    concurrency: 3,
    intervalMs: 1500,
    maxRetries: 3,
    retryBackoffMs: 5000,
    sessionPollMs: 20000,
    panelWidth: 440,
  };

  const STORE_KEY = "jr_config_v5";
  function loadConfig() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (_) {}
    return Object.assign({}, DEFAULTS, saved);
  }
  function saveConfig(cfg) { try { localStorage.setItem(STORE_KEY, JSON.stringify(cfg)); } catch (_) {} }
  let CONFIG = loadConfig();

  const STATE = { at: "", session: null, email: "", deviceId: crypto.randomUUID(), running: false };

  // ===================== ChatGPT session =====================
  async function fetchSession(query) {
    const res = await fetch("/api/auth/session" + (query || ""), { headers: { accept: "*/*" }, credentials: "include" });
    if (!res.ok) throw new Error(`session HTTP ${res.status}`);
    return res.json();
  }

  function decodeJwt(at) {
    try {
      const p = at.split(".")[1];
      const j = JSON.parse(atob(p.replace(/-/g, "+").replace(/_/g, "/")));
      const auth = j["https://api.openai.com/auth"] || {};
      const prof = j["https://api.openai.com/profile"] || {};
      return { account_id: auth.chatgpt_account_id || "", email: prof.email || "",
               plan_type: auth.chatgpt_plan_type || "", exp: j.exp || 0 };
    } catch (_) { return {}; }
  }

  async function refreshSession() {
    try {
      const s = await fetchSession();
      const at = s.accessToken || "";
      STATE.session = s;
      if (at) { STATE.at = at; STATE.email = decodeJwt(at).email || STATE.email;
                updateUserBar(decodeJwt(at), "ok"); }
      else updateUserBar(null, "warn");
    } catch (e) { log(`session 获取失败: ${e.message}`, "warn"); updateUserBar(null, "err"); }
  }

  // 切换到指定 workspace 命名空间, 返回该 workspace-scoped 的 {accessToken, idToken}
  // 机制来自实测抓包: GET /api/auth/session?exchange_workspace_token=true&workspace_id=..&reason=setCurrentAccount
  async function exchangeWorkspaceToken(wsId) {
    const q = `?exchange_workspace_token=true&workspace_id=${encodeURIComponent(wsId)}&reason=setCurrentAccount`;
    let s = await fetchSession(q);
    let at = s.accessToken || "";
    if (!at || decodeJwt(at).account_id !== wsId) {
      // 兜底: 再普通取一次 session
      s = await fetchSession();
      at = s.accessToken || at;
    }
    return { accessToken: at, idToken: s.idToken || "", account_id: decodeJwt(at).account_id };
  }

  // ===================== 加入 workspace (request/accept) =====================
  async function sendInvite(wsId, route, attempt) {
    attempt = attempt || 0;
    const url = `/backend-api/accounts/${wsId}/invites/${route}`;
    const headers = { accept: "*/*", authorization: "Bearer " + STATE.at, "content-type": "application/json",
                      "oai-device-id": STATE.deviceId, "oai-language": navigator.language || "en-US" };
    try {
      const res = await fetch(url, { method: "POST", headers, body: "", mode: "cors", credentials: "include" });
      const text = await res.text();
      if (res.ok) { log(`✓ join ${wsId.slice(0,8)} HTTP ${res.status}`, "ok"); return true; }
      // 已是成员 / 已申请, 视作成功继续
      if (/already|member|exists|pending/i.test(text)) { log(`· join ${wsId.slice(0,8)} 已是成员/已申请`, "ok"); return true; }
      log(`✗ join ${wsId.slice(0,8)} HTTP ${res.status}: ${text.slice(0,120)}`, "warn");
      if ((res.status === 401 || res.status === 403)) { STATE.at = ""; await refreshSession(); }
      if (attempt < CONFIG.maxRetries) { await sleep(CONFIG.retryBackoffMs * (attempt + 1)); return sendInvite(wsId, route, attempt + 1); }
      return false;
    } catch (e) {
      log(`join 网络错误: ${e.message}`, "err");
      if (attempt < CONFIG.maxRetries) { await sleep(CONFIG.retryBackoffMs); return sendInvite(wsId, route, attempt + 1); }
      return false;
    }
  }

  // ===================== sub2api (跨域 -> GM_xmlhttpRequest) =====================
  function gmReq(opts) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest(Object.assign({ timeout: 30000, onload: resolve, onerror: () => reject(new Error("net")), ontimeout: () => reject(new Error("timeout")) }, opts));
    });
  }
  function subUrl(path) { return CONFIG.sub2apiUrl.replace(/\/+$/, "") + path; }

  async function sub2apiLogin() {
    const r = await gmReq({ method: "POST", url: subUrl("/api/v1/auth/login"),
      headers: { "content-type": "application/json" }, data: JSON.stringify({ email: CONFIG.sub2apiEmail, password: CONFIG.sub2apiPassword }) });
    if (r.status >= 300) throw new Error(`sub2api 登录失败 HTTP ${r.status}: ${(r.responseText||"").slice(0,120)}`);
    const data = (JSON.parse(r.responseText).data) || {};
    if (data.requires_2fa) throw new Error("sub2api 登录要 2FA, 脚本不支持");
    if (!data.access_token) throw new Error("sub2api 登录无 access_token");
    return data.access_token;
  }

  function buildCpaPayload(email, wsId, accessToken, idToken) {
    const creds = { access_token: accessToken, chatgpt_account_id: wsId,
                    auth_mode: "personalAccessToken", token_type: "Bearer", email: email };
    if (idToken) creds.id_token = idToken;
    return { data: { exported_at: new Date().toISOString(), proxies: [],
      accounts: [{ name: `${email}#${wsId.slice(0,8)}`, platform: "openai", type: "oauth",
                   credentials: creds, concurrency: CONFIG.concurrency, priority: 0 }] },
      skip_default_group_bind: true };
  }

  async function sub2apiImport(token, payload) {
    const r = await gmReq({ method: "POST", url: subUrl("/api/v1/admin/accounts/data"),
      headers: { "content-type": "application/json", authorization: "Bearer " + token }, data: JSON.stringify(payload) });
    return r;
  }

  // 续期/去重: 按 name 查已存在账号, 有则更新凭证(不重复建), 无则导入; 再可选绑组
  async function sub2apiUpsert(token, email, wsId, accessToken, idToken) {
    const name = `${email}#${wsId.slice(0,8)}`;
    const creds = buildCpaPayload(email, wsId, accessToken, idToken).data.accounts[0].credentials;
    const existId = await sub2apiFindId(token, name);
    if (existId) {
      const r = await gmReq({ method: "PUT", url: subUrl(`/api/v1/admin/accounts/${existId}`),
        headers: { "content-type": "application/json", authorization: "Bearer " + token }, data: JSON.stringify({ credentials: creds }) });
      if (r.status >= 300) throw new Error(`更新失败 HTTP ${r.status}: ${(r.responseText||"").slice(0,120)}`);
      await sub2apiBindGroup(token, existId);
      return { id: existId, stage: "renewed" };
    }
    const r = await sub2apiImport(token, buildCpaPayload(email, wsId, accessToken, idToken));
    if (r.status >= 300) throw new Error(`导入失败 HTTP ${r.status}: ${(r.responseText||"").slice(0,120)}`);
    const id = await sub2apiFindId(token, name);
    await sub2apiBindGroup(token, id);
    return { id: id, stage: "imported" };
  }

  async function sub2apiFindId(token, name) {
    try {
      const r = await gmReq({ method: "GET", url: subUrl(`/api/v1/admin/accounts?platform=openai&search=${encodeURIComponent(name)}&page_size=5`),
        headers: { authorization: "Bearer " + token } });
      if (r.status >= 300) return "";
      const d = (JSON.parse(r.responseText).data) || {};
      const items = d.accounts || d.items || d.list || [];
      for (const it of items) if ((it.name || "").toLowerCase() === name.toLowerCase()) return String(it.id || "");
      return items.length ? String(items[0].id || "") : "";
    } catch (_) { return ""; }
  }

  async function sub2apiBindGroup(token, id) {
    // 支持多个分组: "4,15" / "4 15" -> [4,15]
    const gids = String(CONFIG.sub2apiGroup || "").split(/[\s,]+/).map(s => parseInt(s, 10)).filter(n => n > 0);
    if (!id || !gids.length) return;
    try {
      await gmReq({ method: "PUT", url: subUrl(`/api/v1/admin/accounts/${id}`),
        headers: { "content-type": "application/json", authorization: "Bearer " + token },
        data: JSON.stringify({ group_ids: gids, confirm_mixed_channel_risk: true }) });
      log(`  绑组 ${gids.join(",")} (acct ${id})`, "ok");
    } catch (_) {}
  }

  // ===================== 主流程: 每个 workspace 加入 + 切换 + 导入 =====================
  function parseWorkspaceIds() { return CONFIG.workspaceIds.split(/[\n,]+/).map(s => s.trim()).filter(Boolean); }

  async function runJoinImport() {
    if (STATE.running) { log("正在运行, 稍候", "warn"); return; }
    await refreshSession();
    if (!STATE.at) { log("无 AT, 请先登录 chatgpt.com", "err"); return; }
    const email = STATE.email || decodeJwt(STATE.at).email;
    if (!email) { log("拿不到 email", "err"); return; }
    const ids = parseWorkspaceIds();
    if (!ids.length) { log("未配置 workspace", "err"); return; }

    STATE.running = true; setBtns(false);
    let token;
    try { log("sub2api 登录 ...", "info"); token = await sub2apiLogin(); log("sub2api 登录 OK", "ok"); }
    catch (e) { log("" + e.message, "err"); STATE.running = false; setBtns(true); return; }

    let ok = 0;
    for (const ws of ids) {
      try {
        log(`=== workspace ${ws.slice(0,8)} ===`, "info");
        // 1) 加入(已是成员也 OK)
        await sendInvite(ws, "request");
        await sleep(CONFIG.intervalMs);
        // 2) 切换命名空间 + 取 scoped session
        const sc = await exchangeWorkspaceToken(ws);
        if (!sc.accessToken || sc.account_id !== ws) {
          log(`✗ ${ws.slice(0,8)} token 未切到该 workspace (${sc.account_id || "?"})`, "warn"); continue;
        }
        log(`切到 workspace, token scoped ✓`, "ok");
        // 3) 导入/续期 sub2api
        const res = await sub2apiUpsert(token, email, ws, sc.accessToken, sc.idToken);
        log(`✓ ${res.stage} -> sub2api acct ${res.id} (${ws.slice(0,8)})`, "ok");
        ok++;
      } catch (e) { log(`✗ ${ws.slice(0,8)}: ${e.message}`, "err"); }
      await sleep(CONFIG.intervalMs);
    }
    // 收尾: 切回让 session 正常
    try { await fetchSession(); } catch (_) {}
    log(`完成: ${ok}/${ids.length} 导入 sub2api`, ok === ids.length ? "ok" : "warn");
    STATE.running = false; setBtns(true);
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ===================== UI =====================
  let panelBody, userBarEl, runBtnEl, wsInputEl, cfgInputs = {}, dirty = false, saveBtnEl;
  function setBtns(en) { if (runBtnEl) { runBtnEl.disabled = !en; runBtnEl.style.opacity = en ? "1" : "0.5"; } }
  function fmtExp(exp) { if (!exp) return "?"; const m = Math.round((exp*1000 - Date.now())/60000); return m>60?`剩 ${Math.round(m/60)}h`:`剩 ${m}m`; }
  function updateUserBar(info, status) {
    if (!userBarEl) return;
    const c = { ok: "#2f855a", warn: "#b7791f", err: "#c53030" };
    if (info && info.email) userBarEl.innerHTML = `<span style="color:${c.ok}">●</span> <b>${info.email}</b> · ${info.plan_type||"?"} · <code style="background:#edf2f7;padding:1px 4px;border-radius:3px">${(info.account_id||"").slice(0,8)}</code> · <span style="color:#718096">${fmtExp(info.exp)}</span>`;
    else userBarEl.innerHTML = `<span style="color:${c[status]||c.warn}">●</span> ${status==="err"?"session 失败, 请确认已登录":"等待登录..."}`;
  }
  function markDirty(){ dirty=true; if(saveBtnEl){saveBtnEl.textContent="保存 *";saveBtnEl.style.background="#d69e2e";saveBtnEl.style.color="#fff";} }

  function buildPanel() {
    const css = `.jr-panel{position:fixed;top:14px;right:14px;width:${CONFIG.panelWidth}px;background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 12px 32px rgba(0,0,0,.16);z-index:99999;font:13px/1.5 -apple-system,Segoe UI,sans-serif;color:#1a202c;overflow:hidden}
.jr-head{padding:11px 16px;background:linear-gradient(135deg,#3182ce,#2b6cb0);color:#fff;display:flex;justify-content:space-between;align-items:center;font-weight:600}
.jr-sub{padding:8px 16px;background:#f7fafc;border-bottom:1px solid #edf2f7;font-size:12px;color:#4a5568;min-height:20px}
.jr-sec{padding:10px 16px;border-bottom:1px solid #edf2f7}
.jr-label{font-size:11px;color:#718096;margin-bottom:4px;font-weight:600;text-transform:uppercase}
.jr-in{width:100%;box-sizing:border-box;border:1px solid #e2e8f0;border-radius:7px;padding:6px 9px;font:12px monospace;margin-bottom:6px}
.jr-ta{width:100%;box-sizing:border-box;border:1px solid #e2e8f0;border-radius:7px;padding:7px 9px;font:12px monospace;min-height:48px;resize:vertical}
.jr-row{display:flex;gap:6px}.jr-row .jr-in{flex:1}
.jr-body{padding:8px 16px;max-height:34vh;overflow:auto;font-size:12px}
.jr-foot{padding:10px 16px;border-top:1px solid #edf2f7;display:flex;gap:8px;justify-content:flex-end}
.jr-line{padding:2px 0;word-break:break-all;border-bottom:1px dashed #f1f5f9}
.jr-info{color:#2b6cb0}.jr-ok{color:#2f855a}.jr-warn{color:#b7791f}.jr-err{color:#c53030}
.jr-btn{cursor:pointer;border:0;border-radius:7px;padding:8px 16px;font-size:13px;font-weight:600}
.jr-btn-primary{background:#1f883d;color:#fff}.jr-btn-ghost{background:#edf2f7;color:#4a5568}`;
    const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
    const p = document.createElement("div"); p.className = "jr-panel";
    p.innerHTML = `<div class="jr-head"><span>Workspace → sub2api</span><span style="font-size:11px;opacity:.8">v5.0</span></div>
<div class="jr-sub" id="jr-user">等待登录...</div>
<div class="jr-sec">
  <div class="jr-label">Team workspace ID (逗号/换行分隔)</div>
  <textarea class="jr-ta" id="jr-ws"></textarea>
  <div class="jr-label" style="margin-top:8px">sub2api</div>
  <input class="jr-in" id="jr-url" placeholder="https://api.xjy.de5.net">
  <div class="jr-row"><input class="jr-in" id="jr-email" placeholder="admin email"><input class="jr-in" id="jr-pass" type="password" placeholder="password"></div>
  <input class="jr-in" id="jr-group" placeholder="group id 可多个: 4,15 (可空)">
  <div style="display:flex;justify-content:flex-end"><button class="jr-btn jr-btn-ghost" id="jr-save" style="padding:5px 12px">保存配置</button></div>
</div>
<div class="jr-body" id="jr-body"></div>
<div class="jr-foot"><button class="jr-btn jr-btn-ghost" id="jr-refresh">刷新 AT</button><button class="jr-btn jr-btn-primary" id="jr-run">加入 + 导入 sub2api</button></div>`;
    document.body.appendChild(p);
    panelBody = p.querySelector("#jr-body"); userBarEl = p.querySelector("#jr-user");
    runBtnEl = p.querySelector("#jr-run"); wsInputEl = p.querySelector("#jr-ws"); saveBtnEl = p.querySelector("#jr-save");
    cfgInputs = { url: p.querySelector("#jr-url"), email: p.querySelector("#jr-email"), pass: p.querySelector("#jr-pass"), group: p.querySelector("#jr-group") };
    wsInputEl.value = CONFIG.workspaceIds; cfgInputs.url.value = CONFIG.sub2apiUrl;
    cfgInputs.email.value = CONFIG.sub2apiEmail; cfgInputs.pass.value = CONFIG.sub2apiPassword; cfgInputs.group.value = CONFIG.sub2apiGroup;
    [wsInputEl, cfgInputs.url, cfgInputs.email, cfgInputs.pass, cfgInputs.group].forEach(el => el.addEventListener("input", markDirty));
    saveBtnEl.addEventListener("click", () => {
      CONFIG.workspaceIds = wsInputEl.value; CONFIG.sub2apiUrl = cfgInputs.url.value.trim();
      CONFIG.sub2apiEmail = cfgInputs.email.value.trim(); CONFIG.sub2apiPassword = cfgInputs.pass.value;
      CONFIG.sub2apiGroup = cfgInputs.group.value.trim(); saveConfig(CONFIG);
      dirty=false; saveBtnEl.textContent="已保存"; saveBtnEl.style.background="#38a169"; saveBtnEl.style.color="#fff"; log("配置已保存", "ok");
    });
    runBtnEl.addEventListener("click", runJoinImport);
    p.querySelector("#jr-refresh").addEventListener("click", refreshSession);
  }

  function log(msg, level) {
    const cls = { info:"jr-info", ok:"jr-ok", warn:"jr-warn", err:"jr-err" }[level] || "jr-info";
    console.log("%c[WS→sub2api]", "color:#3182ce;font-weight:bold", msg);
    if (panelBody) { const l = document.createElement("div"); l.className = `jr-line ${cls}`;
      l.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`; panelBody.appendChild(l); panelBody.scrollTop = panelBody.scrollHeight; }
  }

  function boot() {
    buildPanel();
    log("v5.0 已加载: 手动登录后点「加入 + 导入 sub2api」", "info");
    log("对每个 workspace: 申请加入 → 切换命名空间 → 取 scoped session → CPA 导入 sub2api", "info");
    refreshSession();
    setInterval(() => { if (!STATE.running) refreshSession(); }, CONFIG.sessionPollMs);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
