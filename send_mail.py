#!/usr/bin/env python3
"""Send a research markdown report by SMTP.

Credentials come from environment only:
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM, MAIL_TO
Never hard-code secrets. If SMTP is not configured, save an .eml draft instead.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from email.utils import format_datetime


def build_message(subject: str, body: str, mail_to: str, mail_from: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Date"] = format_datetime(datetime.now(ZoneInfo("Asia/Shanghai")))
    identity = hashlib.sha256((subject + '\n' + body + '\n' + mail_to).encode()).hexdigest()
    msg["Message-ID"] = f"<{identity}@ashare-research.local>"
    msg.set_content(body)
    return msg


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--subject", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--mail-to", default=os.environ.get("MAIL_TO", ""))
    p.add_argument("--draft-out", help="write .eml here when SMTP is not configured or send fails")
    p.add_argument("--status-out", help="machine-readable delivery result; never includes credentials")
    p.add_argument("--draft-only", action="store_true", help="explicit offline preview; does not claim delivery")
    args = p.parse_args()

    body = Path(args.body_file).read_text(encoding="utf-8")
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    mail_from = os.environ.get("SMTP_FROM", user or "noreply@localhost")
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = build_message(args.subject, body, args.mail_to, mail_from)

    draft = Path(args.draft_out) if args.draft_out else None
    status_path = Path(args.status_out) if args.status_out else Path(args.body_file).with_suffix('.delivery.json')

    def status(state, attempt=0, error=None):
        value = {'state': state, 'report_sha256': hashlib.sha256(body.encode()).hexdigest(),
                 'message_id': msg['Message-ID'], 'attempts': attempt,
                 'recorded_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                 'smtp_accepted': state == 'smtp_accepted', 'receipt_confirmed': False,
                 'error_class': error}
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    if args.draft_only:
        if not draft:
            raise ValueError('--draft-only requires --draft-out')
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(msg.as_bytes())
        status('draft_only')
        return 0
    if not (host and user and password and args.mail_to.strip()):
        if draft:
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_bytes(msg.as_bytes())
            print(f"SMTP not configured; draft saved to {draft}")
            status('not_configured')
            return 2
        print("SMTP not configured and no --draft-out; nothing sent")
        status('not_configured')
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
                    refused = smtp.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                    smtp.login(user, password)
                    refused = smtp.send_message(msg)
            if refused:
                status('recipient_refused', attempt, 'SMTPRecipientsRefused')
                return 1
            status('smtp_accepted', attempt)
            print(f"SMTP accepted report attempt={attempt}; receipt not independently confirmed")
            return 0
        except Exception as e:
            last_err = e
            print(f"send attempt {attempt} failed: {type(e).__name__}")
            if isinstance(e, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused)):
                break
            if attempt < 3:
                time.sleep(2 * attempt)
    if draft:
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(msg.as_bytes())
        print(f"send failed ({type(last_err).__name__}); draft saved to {draft}")
    status('failed', attempt, type(last_err).__name__)
    print(f"send failed: {type(last_err).__name__}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
