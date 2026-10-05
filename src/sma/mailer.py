"""Gmail over SMTP (send) + IMAP (thread ids, replies, bounces) with an App Password.

Dry run writes each email to outbox/ as an .eml file and never opens a connection.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage, Message
from email.utils import formataddr, make_msgid, parseaddr

from .config import Config
from .templates import resume_path


@dataclass
class SentRef:
    msg_id: str        # RFC Message-ID
    thread_id: str     # Gmail X-GM-THRID ('' when unknown / dry run)


@dataclass
class Inbound:
    gm_msgid: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    headers: dict[str, str]

    @property
    def sender_addr(self) -> str:
        return parseaddr(self.sender)[1].lower()


class Mailer:
    def __init__(self, cfg: Config, dry_run: bool | None = None) -> None:
        self.cfg = cfg
        self.dry_run = cfg.dry_run if dry_run is None else dry_run
        self._smtp: smtplib.SMTP_SSL | None = None
        self._imap: imaplib.IMAP4_SSL | None = None

    # --- connections ---------------------------------------------------------
    def _password(self) -> str:
        pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
        if not pw:
            raise RuntimeError("GMAIL_APP_PASSWORD is not set in .env (create one at "
                               "https://myaccount.google.com/apppasswords; needs 2-step verification on the account)")
        return pw

    def smtp(self) -> smtplib.SMTP_SSL:
        if self._smtp is None:
            pw = self._password()
            self._smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
            self._smtp.login(self.cfg.me.email, pw)
        return self._smtp

    def imap(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            pw = self._password()
            self._imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            self._imap.login(self.cfg.me.email, pw)
        return self._imap

    def close(self) -> None:
        for conn, quit_ in ((self._smtp, "quit"), (self._imap, "logout")):
            if conn is not None:
                try:
                    getattr(conn, quit_)()
                except Exception:  # noqa: BLE001 - closing a dead connection is not an error worth raising
                    pass
        self._smtp = self._imap = None

    def check(self) -> None:
        """Raises when the App Password, SMTP or IMAP access is wrong."""
        self.smtp().noop()
        self.imap().select('"[Gmail]/All Mail"', readonly=True)

    # --- send ------------------------------------------------------------------
    def build(self, to: str, subject: str, body: str, in_reply_to: str = "", references: str = "",
              attach_resume: bool = False) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self.cfg.me.name, self.cfg.me.email))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.cfg.me.email.split("@")[-1] or "localhost")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        msg.set_content(strip_bold(body), subtype="plain", charset="utf-8")
        if BOLD_RE.search(body):                       # **x** markup -> an HTML part that shows it bold
            msg.add_alternative(to_html(body), subtype="html", charset="utf-8")
        resume = resume_path(self.cfg)
        if attach_resume and resume:
            msg.add_attachment(resume.read_bytes(), maintype="application", subtype="pdf", filename=resume.name)
        return msg

    def send(self, msg: EmailMessage) -> SentRef:
        if self.dry_run:
            out = self.cfg.path("outbox")
            out.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            safe_to = re.sub(r"[^a-z0-9]+", "_", str(msg["To"]).lower())
            (out / f"{stamp}-{safe_to}.eml").write_bytes(bytes(msg))
            return SentRef(msg_id=str(msg["Message-ID"]), thread_id="")
        try:
            self.smtp().send_message(msg)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError):
            self._smtp = None                       # dropped connection: reopen once
            self.smtp().send_message(msg)
        return SentRef(msg_id=str(msg["Message-ID"]), thread_id=self.thread_id_for(str(msg["Message-ID"])))

    def thread_id_for(self, rfc_message_id: str) -> str:
        """Gmail files SMTP-sent mail under Sent Mail right away; its X-GM-THRID is what replies share."""
        try:
            im = self.imap()
            im.select('"[Gmail]/Sent Mail"', readonly=True)
            _, data = im.uid("search", None, "HEADER", "Message-ID", rfc_message_id)
            uids = (data[0] or b"").split()
            if not uids:
                return ""
            _, res = im.uid("fetch", uids[-1], "(X-GM-THRID)")
            head = res[0][0] if isinstance(res[0], tuple) else res[0]
            m = re.search(rb"X-GM-THRID (\d+)", head or b"")
            return m.group(1).decode() if m else ""
        except (imaplib.IMAP4.error, OSError, IndexError, TypeError):
            self._imap = None
            return ""

    # --- read ------------------------------------------------------------------
    def inbound_since(self, since: datetime) -> list[Inbound]:
        """Every message in All Mail since `since` not sent by us, with full body. Read-only (nothing marked read)."""
        im = self.imap()
        im.select('"[Gmail]/All Mail"', readonly=True)
        _, data = im.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"), "NOT", "FROM", self.cfg.me.email)
        uids = (data[0] or b"").split()
        out: list[Inbound] = []
        for i in range(0, len(uids), 100):
            chunk = b",".join(uids[i:i + 100]).decode()
            _, res = im.uid("fetch", chunk, "(X-GM-THRID X-GM-MSGID BODY.PEEK[])")
            for part in res:
                if not isinstance(part, tuple):
                    continue
                t = re.search(rb"X-GM-THRID (\d+)", part[0])
                m = re.search(rb"X-GM-MSGID (\d+)", part[0])
                msg = email.message_from_bytes(part[1])
                try:
                    received = email.utils.parsedate_to_datetime(msg.get("Date", "")).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    received = datetime.now(timezone.utc)
                out.append(Inbound(
                    gm_msgid=m.group(1).decode() if m else "", thread_id=t.group(1).decode() if t else "",
                    sender=str(msg.get("From", "")), subject=str(msg.get("Subject", "")), body=mime_text(msg),
                    received_at=received,
                    headers={k.lower(): str(v) for k, v in msg.items()
                             if k.lower() in ("in-reply-to", "references", "auto-submitted", "x-failed-recipients",
                                              "x-autoreply", "x-autorespond", "precedence")},
                ))
        return out


BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def strip_bold(text: str) -> str:
    return BOLD_RE.sub(r"\1", text)


def to_html(text: str) -> str:
    """Template text with **bold** -> the minimal HTML a hand-written Gmail message has."""
    import html as html_lib
    paras = []
    for para in text.split("\n\n"):
        esc = BOLD_RE.sub(r"<b>\1</b>", html_lib.escape(para, quote=False))
        paras.append("<p>" + esc.replace("\n", "<br>") + "</p>")
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">'
            + "\n".join(paras) + "</div>")


def mime_text(msg: Message) -> str:
    """text/plain preferred, stripped text/html otherwise; delivery reports keep their machine part too."""
    plain, html = [], []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype in ("message/delivery-status", "text/rfc822-headers"):
            payload = part.get_payload()
            plain.append(payload if isinstance(payload, str) else "\n".join(str(p) for p in payload or []))
            continue
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        (plain if ctype == "text/plain" else html).append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    text = "\n".join(plain) if plain else re.sub(r"<[^>]+>", " ", "\n".join(html))
    return strip_quotes(text)


def strip_quotes(text: str) -> str:
    text = re.split(r"\n(On .{5,160} wrote:|-{2,} ?Original Message|From: .+\n?Sent: )", text)[0]
    text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()
