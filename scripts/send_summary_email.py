"""Sends the prepared invoice-sync summary email via Gmail SMTP.

Replaces the previous approach of asking a second `claude -p` agent to
read the prepared JSON and pass its fields to `send_message` "verbatim".
An LLM copying a multi-kilobyte HTML blob by hand is unreliable: it
reliably reproduced the overall skeleton (the four stat tiles) while
silently dropping the repetitive table rows, so reports arrived with
counters but no data lines. Sending here instead is byte-exact, costs
no tokens, needs no tool-permission rules, and can't be throttled by an
API spend limit.

Reads the pending-summary JSON on stdin ({to, subject, body, htmlBody})
and authenticates with the same Gmail address + App Password already
used for IMAP attachment retrieval.
"""
import json
import os
import smtplib
import sys
from email.message import EmailMessage


def send(payload):
    """Send one summary email. `payload` is {to, subject, body, htmlBody}."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    creds_path = os.path.join(script_dir, "..", "invoices", ".credentials", "gmail_imap.json")
    try:
        with open(creds_path, encoding="utf-8") as f:
            creds = json.load(f)
    except FileNotFoundError:
        print(f"credentials file not found: {creds_path}", file=sys.stderr)
        sys.exit(1)

    sender = creds["email"]
    recipients = payload["to"]
    if isinstance(recipients, str):
        recipients = [recipients]

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = payload["subject"]
    msg.set_content(payload["body"])
    msg.add_alternative(payload["htmlBody"], subtype="html")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(sender, creds["app_password"])
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        print(
            "SMTP login failed. The Gmail App Password in "
            "invoices/.credentials/gmail_imap.json was rejected.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"sent summary email to {', '.join(recipients)} ({len(payload['htmlBody'])} bytes of HTML)")


def main():
    # Read the raw bytes and decode UTF-8 explicitly. sys.stdin.read() would
    # use the Windows locale encoding (cp1252 here), which mangles every
    # non-ASCII character the report contains — em-dashes in the subject,
    # Greek vendor names and filenames in the tables.
    send(json.loads(sys.stdin.buffer.read().decode("utf-8")))


if __name__ == "__main__":
    main()
