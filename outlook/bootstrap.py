#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-time device-code bootstrap: mint a reusable refresh_token per Outlook account.

Personal Microsoft accounts no longer allow password/IMAP-basic-auth or ROPC,
so we sign in *once* via the device-code flow — you open the shown URL in your
own browser and enter the code. Do this from a normal (residential) browser, not
a datacenter IP, or the fresh account may get flagged with an "unusual sign-in"
challenge / lock.

The minted refresh_token is long-lived and reused headlessly afterwards (CI reads
the mailbox over IMAP XOAUTH2 with no browser). Each account is signed in
separately so you always know which code belongs to which mailbox.

Account file (default outlook/accounts.txt), one per line:
    email----password                      (before bootstrap)
    email----password----refresh_token     (after bootstrap; rewritten in place)

Usage:
    python outlook/bootstrap.py                     # bootstrap all not-yet-done
    python outlook/bootstrap.py --only a@outlook.com  # just one (validation)
    python outlook/bootstrap.py --file path.txt --force
"""
import argparse
import os
import sys

# allow `python outlook/bootstrap.py` from repo root to import the backend pkg
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.integrations.mail.outlook import (  # noqa: E402
    THUNDERBIRD_CLIENT_ID,
    OutlookAuthError,
    device_code_login,
    parse_account_line,
)

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.txt")


def _serialize(acct) -> str:
    fields = [acct.email, acct.password, acct.refresh_token]
    if acct.client_id and acct.client_id != THUNDERBIRD_CLIENT_ID:
        fields.append(acct.client_id)
    return "----".join(fields)


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint Outlook refresh_tokens via device-code login.")
    ap.add_argument("--file", default=DEFAULT_FILE, help="account file (default outlook/accounts.txt)")
    ap.add_argument("--only", help="bootstrap only this email")
    ap.add_argument("--force", action="store_true", help="re-bootstrap even if a refresh_token already exists")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"account file not found: {args.file}", file=sys.stderr)
        return 1

    accounts = []
    with open(args.file, "r", encoding="utf-8") as f:
        for line in f:
            acct = parse_account_line(line)
            if acct:
                accounts.append(acct)
    if not accounts:
        print("no accounts parsed", file=sys.stderr)
        return 1

    changed = False
    for acct in accounts:
        if args.only and acct.email != args.only.strip().lower():
            continue
        if acct.refresh_token and not args.force:
            print(f"[skip] {acct.email} already bootstrapped")
            continue
        print(f"\n=== {acct.email} ===")
        try:
            tok = device_code_login(acct.email, client_id=acct.client_id, on_prompt=print)
        except OutlookAuthError as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            continue
        rt = tok.get("refresh_token", "")
        if not rt:
            print("  FAILED: no refresh_token returned (scope missing offline_access?)", file=sys.stderr)
            continue
        acct.refresh_token = rt
        changed = True
        print(f"  OK refresh_token minted (len={len(rt)})")

    if changed:
        with open(args.file, "w", encoding="utf-8") as f:
            for acct in accounts:
                f.write(_serialize(acct) + "\n")
        print(f"\nwrote {args.file}")
    else:
        print("\nnothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
