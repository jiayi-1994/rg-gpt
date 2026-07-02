# Outlook account pool (web service)

A small FastAPI + SQLite service that holds Outlook accounts and hands them to CI
runners with an **atomic lease**, then records which succeeded / failed so they can
be retried deliberately. Replaces the local `outlook/accounts.txt` for CI: many
parallel jobs (and re-runs) can share one pool without colliding.

## State machine

```
available --lease--> leased --success--> success   (terminal; stores sub2api acct id)
                       |     --failure--> failed     (stores reason; NOT auto-recycled)
                       |     --TTL expiry--> stale    (job died mid-run; needs review)
failed / stale --(user clicks 重试)--> available
any --disable--> disabled
```

A signup irreversibly consumes a mailbox, so a leased account a job touched is **never**
auto-returned to `available` — retry is always a deliberate action (UI button or
`POST /api/accounts/{id}/retry`).

## Run

```bash
pip install -r pool/requirements.txt
POOL_API_KEY='<long-random-key>' POOL_DB=/var/lib/outlook-pool/pool.db \
  uvicorn pool.app:app --host 0.0.0.0 --port 8080
```

Open `http://HOST:8080/`, paste the API key once (stored in localStorage), then bulk-add
accounts (`email----password----client_id----refresh_token`, field order auto-detected).

Env:
- `POOL_API_KEY` — required; guards **every** `/api/*` endpoint. Reads redact password +
  refresh_token; full creds only via authenticated `POST /api/lease`.
- `POOL_DB` — SQLite path (default `pool/pool.db`, gitignored).
- `POOL_LEASE_TTL` — seconds before a stuck lease flips to `stale` (default 1200).

Run single-worker (default). The lease SELECT+UPDATE is serialized in-process, so do
**not** run multiple uvicorn workers against the same DB.

## API

| method + path | purpose |
|---|---|
| `GET /api/stats` | counts per status |
| `GET /api/accounts?status=` | list (redacted) |
| `POST /api/accounts` | bulk add `{ "lines": "email----pw----cid----rt\n..." }` |
| `POST /api/lease` | atomically lease `{count, leased_by}` → full creds + `lease_token` |
| `POST /api/accounts/{id}/result` | `{status: success\|failed, reason, sub2api_account_id, workspace_id, refresh_token, lease_token}` |
| `POST /api/accounts/{id}/retry` | failed/stale → available |
| `POST /api/accounts/{id}/disable` · `DELETE /api/accounts/{id}` | |

All require header `X-API-Key: <POOL_API_KEY>`.

## Runner integration

`register_workspace.py` uses the pool when `OUTLOOK_POOL_URL` + `POOL_API_KEY` are set
(otherwise falls back to local `outlook/accounts.txt`):
`create_email()` leases one account; on completion the runner reports success (with the
sub2api account id + rotated refresh_token) or failed (with the stage as reason).
See `.github/workflows/register_workspace.yml` — set repo secrets `OUTLOOK_POOL_URL`,
`POOL_API_KEY`, `WORKSPACE_ID`, `SUB2API_*`, `VERIFY_PROXY`.

## Deploy (target host — creds via env, never committed)

Deploy target: `1.94.147.46:22` (root). Do NOT hardcode the SSH password anywhere in
git; pass it at deploy time.

```bash
# 1) copy the pool app to the server
scp -r pool root@1.94.147.46:/opt/outlook-pool

# 2) on the server: venv + deps
ssh root@1.94.147.46
  cd /opt/outlook-pool && python3 -m venv .venv && . .venv/bin/activate
  pip install -r requirements.txt

# 3) systemd unit (edit the key)
cat >/etc/systemd/system/outlook-pool.service <<'UNIT'
[Unit]
Description=Outlook account pool
After=network.target
[Service]
WorkingDirectory=/opt/outlook-pool
Environment=POOL_API_KEY=CHANGE_ME_LONG_RANDOM
Environment=POOL_DB=/var/lib/outlook-pool/pool.db
ExecStart=/opt/outlook-pool/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
  mkdir -p /var/lib/outlook-pool
  systemctl daemon-reload && systemctl enable --now outlook-pool
```

Note `uvicorn app:app` (run from inside `/opt/outlook-pool`), not `pool.app:app`.

**Security:** this stores passwords + refresh_tokens on a public IP. Use a strong
`POOL_API_KEY`, and prefer fronting it with a TLS reverse proxy (caddy/nginx) +
firewalling port 8080 to the CI egress / your IPs. The API key travels in a header, so
plain-HTTP exposure leaks it — put TLS in front before real use.
