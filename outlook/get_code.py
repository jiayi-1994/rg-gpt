#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read verification codes from Outlook/Hotmail mailboxes via OAuth2 IMAP XOAUTH2.

Basic-auth IMAP is dead for personal Microsoft accounts, so this reads mail with
an access token derived from the account's refresh_token. Bootstrap the tokens
first with ``python outlook/bootstrap.py``.

Account file (default outlook/accounts.txt), one per line:
    email----password----refresh_token

Usage:
    python outlook/get_code.py                      # scan all bootstrapped accounts
    python outlook/get_code.py --only a@outlook.com
    python outlook/get_code.py --keyword openai --limit 5
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.integrations.mail.outlook import (  # noqa: E402
    OutlookAuthError,
    _extract_otp,
    _html_to_text,
    imap_read_messages,
    load_accounts,
)

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.txt")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch verification codes from Outlook via OAuth2 IMAP.")
    ap.add_argument("--only", help="only this email")
    ap.add_argument("--keyword", default="", help="require this substring in the mail")
    ap.add_argument("--limit", type=int, default=5, help="messages to scan per folder")
    args = ap.parse_args()

    os.environ.setdefault("OUTLOOK_ACCOUNTS_FILE", DEFAULT_FILE)
    accounts = load_accounts()
    if not accounts:
        print("no accounts parsed (is outlook/accounts.txt populated?)", file=sys.stderr)
        return 1

    kw = args.keyword.lower()
    for acct in accounts:
        if args.only and acct.email != args.only.strip().lower():
            continue
        print(f"\n=== {acct.email} ===")
        if not acct.bootstrapped:
            print("  (no refresh_token — run: python outlook/bootstrap.py)")
            continue
        try:
            token = acct.access_token()
            mails = imap_read_messages(acct.email, token, per_folder=args.limit)
        except OutlookAuthError as exc:
            print(f"  ERROR: {exc}")
            continue
        if not mails:
            print("  (mailbox empty)")
            continue
        shown = 0
        for m in mails:
            haystack = "\n".join(p for p in (m["subject"], m["text"], _html_to_text(m["html"])) if p)
            if kw and kw not in haystack.lower():
                continue
            code = _extract_otp(haystack, None)
            print(f"  [{m['folder']}] {m['received_at']}  from {m['sender'][:50]}")
            print(f"    Subject: {m['subject'][:80]}")
            print(f"    CODE   : {code or '(none found)'}")
            shown += 1
            if shown >= args.limit:
                break
        if not shown:
            print("  (no matching mail)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
