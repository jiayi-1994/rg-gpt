# Outlook mailbox OTP support

Read ChatGPT/OpenAI verification codes from **personal** Microsoft mailboxes
(`@outlook.com` / `@hotmail.com` / `@live.com`) and feed them into the account
registration runner.

## Why not just email + password

Microsoft killed both password paths for personal accounts (Sept 2024):

| mechanism | result |
|-----------|--------|
| IMAP basic auth | `AUTHENTICATE failed` |
| ROPC (`grant_type=password`) | `AADSTS9001023` — blocked on `/consumers` |

So the mailbox is read over **IMAP XOAUTH2** with an OAuth2 access token derived
from a per-account **refresh_token**.

## Account format

`outlook/accounts.txt` (gitignored) or the `OUTLOOK_ACCOUNTS` env, one per line.
Field order is auto-detected (UUID = client_id, long blob = refresh_token):

```
email----password----client_id----refresh_token      # seller "full" format
email----password----refresh_token                   # client_id defaults to Thunderbird
email----password                                     # needs bootstrap (see below)
```

- A line **with** a refresh_token works immediately (no browser).
- A line with **only** email+password must be bootstrapped once.

## Bootstrap (only for email+password-only lines)

Mints a reusable refresh_token via the device-code flow. Sign in from your **own
browser** (residential IP) — a datacenter/CI login can flag/lock a fresh account.

```bash
python outlook/bootstrap.py --only you@outlook.com   # one (validate first)
python outlook/bootstrap.py                           # all not-yet-done
```

It rewrites `accounts.txt` in place, appending the minted refresh_token.

## Read codes (debug / manual)

```bash
python outlook/get_code.py                    # scan all bootstrapped accounts
python outlook/get_code.py --only a@outlook.com --keyword openai
```

## Use in registration

```bash
MAIL_PROVIDER=outlook python register_sub2api_oauth.py <count>
```

Pool source: `OUTLOOK_ACCOUNTS` env (newline/`;`-separated) or `outlook/accounts.txt`.

### Two registration runners

| runner | credential | durability | how |
|--------|-----------|-----------|-----|
| `register_sub2api_oauth.py` | OAuth **refresh_token** | durable (auto-renews, months) | codex authorize → `create-from-oauth` |
| `register_workspace.py` | session **access_token** (CPA) | **short-lived** (auto-pauses on expiry) | signup → join k12 workspace → import session |

**`register_workspace.py` (session/CPA + k12 workspace join):**

```bash
MAIL_PROVIDER=outlook WORKSPACE_ID=631e1603-06cf-4f0b-b79b-d09fbfcfe98d \
  python register_workspace.py <count>
```

Flow: claim Outlook mailbox → Camoufox signs up ChatGPT (OTP from Outlook) → in the
same browser `POST /backend-api/accounts/{WORKSPACE_ID}/invites/request` (join, runs
in-browser to pass Cloudflare) → re-read `/api/auth/session` → import a CPA account
(`credentials.access_token` + `chatgpt_account_id=WORKSPACE_ID`, no refresh_token) via
`POST /api/v1/admin/accounts/data`.

- sub2api **ignores `session_token`** and stores no cookie, so there is no durable
  credential here — the account is pinned to the access_token expiry and **auto-pauses**
  when it lapses (hours~days). Re-run to refresh, or use `register_sub2api_oauth.py`
  for durable accounts.
- `WORKSPACE_ID` = the k12 workspace to join (gives its paid plan). Selected purely via
  `credentials.chatgpt_account_id` → gateway sends the `chatgpt-account-id` header; no
  manual UI workspace switch needed.
- A failed join (Turnstile / not auto-approved) burns the Outlook account without a
  usable result.

### Finite pool — read this

Each Outlook mailbox can back **exactly one** ChatGPT
signup (an email registers once). So:

- `create_email()` *claims* a distinct unused mailbox per signup; used ones are
  recorded in `outlook/.used` so re-runs skip them.
- `count` is auto-capped to the number of available (bootstrapped, unused) accounts.
- This is a **one-shot batch**, not the 30-minute cron in `register.yml` (that cron
  is built for CloudMail's infinite temp domains). Run outlook mode manually, or as
  a manual `workflow_dispatch` with a single job / small count.
- Across parallel CI jobs set `JOB_INDEX` (1-based) + `JOB_TOTAL` so each job gets a
  disjoint slice of the pool (the `.used` marker is per-runner, not shared).
