# ChatGPT 账号工厂 —— 注册 → 加 workspace → 导入 sub2api

把 Outlook/Gmail 邮箱 → 批量注册/复用 ChatGPT 账号 → 加入 team workspace 拿套餐 →
以 CPA(session) 凭证导入 sub2api。核心组件、机制、以及一堆**花了大力气才搞明白的坑**。

代码入口：
- `register_workspace.py` —— 主 runner（signup/login → join → switch → import）
- `backend/integrations/mail/outlook.py` —— 邮箱读码（Outlook + Gmail，域名路由）
- `backend/integrations/chatgpt/camoufox_register.py` —— Camoufox 浏览器流程
- `pool/app.py` —— 账号池 web 服务（FastAPI+SQLite，部署在 1.94.147.46:8091）
- `outlook/join_import.user.js` —— 油猴脚本（手动登录后一键 join+导入）
- `.github/workflows/register_workspace.yml` —— CI（手动触发，按 mail_kind/workspace 分批）

---

## 0. 一句话数据流

```
pool 租一个邮箱 → Camoufox 打开 chatgpt.com
  ├─ 新号:   signup → 邮箱 OTP → about-you(姓名/年龄) → chatgpt.com
  └─ 已注册: 输邮箱 → 邮箱 OTP → /workspace 选择页 → 选「个人账户」→ chatgpt.com
→ 对每个目标 workspace: 幂等 join(invites/request) → exchange 切换取 scoped token
→ 每个 workspace 导一个 CPA 账号进 sub2api(name=email#wsShort) → 绑组 → 回写 pool
```

一个邮箱 + N 个 workspace = sub2api 里 N 个号（一次注册多导）。

---

## 1. 邮箱读码 —— 密码直连都死了

### Outlook（个人 MSA：@outlook/@hotmail/@live）
密码直连 **全被微软封死**（2024-09）：
- IMAP 基础认证 → `AUTHENTICATE failed`
- ROPC (`grant_type=password`) → `AADSTS9001023`（/consumers 端点根本不支持）

**唯一可行：OAuth2 refresh_token → access_token → IMAP XOAUTH2 读信。**
- 账号格式：`email----password----client_id----refresh_token`（字段顺序自动识别：UUID=client_id，长串=refresh_token）
- client_id 默认 Thunderbird 公共客户端 `9e5f94bc-e8a4-4e73-b8be-63364c29d753`
- 只有 `email----password` 的号：`outlook/bootstrap.py` 走 device-code 登录（**从自己浏览器/住宅IP**，不要在 CI 数据中心 IP 登，会触发"异常登录"锁号）一次性换 refresh_token
- IMAP `outlook.office365.com:993`，读 INBOX + Junk

### Gmail（@gmail.com / @googlemail.com）—— 比 Outlook 简单
Gmail **还留着 App Password**（Outlook 彻底废了，Gmail 没废）：
- 账号格式：`email----app_password`（16 位应用专用密码，去空格）
- 前提：账号开了 **两步验证(2FA)**，然后 myaccount.google.com/apppasswords 生成
- IMAP `imap.gmail.com:993`，**直接 LOGIN**（不用 OAuth），读 INBOX + `[Gmail]/Spam`

### 路由（一个服务两种读法）
`OutlookEmailService` 按**邮箱域名**自动路由：`@gmail.com`→app-password IMAP，其它→XOAUTH2。
混合池能跑。`OutlookAccount.kind` = `"gmail"`/`"outlook"`。

---

## 2. Plus/Dot 子地址 —— 一个信箱开多个号

一个邮箱能开多个 ChatGPT 号，OTP 都进**同一个物理信箱**：
- **Outlook**：`user+1@outlook.com` … `user+5@outlook.com`（plus-addressing）
- **Gmail**：`user+N@` **和** 点号 `u.ser@`=`user@`（Gmail 忽略点）都行
- **OpenAI 限制：一个 base 最多 5 个子邮箱**（`+1`~`+5`），base+5=6 个号/信箱

### 并发安全（关键坑）
多个 `+N` 同时注册 → OTP 全堆一个信箱。**必须按收件人(To 头)过滤**：
- 实测 OTP 邮件的 **To 头保留精确的 `+N`/点号形式**（`looknicemm+1@` vs `loo.knicemm@` 能区分）
- `get_verification_code` 读 base 信箱 → 只认 `To/Cc == 本别名` 的邮件（`_msg_recipients` 只取 To+Cc，**不取 Delivered-To**，因为 Delivered-To 永远是 base 会误配）
- IMAP 登录用 base（`_base_email` 剥掉 `+tag`）；Gmail 点号别名登录也 OK（Gmail 归一化点）

### RT 轮换坑
一个 base 的 N 个别名**共用一个 refresh_token**。并发刷新会轮换冲突。
`OutlookEmailService._base_access_token` **按 base 缓存 access_token**，进程内 N 个别名只刷一次。
跨 CI job 仍可能撞（MSA 有宽限窗口，多数没事，失败标 failed 重试即可）。

---

## 3. 浏览器注册流程（camoufox_register.py）

### about-you 表单有多个变种（都要处理）
- 老版：Full name + Age（数字框）
- 2026-04：Full name + Birthday（`MM/DD/YYYY` 或 native date）
- 2026-07：**"What year were you born?" → 只要 4 位年份**（`Year of birth`，type=number）
  —— 填完整日期会被拒 `Enter a valid year of birth`

### 已注册账号 → /workspace 选择页（关键分支）
已注册的邮箱：输邮箱 → 邮箱 OTP（**无密码 OTP 登录**）→ 落到 `auth.openai.com/workspace`
选择页（不是新号的 `/about-you`）。**这里选「个人账户」**（"Personal account"/"个人帐户"
文字固定、每号都有 → 可靠），进 chatgpt.com，然后走**正常 join**（不要跳过 join —— 号
可能属于别的 org，得真正申请加入目标 workspace）。`skip_about_you` 标记跳过 about-you 表单。

- 若账号是**密码注册**的 → 输邮箱后到 `/log-in/password`，我们没密码 → 走不了（自动流程只做
  signup / 邮箱OTP登录，不做密码登录）。
- `/workspace` 选择：**用精确 workspace_id POST `/api/accounts/workspace/select` + goto，
  或点「个人账户」条目**。曾经用 `page.goto` 撞正在跑的 SPA 点击导航 → FF driver 崩
  (`pageError.location.url`)。原则：**点了就别 goto**（等 SPA 自己导航），**没点才 goto**。

### chatgpt.com 偶发跳 /auth/login
首页有时直接跳 `/auth/login`（没有 "Sign up" 按钮）。兜底：**找到邮箱框就直接进邮箱步骤**
（统一登录页，输邮箱后 OpenAI 自动分流注册/登录）。

---

## 4. sub2api 导入 —— CPA 的真相

从 `account_data.go` 抠出来的硬事实：

- 导入端点 `POST /api/v1/admin/accounts/data`，body：
  `{data:{proxies:[], accounts:[{name, platform:"openai", type:"oauth", credentials:{...}}]}, skip_default_group_bind:true}`
  —— proxies/accounts 必须非 null（空数组）；字段是 `platform`/`type`（不是 platform_type）
- **sub2api 根本不存 session_token / cookie**（OpenAI 的 session_token 被显式忽略）。模型是
  **OAuth: `access_token` + `refresh_token`**
- OpenAI 号的请求走 **`chatgpt.com/backend-api/codex/responses`（Codex 端点）**，`access_token`
  原样当 Bearer + `chatgpt-account-id` 头选 workspace
- **workspace 靠 `credentials.chatgpt_account_id`** —— 网关发 `chatgpt-account-id` 头。无需手动切 UI
- **耐久性**：只有 `access_token` 无 `refresh_token` → 号在 token 过期(几小时~几天)被强制暂停
  = **短命 CPA**（`auth_mode:personalAccessToken`, `token_type:Bearer`）。**重跑刷新**（续期）
  —— 本项目选的就是这条短命路（简单）；durable 那条是 `register_sub2api_oauth.py` 的 OAuth refresh_token 流
- **个人空间 token 会被 Codex 端 401**（`{"detail":"Unauthorized"}`）—— 必须是**目标 workspace
  作用域**的 token。所以有了下面的 exchange 切换 + JWT-scope 门禁

### 续期识别（避免重复建号）
`_find_account_id(name)` 按 name 查；有则 `update_openai_account(id, {credentials})`（renewed），
无则 `import_account_data`（imported）。sub2api 被删/首次 → import；否则 update。

---

## 5. Workspace 机制

### 切换到 workspace 作用域（exchange，实测抓包）
```
GET /api/auth/session?exchange_workspace_token=true&workspace_id=<id>&reason=setCurrentAccount
```
浏览器内 fetch（继承 CF 通过的会话）。响应 accessToken 即该 workspace-scoped。
**验证**：解码 JWT 的 `chatgpt_account_id` claim == 目标 id（`_switch_workspace_and_get_token`）。
不是成员 → 返回 200 但 token 停在当前空间（scope 不变）→ 门禁拦下不导入（否则 401 废号）。

### Join 有域名限制（关键坑）
```
[join] ✗ 401: "Only users with emails on the same domain can request access to a workspace"
```
**某些 workspace 限定邮箱域名** —— outlook 号 join gmail 域的 workspace 会被 401 拒。
所以 **workspace 匹配邮箱后缀**：gmail-workspace 用 gmail 号，outlook-workspace 用 outlook 号。
用 CI 的 `mail_kind` + 对应 `workspace_id` 分批，**别混**。
能自动批准的 workspace：`invites/request` 返回 `200 {"success":true}`。

### 多 workspace（一次多导）
`WORKSPACE_ID` 支持逗号分隔。一个号 join 所有 → 逐个 switch 取 scoped token →
每个导一个独立 sub2api 号（`name=email#<ws前8>`）。实测 3 个 workspace → 3 个号一次搞定。

---

## 6. 账号池（pool/app.py）

FastAPI + SQLite，自包含（只 fastapi+uvicorn）。部署在 `1.94.147.46:8091`，systemd `outlook-pool`。

### 状态机（号被 job 碰过绝不自动回收）
```
available →lease→ leased →成功→ success  |  →失败→ failed  |  →TTL超时→ stale
failed/stale →retry(手动)→ available   |   any →disable→ disabled
```
signup 消耗邮箱不可逆，所以 leased 绝不自动回 available（否则半注册的号重试撞 "email in use"）。

### API（都要 `X-API-Key` 头）
| 端点 | 作用 |
|------|------|
| `POST /api/accounts` | 批量加；母号自动展开 +1~+5，子号不展开（`expand`/`plus` 可调）|
| `POST /api/lease` | 原子租 `{count, leased_by, kind}`；kind=gmail/outlook 只捞对应类型 |
| `GET /api/available?kind=` | 某类型可租数（runner 按此 cap count）|
| `POST /api/accounts/{id}/result` | 回写 success/failed + sub2api id + 轮换后的 RT |
| `POST /api/accounts/{id}/retry` · `/disable` · `DELETE` | |
| `POST /api/reset-all` · `POST /api/expand-plus` | 全部重置 / 现有号补 +5 别名 |
| `GET /api/accounts?status=&search=` | 列表（search=邮箱 LIKE；密码/RT 脱敏）|

UI：搜索框 + 列头点击排序 + 全部重置/展开按钮 + 每行 重置/停用/删。

### kind 过滤原理
gmail = gmail 域 + `password`(app pw)；outlook = 非 gmail + `refresh_token`。
runner 读 `MAIL_KIND` env 传给 lease。

---

## 7. CI（register_workspace.yml）

手动 `workflow_dispatch`，输入：`count_per_job` / `parallel_jobs`(动态矩阵，起几个就几个) /
`mail_kind`(outlook|gmail) / `workspace_id`(逗号分隔，覆盖 secret)。

Secrets：`OUTLOOK_POOL_URL`(=http://1.94.147.46:8091)、`POOL_API_KEY`、`WORKSPACE_ID`、
`SUB2API_*`、`VERIFY_PROXY`。

### Camoufox 坑
- **playwright 必须锁 `==1.60.0`**：1.61.0(2026-06-29) 发 `viewport.isMobile`，camoufox 0.4.11
  的 Linux 浏览器 juggler 不认 → `"not described in this scheme"` 启动崩。camoufox 也锁 `==0.4.11`
- 本地多 Python 版本坑：`pip` 装到 A、`python` 是 B → 用 `python -m pip` 确保同一个
- CI 用 `xvfb-run`（headed，反检测）；本地 headless。Azure runner 直连 IP **实测能过 Turnstile**
  （曾 53 个成功），但住宅代理更稳（`VERIFY_PROXY`）

---

## 8. 部署（服务器 1.94.147.46）

```bash
scp pool/app.py root@1.94.147.46:/opt/outlook-pool/app.py
ssh: systemctl restart outlook-pool
```
- Ubuntu 24.04，用 `python3 -m pip install --break-system-packages`（apt 有锁，venv 也行）
- systemd `ExecStart=/usr/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8091`
- `POOL_API_KEY` 在 systemd unit 里；`POOL_DB=/var/lib/outlook-pool/pool.db`
- **安全**：存密码+RT 的公网明文 HTTP，key 在 header 传会暴露 → 长期用要套 TLS 反代 + 防火墙

---

## 9. 一句话坑清单

- Outlook 密码/IMAP-basic/ROPC 全死 → 只能 RT+XOAUTH2；Gmail app-password 还活着
- sub2api 忽略 session_token，走 Codex 端点，要 access_token(+refresh_token 才耐久)
- 个人空间 token 会 401 → 必须 exchange 切到目标 workspace 作用域 + JWT 门禁
- join 有**同域名限制** → workspace 按邮箱后缀分批，别混
- 一个 base 最多 5 个子邮箱；并发按 To 头过滤 OTP；RT 共用要按 base 缓存 token
- playwright 锁 1.60.0（1.61 崩 camoufox）
- 已注册号走 /workspace → 选个人账户 → 正常 join（别跳过）；密码注册的号自动流程进不去
- leased 号永不自动回收（半注册撞 email in use）；短命 CPA 靠重跑续期
- OTP 提交后落 `account_deactivated` 页 = 号被封 → runner 抛 `AccountDeactivated` →
  pool `report_result("banned")` **硬删该号**（不再租/重试），区别于普通 failed
