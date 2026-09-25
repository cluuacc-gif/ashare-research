#!/usr/bin/env python3
"""Send a research markdown report by SMTP.

Credentials come from environment only:
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM, MAIL_TO
Never hard-code secrets. If SMTP is not configured, save an .eml draft instead.
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def build_message(subject: str, body: str, mail_to: str, mail_from: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body)
    return msg


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--subject", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--mail-to", default=os.environ.get("MAIL_TO", "mrzouhappy@outlook.com"))
    p.add_argument("--draft-out", help="write .eml here when SMTP is not configured or send fails")
    args = p.parse_args()

    body = Path(args.body_file).read_text(encoding="utf-8")
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    mail_from = os.environ.get("SMTP_FROM", user or "noreply@localhost")
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = build_message(args.subject, body, args.mail_to, mail_from)

    draft = Path(args.draft_out) if args.draft_out else None
    if not (host and user and password):
        if draft:
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_bytes(msg.as_bytes())
            print(f"SMTP not configured; draft saved to {draft}")
            return 0
        print("SMTP not configured and no --draft-out; nothing sent")
        return 2

    import time

    last_err = None
    for attempt in range(1, 4):
        try:
            context = ssl.create_default_context()
            if port == 465:
                # QQ/163 style implicit SSL
                with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                    smtp.login(user, password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                    smtp.login(user, password)
                    smtp.send_message(msg)
            print(f"sent to {args.mail_to} via {host}:{port} attempt={attempt}")
            return 0
        except Exception as e:
            last_err = e
            print(f"send attempt {attempt} failed: {type(e).__name__}: {e}")
            if attempt < 3:
                time.sleep(2 * attempt)
    if draft:
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(msg.as_bytes())
        print(f"send failed ({type(last_err).__name__}: {last_err}); draft saved to {draft}")
        return 0
    print(f"send failed: {type(last_err).__name__}: {last_err}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
