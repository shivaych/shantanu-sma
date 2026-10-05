"""One `sma run`: sync replies/bounces, then send this run's share of today's mail, paced."""
from __future__ import annotations

import random
import smtplib
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime

from . import inbox, scheduler, verify
from .config import Config
from .db import get_meta, set_meta, utcnow_iso
from .mailer import Mailer, SentRef
from .templates import lint, render, touch_count

SYNC_EVERY = 10          # re-check bounces mid-run so a dead batch pauses early


class TemplateError(RuntimeError):
    pass


def build_for(conn: sqlite3.Connection, cfg: Config, mailer: Mailer, lead: sqlite3.Row, touch_n: int, to: str | None = None):
    subject, body = render(lead, touch_n, cfg)
    problems = lint(subject, body)
    if problems:
        raise TemplateError(f"touch{touch_n} for {lead['email']}: " + "; ".join(problems))
    if touch_n == 1:
        return mailer.build(to or lead["email"], subject, body, attach_resume=True)
    refs = " ".join(r["msg_id"] for r in conn.execute(
        "SELECT msg_id FROM messages WHERE lead_id = ? AND direction = 'out' AND msg_id != '' ORDER BY at", (lead["id"],)))
    return mailer.build(to or lead["email"], subject, body, in_reply_to=lead["last_msg_id"], references=refs)


def record_send(conn: sqlite3.Connection, cfg: Config, lead: sqlite3.Row, touch_n: int, msg, ref: SentRef, day: str) -> None:
    body = msg.get_body(preferencelist=("plain",)).get_content()
    conn.execute(
        "INSERT INTO messages(lead_id, touch_n, direction, kind, subject, body, msg_id, thread_id, at, day) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (lead["id"], touch_n, "out", "touch", str(msg["Subject"]), body, ref.msg_id, ref.thread_id, utcnow_iso(), day),
    )
    done = touch_n >= touch_count(cfg)
    conn.execute(
        """UPDATE leads SET touches_sent = ?, state = ?, last_msg_id = ?, last_sent_day = ?,
               subject = CASE WHEN ? = 1 THEN ? ELSE subject END,
               first_msg_id = CASE WHEN ? = 1 THEN ? ELSE first_msg_id END,
               first_sent_day = COALESCE(first_sent_day, ?),
               thread_id = CASE WHEN thread_id = '' THEN ? ELSE thread_id END
           WHERE id = ?""",
        (touch_n, "done" if done else "in_sequence", ref.msg_id, day,
         touch_n, str(msg["Subject"]), touch_n, ref.msg_id, day, ref.thread_id, lead["id"]),
    )
    conn.commit()


def run(conn: sqlite3.Connection, cfg: Config, mailer: Mailer, *, anytime: bool = False, max_this_run: int | None = None,
        now: datetime | None = None, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> dict:
    stats: dict = {"sent": 0, "followups": 0, "touch1": 0, "dry_run": mailer.dry_run}
    if not mailer.dry_run:
        stats["sync"] = inbox.sync(conn, cfg, mailer)
    if get_meta(conn, "paused") == "1":
        log(f"paused ({get_meta(conn, 'pause_reason', 'by hand')}); `sma resume` to continue")
        stats["paused"] = True
        return stats
    now = now or scheduler.now_local(cfg)
    if not anytime and not scheduler.in_window(cfg, now):
        log(f"outside the send window ({cfg.limits.send_window['start']}-{cfg.limits.send_window['end']} "
            f"{'/'.join(cfg.limits.send_days)}, {cfg.limits.timezone}); nothing sent")
        stats["outside_window"] = True
        return stats

    p = scheduler.plan(conn, cfg, now, max_this_run=max_this_run, spread=not anytime)
    log(f"{p.today}: touch-1 cap {p.touch1_cap}, sent {p.touch1_sent} touch-1 / {p.total_sent} total today; "
        f"this run: {len(p.followups)} follow-ups + {len(p.fresh)} new ({p.runs_left} run(s) left in the window)")
    queue = [(lead, lead["touches_sent"] + 1) for lead in p.followups] + [(lead, 1) for lead in p.fresh]

    for i, (lead, touch_n) in enumerate(queue):
        current = conn.execute("SELECT * FROM leads WHERE id = ?", (lead["id"],)).fetchone()
        if current["touches_sent"] != touch_n - 1 or current["state"] not in ("new", "in_sequence"):
            continue                                   # replied / bounced since the plan was made
        if touch_n == 1 and cfg.limits.check_domains:
            domain = current["email"].rsplit("@", 1)[-1]
            if verify.domain_accepts_mail(domain) is False:
                conn.execute("UPDATE leads SET state = 'skipped', note = 'domain has no mail server' WHERE id = ?",
                             (lead["id"],))
                conn.commit()
                stats["dead_domain"] = stats.get("dead_domain", 0) + 1
                log(f"  skipped (domain has no mail server): {current['email']}")
                continue
        msg = build_for(conn, cfg, mailer, current, touch_n)
        try:
            ref = mailer.send(msg)
        except smtplib.SMTPRecipientsRefused:
            conn.execute("UPDATE leads SET state = 'bounced', note = 'recipient refused by SMTP' WHERE id = ?", (lead["id"],))
            conn.commit()
            log(f"  refused: {current['email']}")
            continue
        except (smtplib.SMTPDataError, smtplib.SMTPSenderRefused, smtplib.SMTPAuthenticationError) as e:
            # Google refusing to send ("550 5.7.0 Mail sending denied", daily limit, auth) - stop everything.
            reason = f"auto-paused: Gmail refused to send ({e})"
            set_meta(conn, "paused", "1")
            set_meta(conn, "pause_reason", reason[:500])
            log(reason)
            stats["paused"] = True
            break
        if not mailer.dry_run:
            record_send(conn, cfg, current, touch_n, msg, ref, p.today)
        stats["sent"] += 1
        stats["touch1" if touch_n == 1 else "followups"] += 1
        log(f"  {'[dry] ' if mailer.dry_run else ''}touch {touch_n} -> {current['email']} ({current['company']})")
        if i == len(queue) - 1:
            break
        if not mailer.dry_run and stats["sent"] % SYNC_EVERY == 0:
            inbox.sync(conn, cfg, mailer)
            if get_meta(conn, "paused") == "1":
                log(get_meta(conn, "pause_reason"))
                stats["paused"] = True
                break
        if not mailer.dry_run:
            lo, hi = cfg.limits.gap_seconds
            sleep(random.uniform(lo, hi))
    return stats
