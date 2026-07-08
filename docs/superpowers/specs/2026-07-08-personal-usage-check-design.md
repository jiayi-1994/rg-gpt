# 个人号 usage 检查 — 设计

## 目标

新增一条流水线：从账号池租 `available` 邮箱 → 真浏览器注册/登录到 **personal** ChatGPT（**不** join 任何 team workspace）→ 在浏览器内调用
`https://chatgpt.com/backend-api/wham/usage` → 解析 `rate_limit.primary_window.reset_after_seconds` → 写回池的新字段。支持 CI 批量并发。

与现有 `register_workspace.py`（注册→join→导 sub2api）平行；本流程**不碰 sub2api**（个人空间 token 在 Codex 端会 401，无导入意义）。

## 数据流

```
pool lease(available, kind=MAIL_KIND)
  → browser_register(cfg, mail, fetch_usage=True)      # 无 join_workspace_id
      → personal chatgpt.com, 拿 access_token (/api/auth/session)
      → 浏览器内 page.evaluate fetch /backend-api/wham/usage (Bearer=at, 过 CF)
      → 解析 rate_limit.primary_window.reset_after_seconds
  → POST /api/accounts/{id}/result  status=success + usage_reset_seconds
```

一个 available 邮箱 = 一个注册好的 personal ChatGPT 号 + 一条 usage 记录。signup 不可逆消耗邮箱。

## 组件改动

### a. `backend/integrations/chatgpt/camoufox_register.py`

新增 `_fetch_usage(page, access_token, log) -> dict`：
- 复刻 `_join_workspace` 的浏览器内 fetch 模式（**必须** `page.evaluate`——backend-api 有 Cloudflare，浏览器外 Python 请求被挡；docs §5）。
- 请求：`fetch('/backend-api/wham/usage', {headers:{authorization:'Bearer '+at, accept:'*/*', 'x-openai-target-path':'/backend-api/wham/usage', 'x-openai-target-route':'/backend-api/wham/usage'}, credentials:'include'})`。
- 401/403 → 重取 `/api/auth/session` 刷 token 重试（同 join 逻辑），最多 3 次。
- 解析 `rate_limit.primary_window.reset_after_seconds`，防御式逐层取（缺失/非数 → None）。
- 返回 `{ok: bool, reset_after_seconds: int|None, status: int, raw: <截断字符串>}`。

`browser_register` 加参数 `fetch_usage: bool = False`：开启则在第 [8] 步拿到 `access_token` 后调一次 `_fetch_usage`，结果写入 `result["usage"]`。usage 读失败不抛错（注册成功是主要成果）。

### b. `pool/app.py`

**Schema**（`accounts` 表）新增三列，用 `ALTER TABLE ADD COLUMN` 幂等升级老库（包 try/except OperationalError）：
- `usage_reset_seconds INTEGER` — wham/usage 原始 `reset_after_seconds`
- `usage_reset_at REAL` — 绝对复位 epoch = 读取时刻 + reset_after_seconds
- `usage_checked_at REAL` — 读取时刻 epoch

**`report_result`** payload 加可选 `usage_reset_seconds`：
- 非 None 时，写三列：`usage_reset_seconds=值`、`usage_reset_at=now+值`、`usage_checked_at=now`。
- None/缺省时三列不动（保留旧值）。
- 仅在 `status=success/failed` 分支写（`banned` 删号，不涉及）。

**list / `_redact`** 带出三个新字段（非敏感，明文）。

**UI** 表格加一列「复位」：显示 `usage_reset_at` 的本地时间 + 距今剩余（`checked_at`/`reset_at` 为空显 `—`）。

### c. 新 runner `register_usage.py`

结构镜像 `register_workspace.py`，去掉 sub2api：
- `OutlookEmailService` lease（`MAIL_KIND` 决定 kind）。
- `browser_register(Cfg(), mail, fetch_usage=True)`（无 `join_workspace_id`）。
- 无 `access_token` → `report_result("failed", reason="no_access_token")`。
- 有 `access_token`：取 `res["usage"]`；
  - usage.ok → `report_result("success", usage_reset_seconds=res["usage"]["reset_after_seconds"])`
  - usage 读失败 → `report_result("success", reason="usage_fetch_failed")`（不判 failed——邮箱已消耗，注册成功是主要成果）
- 复用现有 JOB_BUDGET 预算循环 + `AccountDeactivated`→`banned` 删号逻辑。
- 不构造 `Sub2ApiClient`。

### d. 新 CI `.github/workflows/register_usage.yml`

镜像 `register_workspace.yml`：
- 输入：`mail_kind`(outlook|gmail) / `count_per_job` / `parallel_jobs`（动态矩阵）。**去掉** `workspace_id`。
- Secrets：`OUTLOOK_POOL_URL` / `POOL_API_KEY` / `VERIFY_PROXY`。**去掉** `WORKSPACE_ID` / `SUB2API_*`。
- 跑 `python register_usage.py <count_per_job>`。

## 错误处理

| 情况 | 处理 |
|------|------|
| 未到 chatgpt.com（无 access_token） | pool `failed`, reason `no_access_token` |
| usage fetch 失败（401 重试耗尽 / 网络 / 结构异常） | pool `success`, reason `usage_fetch_failed`, usage 字段留空 |
| `reset_after_seconds` 为 0 或缺失 | 0 照存（满窗口/无限流）；缺失存 None |
| `account_deactivated`（OTP 后封号） | pool `banned` → 删号（现有逻辑） |

## 硬约束（沿用）

- usage fetch **必须**浏览器内 `page.evaluate`（过 Cloudflare）。
- `playwright==1.60.0` / `camoufox==0.4.11` 锁死，不升。
- pool 改完 `scp` + `systemctl restart outlook-pool` 部署（本 PR 只改代码，部署单独做）。
- 老库平滑升级：`ALTER TABLE ADD COLUMN` 幂等。

## 非目标（YAGNI）

- 不做“重查已有 success 号”的独立租取流程（当前只新签即读）。
- 不加独立 `/api/accounts/{id}/usage` 端点（扩展 report_result 够用）。
- 不存整个 usage JSON（只三列）。
- 不导 sub2api。
