"""GENERIC IMAP/SMTP MAIL ACCOUNTS (Outlook / Office 365, Yahoo, Zoho, iCloud, a company mail server...).

This module holds only the protocol helpers; the account LIST, tags, the per-account `cloud_ai_allowed` switch and the
routes live in hub/gmailx.py (same interface: list_accounts / search / recent / send_mail / summary), so Gmail accounts and
IMAP accounts sit side by side in one list ("Mail accounts" in Settings).

Reading reuses the read-only `inbox.ImapMailBox` (the folder is opened readonly, messages fetched with BODY.PEEK). Sending
uses smtplib and happens ONLY on an explicit call to `send_mail` for an account whose "allow sending" switch is on.

The password (an app password where the provider requires one) is kept in the OS keyring under the account id, never in
a settings file, never in a log line, never in a response.

Presets are convenience defaults taken from the providers' public help pages. UNVERIFIED against live accounts:
Microsoft has been switching off basic-auth IMAP/SMTP for many Office 365 tenants (then a personal Outlook.com account with
an app password, or a different server, is needed); the first real connection is the real test.
"""
from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Callable

SECURITY = ("ssl", "starttls", "none")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$")
USER_RE = re.compile(r"^[^\s\x00-\x1f]{1,200}$")
IMAP_TIMEOUT = 20.0

PRESETS: dict[str, dict] = {
    "outlook": {"label": "Outlook / Office 365", "imap_host": "outlook.office365.com", "imap_port": 993, "imap_security": "ssl",
                "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls"},
    "yahoo": {"label": "Yahoo Mail", "imap_host": "imap.mail.yahoo.com", "imap_port": 993, "imap_security": "ssl",
              "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_security": "ssl"},
    "zoho": {"label": "Zoho Mail", "imap_host": "imap.zoho.com", "imap_port": 993, "imap_security": "ssl",
             "smtp_host": "smtp.zoho.com", "smtp_port": 465, "smtp_security": "ssl"},
    "icloud": {"label": "iCloud Mail", "imap_host": "imap.mail.me.com", "imap_port": 993, "imap_security": "ssl",
               "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_security": "starttls"},
    "custom": {"label": "Another mail server", "imap_host": "", "imap_port": 993, "imap_security": "ssl",
               "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls"},
}


class ImapProblem(ValueError):
    """A sentence the UI can show (never contains the password)."""


def clean_settings(src: dict, base: dict | None = None) -> dict:
    """Validated server settings from a request body, layered over `base` (an existing entry). Raises ImapProblem."""
    out = dict(base or {})
    preset = PRESETS.get(str(src.get("preset") or "")) if src.get("preset") else None
    merged = {**(preset or {}), **{k: v for k, v in src.items() if v is not None}}
    for side in ("imap", "smtp"):
        host = str(merged.get(f"{side}_host", out.get(f"{side}_host", "")) or "").strip()
        if side == "imap" and not host:
            raise ImapProblem("Give the IMAP server name (for example imap.example.com).")
        if host and not HOST_RE.match(host):
            raise ImapProblem(f"The {side.upper()} server name looks wrong.")
        port = merged.get(f"{side}_port", out.get(f"{side}_port"))
        sec = str(merged.get(f"{side}_security", out.get(f"{side}_security") or ("ssl" if side == "imap" else "starttls"))).lower()
        if sec not in SECURITY:
            raise ImapProblem("Security is ssl, starttls or none.")
        if host:
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ImapProblem(f"The {side.upper()} port is a number from 1 to 65535.")
        out[f"{side}_host"], out[f"{side}_port"], out[f"{side}_security"] = host, (port if host else None), sec
    user = str(merged.get("username", out.get("username", "")) or "").strip()
    if not USER_RE.match(user):
        raise ImapProblem("Give the user name (usually the full email address).")
    out["username"] = user
    return out


def open_box(entry: dict, password: str, *, connect: Callable[[], Any] | None = None):
    """A read-only mailbox for the account. `connect` lets tests hand in a fake IMAP connection."""
    from .inbox import ImapMailBox
    host, port, sec = entry["imap_host"], int(entry["imap_port"]), entry.get("imap_security", "ssl")
    if connect is None and sec != "ssl":
        def connect() -> Any:  # noqa: E306 - STARTTLS or (discouraged) plain
            conn = imaplib.IMAP4(host, port, timeout=IMAP_TIMEOUT)
            if sec == "starttls":
                conn.starttls(ssl_context=ssl.create_default_context())
            conn._encoding = "utf-8"
            return conn
    return ImapMailBox(entry.get("username") or entry.get("address") or "", password, host=host, port=port, connect=connect)


def send_smtp(entry: dict, password: str, from_addr: str, recipients: list[str], subject: str, body: str, *,
              attachments: list[tuple[str, bytes, str]] | None = None, smtp_factory: Callable[[dict], Any] | None = None) -> None:
    """Send one message. Only ever called from an explicit send. Raises ImapProblem with a safe sentence."""
    host = entry.get("smtp_host") or ""
    if not host:
        raise ImapProblem("This account has no SMTP server saved, so it can only read mail.")
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = ", ".join(recipients), from_addr, subject
    msg.set_content(body)
    for name, data, mime in attachments or []:
        main, _, sub = mime.partition("/")
        msg.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
    port, sec = int(entry.get("smtp_port") or 587), entry.get("smtp_security", "starttls")
    server = None
    try:
        if smtp_factory is not None:
            server = smtp_factory(entry)
        elif sec == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=IMAP_TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=IMAP_TIMEOUT)
            if sec == "starttls":
                server.starttls(context=ssl.create_default_context())
        server.login(entry.get("username") or from_addr, password.replace(" ", ""))
        server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise ImapProblem("The mail server did not accept the user name and password.") from None
    except (smtplib.SMTPException, OSError):
        raise ImapProblem("Couldn't send through the mail server. Check the server, port and security setting.") from None
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
