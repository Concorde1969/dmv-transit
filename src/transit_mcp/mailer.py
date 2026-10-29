"""Sending the sign-in code email (``accounts.py``).

Mail goes out through any SMTP server with STARTTLS; on the public site that is Amazon
SES's SMTP endpoint (``SMTP_HOST=email-smtp.<region>.amazonaws.com``) with SES SMTP
credentials. Without SMTP settings, a development server writes the code to its log
instead, and a production server can't send codes (accounts are then unavailable).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

log = logging.getLogger("transit_mcp.mailer")


class MailError(RuntimeError):
    pass


class Mailer:
    def __init__(self, *, host: str | None, port: int = 587, username: str | None = None,
                 password: str | None = None, sender: str = "", log_only: bool = False):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.sender = sender
        self.log_only = log_only and not host

    @classmethod
    def from_settings(cls, settings) -> Mailer:
        return cls(host=settings.smtp_host, port=settings.smtp_port,
                   username=settings.smtp_username, password=settings.smtp_password,
                   sender=settings.mail_from, log_only=not settings.is_production)

    @property
    def available(self) -> bool:
        return bool(self.host and self.sender) or self.log_only

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        if self.log_only:
            log.warning("No SMTP_HOST set (development): email to %s - %s\n%s",
                        to, subject, text)
            return
        if not self.available:
            raise MailError("email isn't set up")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg["Message-ID"] = make_msgid(domain=self.sender.rpartition("@")[2].strip("> "))
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            # The SMTP password is never part of these messages.
            raise MailError(f"{type(exc).__name__}: {exc}") from None


def code_email(code: str, site: str) -> tuple[str, str, str]:
    """(subject, text, html) of the sign-in code email."""
    subject = f"Your DMV Transit sign-in code: {code}"
    text = (f"Your sign-in code for DMV Transit is {code}\n\n"
            "Type it on the page where you asked for it. It works for 10 minutes.\n\n"
            "If you didn't ask to sign in, you can ignore this email; nobody can sign in "
            "without the code.\n\n"
            f"DMV Transit (unofficial) - {site}\n")
    html = f"""<!doctype html><html><body style="margin:0;padding:24px;background:#E7EEE6;
font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#1B2621">
<div style="max-width:480px;margin:0 auto;background:#FAFBF6;border-radius:18px;padding:28px">
<p style="margin:0 0 6px;font-size:17px">Your sign-in code for DMV Transit is</p>
<p style="margin:0 0 18px;font-size:40px;font-weight:700;letter-spacing:6px">{code}</p>
<p style="margin:0 0 14px;font-size:16px">Type it on the page where you asked for it.
It works for 10 minutes.</p>
<p style="margin:0;font-size:14px;color:#4A5651">If you didn't ask to sign in, you can
ignore this email; nobody can sign in without the code.</p>
</div>
<p style="text-align:center;font-size:13px;color:#4A5651">DMV Transit (unofficial) &middot;
{site}</p></body></html>"""
    return subject, text, html
