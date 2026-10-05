"""Reply + bounce sync. Rule-based, no LLM:

  bounce  mailer-daemon / postmaster / delivery-failure subject   -> lead 'bounced', address suppressed
  ooo     auto-reply headers or out-of-office wording             -> logged, sequence continues
  optout  unsubscribe / not interested / remove me                -> lead 'closed', address suppressed
  reply   anything else from the lead                             -> lead 'replied', sequence stops (read it!)
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import Config
from .contacts import add_to_suppression
from .db import get_meta, set_meta, utcnow_iso
from .mailer import Inbound, Mailer
from .scheduler import local_day

EMAIL_IN_TEXT = re.compile(r"[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
BOUNCE_SUBJECT = re.compile(r"delivery status notification|undeliver|mail delivery (failed|subsystem)|returned mail|"
                            r"delivery (has )?failed|failure notice|address not found", re.I)
OOO = re.compile(r"out of (the )?office|automatic reply|auto[- ]?reply|autoreply|on leave|away from (the )?office|"
                 r"on vacation|currently travelling|limited access to (my )?email", re.I)
OPTOUT = re.compile(r"\bunsubscribe\b|remove me|stop (e-?mailing|sending)|do not (contact|email)|don't (contact|email)|"
                    r"not interested|no longer (with|at|part of)|has left the (company|organi[sz]ation)", re.I)


def classify(msg: Inbound) -> str:
    sender = msg.sender_addr
    if "mailer-daemon" in sender or "postmaster" in sender or BOUNCE_SUBJECT.search(msg.subject):
        return "bounce"
    auto = msg.headers.get("auto-submitted", "no").lower()
    if (auto and auto != "no") or "x-autoreply" in msg.headers or "x-autorespond" in msg.headers \
            or OOO.search(msg.subject) or OOO.search(msg.body[:400]):
        # "has left the company" auto-replies are a dead address in practice
        return "optout" if OPTOUT.search(msg.body[:600]) and "no longer" in msg.body[:600].lower() else "ooo"
    if OPTOUT.search(msg.body[:600]):
        return "optout"
    return "reply"


def _bounced_addresses(msg: Inbound, mailed: set[str]) -> set[str]:
    failed = {a.strip().lower() for a in msg.headers.get("x-failed-recipients", "").split(",") if a.strip()}
    failed |= {a.lower() for a in EMAIL_IN_TEXT.findall(msg.body)}
    return failed & mailed


def sync(conn: sqlite3.Connection, cfg: Config, mailer: Mailer) -> dict[str, int]:
    stats = dict(checked=0, reply=0, bounce=0, ooo=0, optout=0)
    if mailer.dry_run:
        return stats
    mailed = conn.execute("SELECT * FROM leads WHERE touches_sent > 0").fetchall()
    if not mailed:
        return stats
    by_email = {r["email"]: r for r in mailed}
    by_thread = {r["thread_id"]: r for r in mailed if r["thread_id"]}
    by_msgid = {}
    for row in conn.execute("SELECT lead_id, msg_id FROM messages WHERE direction = 'out' AND msg_id != ''"):
        by_msgid[row["msg_id"].strip()] = row["lead_id"]
    by_id = {r["id"]: r for r in mailed}
    seen = {r["gm_msgid"] for r in conn.execute("SELECT gm_msgid FROM messages WHERE direction = 'in' AND gm_msgid != ''")}

    last = get_meta(conn, "last_sync_at")
    first_sent = conn.execute("SELECT MIN(at) AS t FROM messages WHERE direction = 'out'").fetchone()["t"]
    since = datetime.fromisoformat(last or first_sent) - timedelta(days=2)
    started = utcnow_iso()

    for msg in mailer.inbound_since(since):
        if msg.gm_msgid and msg.gm_msgid in seen:
            continue
        stats["checked"] += 1
        label = classify(msg)
        leads: list[sqlite3.Row] = []
        if label == "bounce":
            leads = [by_email[a] for a in _bounced_addresses(msg, set(by_email))]
            if not leads and msg.thread_id in by_thread:
                leads = [by_thread[msg.thread_id]]
        else:
            lead = by_thread.get(msg.thread_id) or by_email.get(msg.sender_addr)
            if lead is None:
                refs = (msg.headers.get("in-reply-to", "") + " " + msg.headers.get("references", "")).split()
                lead_id = next((by_msgid[r] for r in refs if r in by_msgid), None)
                lead = by_id.get(lead_id) if lead_id else None
            leads = [lead] if lead is not None else []
        for lead in leads:
            _apply(conn, cfg, lead, msg, label)
            stats[label] += 1
        if msg.gm_msgid:
            seen.add(msg.gm_msgid)
    set_meta(conn, "last_sync_at", started)
    maybe_pause_for_bounces(conn, cfg)
    return stats


def _apply(conn: sqlite3.Connection, cfg: Config, lead: sqlite3.Row, msg: Inbound, label: str) -> None:
    conn.execute(
        "INSERT INTO messages(lead_id, touch_n, direction, kind, subject, body, gm_msgid, thread_id, at, day) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (lead["id"], lead["touches_sent"], "in", label, msg.subject, msg.body[:20000], msg.gm_msgid, msg.thread_id,
         msg.received_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(), local_day(cfg, msg.received_at)),
    )
    new_state = {"bounce": "bounced", "optout": "closed", "reply": "replied"}.get(label)
    # A reply always wins; a later bounce/ooo never downgrades a lead someone answered.
    current = conn.execute("SELECT state FROM leads WHERE id = ?", (lead["id"],)).fetchone()["state"]
    if new_state and current not in ("replied",) and not (current == "closed" and new_state == "bounced"):
        conn.execute("UPDATE leads SET state = ?, note = ? WHERE id = ?", (new_state, f"{label}: {msg.subject[:120]}", lead["id"]))
    conn.commit()
    if label in ("bounce", "optout"):
        add_to_suppression(cfg.path("suppression"), [lead["email"]])


def bounce_rate(conn: sqlite3.Connection, cfg: Config) -> tuple[int, int]:
    """(bounced, sample) over the last bounce_window leads first mailed since the last resume."""
    resumed = get_meta(conn, "resumed_at", "")
    rows = conn.execute(
        "SELECT l.state FROM leads l JOIN messages m ON m.lead_id = l.id AND m.direction = 'out' AND m.touch_n = 1 "
        "WHERE m.at >= ? ORDER BY m.at DESC LIMIT ?", (resumed, cfg.limits.bounce_window)).fetchall()
    return sum(1 for r in rows if r["state"] == "bounced"), len(rows)


def maybe_pause_for_bounces(conn: sqlite3.Connection, cfg: Config) -> str:
    bounced, n = bounce_rate(conn, cfg)
    if n >= cfg.limits.bounce_min_sample and bounced / n >= cfg.limits.bounce_pause_threshold:
        reason = f"auto-paused: {bounced}/{n} recent first emails bounced (threshold {cfg.limits.bounce_pause_threshold:.0%})"
        if get_meta(conn, "paused") != "1":
            set_meta(conn, "paused", "1")
            set_meta(conn, "pause_reason", reason)
        return reason
    return ""
