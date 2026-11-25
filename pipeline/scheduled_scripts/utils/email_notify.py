"""Email notification helper for scheduled-script runs.

Pattern: Gmail SMTP over SSL, app-password auth, sender + recipient as plain env vars and only
the app password as a secret. Works inside GitHub Actions runners
without external dependencies (Python stdlib only).

CLI usage (from a workflow step)::

    python -m pipeline.scheduled_scripts.utils.email_notify \\
      --subject "NPPES snapshot OK — 2026-05" \\
      --body-file data/registries/nppes_diff_report.md

Required environment variables:

    GMAIL_APP_PASSWORD   Gmail app password for the sending account.
                         Generate at https://myaccount.google.com/apppasswords.
    GMAIL_FROM           Sender address.
    GMAIL_TO             Recipient address(es). Comma-separated for
                         multiple recipients (e.g.
                         "alice@x.com, bob@y.com"). All recipients see
                         each other on the To line.

If ``GMAIL_APP_PASSWORD`` is not set the CLI exits cleanly with a
warning — useful for ``--no-push`` style local tests where you don't
want to actually send mail.
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path


def send_email(
    *,
    subject: str,
    body: str,
    from_addr: str,
    to_addr: str,
    app_password: str,
    attachments: list[Path] | None = None,
) -> None:
    """Send a plain-text email via Gmail SMTP, with optional file attachments."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    for att in attachments or []:
        att = Path(att)
        if not att.exists():
            print(f"[email_notify] attachment not found, skipping: {att}", file=sys.stderr)
            continue
        msg.add_attachment(
            att.read_bytes(), maintype="text", subtype="csv", filename=att.name
        )
    with smtplib.SMTP_SSL(
        "smtp.gmail.com", 465, context=ssl.create_default_context()
    ) as s:
        s.login(from_addr, app_password)
        s.send_message(msg)


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--subject", required=True, help="Email subject line.")
    body_group = ap.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", help="Inline email body.")
    body_group.add_argument(
        "--body-file",
        type=Path,
        help="Path to a file whose contents form the email body.",
    )
    ap.add_argument(
        "--attach",
        type=Path,
        action="append",
        default=[],
        help="File to attach (repeatable). Missing files are skipped with a warning.",
    )
    args = ap.parse_args()

    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    from_addr = os.environ.get("GMAIL_FROM", "").strip()
    to_raw = os.environ.get("GMAIL_TO", "").strip()
    to_list = [a.strip() for a in to_raw.split(",") if a.strip()]

    if not app_password:
        print(
            "[email_notify] GMAIL_APP_PASSWORD not set — skipping email send",
            file=sys.stderr,
        )
        return 0
    if not from_addr or not to_list:
        print(
            "[email_notify] GMAIL_FROM / GMAIL_TO not set — skipping email send",
            file=sys.stderr,
        )
        return 0

    if args.body_file is not None:
        body = args.body_file.read_text()
    else:
        body = args.body or ""

    send_email(
        subject=args.subject,
        body=body,
        from_addr=from_addr,
        to_addr=", ".join(to_list),
        app_password=app_password,
        attachments=args.attach,
    )
    print(
        f"[email_notify] sent to {len(to_list)} recipient(s) "
        f"({', '.join(to_list)}): {args.subject}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
