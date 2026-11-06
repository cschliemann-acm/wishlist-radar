"""Saved-search alert emails.

Many sites can email you when something new matches a saved search (Depop, Vinted, eBay,
Grailed, SSENSE wishlist sale alerts, brand newsletters…), even when they block automated
access. Point those alerts at a dedicated mailbox (or forward them there with a filter), and
the radar reads it over IMAP every few minutes: Claude pulls the listings out of each email
and they're matched against your wishlist like any other search result.

Set ALERTS_IMAP_USER and ALERTS_IMAP_PASSWORD (for Gmail: an app password) and optionally
ALERTS_IMAP_HOST (default imap.gmail.com).
"""

from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.policy import default as default_policy
from email.utils import parseaddr

POLL_MINUTES = 10


@dataclass
class AlertEmail:
    uid: str
    sender_name: str
    sender: str
    subject: str
    html: str
    text: str


def _decode(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def parse(uid: str, raw: bytes) -> AlertEmail:
    msg = email.message_from_bytes(raw, policy=default_policy)
    name, addr = parseaddr(msg.get("From", ""))
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    return AlertEmail(
        uid=uid,
        sender_name=_decode(name) or addr.split("@")[-1],
        sender=addr.lower(),
        subject=_decode(msg.get("Subject")),
        html=html_part.get_content() if html_part else "",
        text=text_part.get_content() if text_part else "",
    )


def fetch_unread(host: str, user: str, password: str, folder: str = "INBOX",
                 limit: int = 30) -> list[AlertEmail]:
    """Unread messages, marked as read once fetched."""
    out = []
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(folder)
        status, data = imap.uid("search", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"mailbox search failed: {status}")
        for uid in (data[0].split() if data and data[0] else [])[:limit]:
            status, parts = imap.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            out.append(parse(uid.decode(), parts[0][1]))
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    return out
