# rg-gpt

并发批量注册 ChatGPT 账号(codex OAuth),拿 refresh_token,直接写入 sub2api 网关当活号。

## 原理(sub2api 主导闭环)

1. `sub2api` `POST /api/v1/admin/openai/generate-auth-url` → `{auth_url, session_id}`
   PKCE(state/code_challenge/code_verifier)全在 sub2api 侧 session。
2. 真浏览器(Camoufox)走 `auth_url`,在 `auth.openai.com` 内完成
   邮箱 → 密码(真浏览器过 Turnstile) → 邮箱 OTP → 姓名/年龄 → consent,
   拦截 `localhost:1455` 回调,抓 `code` + `state`(本地不 exchange)。
3. `sub2api` `POST /api/v1/admin/openai/create-from-oauth` `{session_id, code, state, group_ids}`
   → sub2api 用 session 内 code_verifier 自换 RT、建号、绑分组。**RT 直接进账号库,不经过本地。**

关键点:`screen_hint=signup` 进注册分支;真浏览器原生过 Turnstile(协议路子拿不到 `so` 必死);
同一会话内"签注→授权"一气呵成 → 跳过 add_phone。

## 本地跑

```bash
python -m venv .venv && . .venv/Scripts/activate   # win: .\.venv\Scripts\activate
pip install -r requirements.txt
python -m camoufox fetch
# 配置见下方环境变量(或写 .env，本地 settings 不读 .env，请用真实环境变量)
python register_sub2api_oauth.py 1
```

## GitHub Actions 并发

`Actions → register-accounts → Run workflow`(可填每个 job 注册数量,默认 1)。
matrix 固定 40 个并发 job(受 GitHub 套餐并发上限排队)。裸跑 GitHub(Azure)IP。

### 必需 Secrets

| Secret | 说明 |
|---|---|
| `CLOUDMAIL_BASE_URL` | CloudMail 管理 API 根 |
| `CLOUDMAIL_PASSWORD` | CloudMail admin 密码 |
| `CLOUDMAIL_DOMAIN` | 临时邮箱域(如 `@edu.xjy.hidns.vip`) |
| `SUB2API_URL` | sub2api 网关根 |
| `SUB2API_EMAIL` | sub2api 管理员邮箱 |
| `SUB2API_PASSWORD` | sub2api 管理员密码 |
| `SUB2API_GROUP` | 绑定分组名或 id(如 `额度`) |

可选 `VERIFY_PROXY`(浏览器代理;留空=裸跑)。

> ⚠️ GitHub 是 Azure 机房 IP,OpenAI 风控可能触发 add_phone 或拦截。若失败,
> 给 `VERIFY_PROXY` 挂一个住宅/美国代理(改 workflow env 用 secret 注入)。
