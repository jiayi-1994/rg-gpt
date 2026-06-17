"""Gate: query sub2api active OpenAI account count, emit `low` for the workflow.

Used by the register-accounts workflow's gate job. Sets GitHub step outputs
`low` (true if active < THRESHOLD) and `active`, so the register matrix runs
only when the pool is below target — the cron self-regulates, no over-registering.

Env: SUB2API_URL / SUB2API_EMAIL / SUB2API_PASSWORD, THRESHOLD (default 100).
Needs only `requests` (Sub2ApiClient + env-shim settings).
"""
import os

from backend.integrations.sub2api import Sub2ApiClient


def main():
    threshold = int(os.getenv("THRESHOLD", "100") or "100")
    client = Sub2ApiClient()
    client.ensure_configured()
    r = client.list_accounts(platform="openai", status="active", page=1, page_size=1)
    d = r.get("data") if isinstance(r, dict) else r
    active = 0
    if isinstance(d, dict):
        active = int(d.get("total") or d.get("total_count")
                     or (d.get("pagination") or {}).get("total") or 0)
    low = active < threshold
    print(f"active={active} threshold={threshold} low={low}")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"low={'true' if low else 'false'}\n")
            f.write(f"active={active}\n")


if __name__ == "__main__":
    main()
