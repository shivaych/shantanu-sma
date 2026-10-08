"""Who gets mailed this run, and how many: warm-up caps, send window, per-company spacing, follow-up due dates."""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .contacts import FREE_MAIL

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now_local(cfg: Config) -> datetime:
    return datetime.now(ZoneInfo(cfg.limits.timezone))


def local_day(cfg: Config, dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(cfg.limits.timezone)).date().isoformat()


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_window(cfg: Config, now: datetime) -> bool:
    w = cfg.limits.send_window
    return DAYS[now.weekday()] in cfg.limits.send_days and _hhmm(w["start"]) <= now.time() < _hhmm(w["end"])


def runs_left_today(cfg: Config, now: datetime) -> int:
    """How many scheduled runs (including this one) remain before the window closes."""
    end = now.replace(hour=_hhmm(cfg.limits.send_window["end"]).hour, minute=_hhmm(cfg.limits.send_window["end"]).minute,
                      second=0, microsecond=0)
    minutes = max(0.0, (end - now).total_seconds() / 60)
    return max(1, math.ceil(minutes / max(1, cfg.limits.run_interval_minutes)))


def sending_days_before(conn: sqlite3.Connection, today: str) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT day) AS n FROM messages WHERE direction = 'out' AND touch_n = 1 AND day < ?",
                       (today,)).fetchone()
    return row["n"]


def touch1_cap(conn: sqlite3.Connection, cfg: Config, today: str) -> int:
    w = cfg.limits.warmup or [0]
    return w[min(sending_days_before(conn, today), len(w) - 1)]


def sent_today(conn: sqlite3.Connection, today: str) -> tuple[int, int]:
    """(touch-1 sends, all sends) today."""
    row = conn.execute("SELECT COALESCE(SUM(touch_n = 1), 0) AS t1, COUNT(*) AS total FROM messages "
                       "WHERE direction = 'out' AND day = ?", (today,)).fetchone()
    return row["t1"], row["total"]


def due_followups(conn: sqlite3.Connection, cfg: Config, today: str) -> list[sqlite3.Row]:
    offsets = cfg.sequence.touch_days
    out = []
    for lead in conn.execute("SELECT * FROM leads WHERE state = 'in_sequence' ORDER BY first_sent_day, id"):
        n = lead["touches_sent"]
        if n >= len(offsets) or not lead["first_sent_day"] or lead["last_sent_day"] == today:
            continue
        due = date.fromisoformat(lead["first_sent_day"]) + timedelta(days=offsets[n])
        if due.isoformat() <= today:
            out.append(lead)
    return out


def fresh_leads(conn: sqlite3.Connection, cfg: Config, today: str, limit: int) -> list[sqlite3.Row]:
    """Touch-1 candidates in list order, at most max_per_company_per_day per email domain today (Gmail etc. exempt)."""
    if limit <= 0:
        return []
    per_domain: dict[str, int] = {}
    for row in conn.execute("SELECT l.domain, COUNT(*) AS n FROM messages m JOIN leads l ON l.id = m.lead_id "
                            "WHERE m.direction = 'out' AND m.touch_n = 1 AND m.day = ? GROUP BY l.domain", (today,)):
        per_domain[row["domain"]] = row["n"]
    cap = cfg.limits.max_per_company_per_day
    picked = []
    for lead in conn.execute("SELECT * FROM leads WHERE state = 'new' AND touches_sent = 0 ORDER BY sno, id"):
        if lead["domain"] not in FREE_MAIL and per_domain.get(lead["domain"], 0) >= cap:
            continue
        per_domain[lead["domain"]] = per_domain.get(lead["domain"], 0) + 1
        picked.append(lead)
        if len(picked) >= limit:
            break
    return picked


@dataclass
class Plan:
    today: str
    touch1_cap: int
    touch1_sent: int
    total_sent: int
    followups: list[sqlite3.Row]
    fresh: list[sqlite3.Row]
    runs_left: int

    @property
    def size(self) -> int:
        return len(self.followups) + len(self.fresh)


def plan(conn: sqlite3.Connection, cfg: Config, now: datetime, max_this_run: int | None = None, spread: bool = True) -> Plan:
    today = now.date().isoformat()
    cap = touch1_cap(conn, cfg, today)
    t1, total = sent_today(conn, today)
    runs = runs_left_today(cfg, now) if spread else 1
    room_total = max(0, cfg.limits.hard_daily_total - total)
    room_t1 = max(0, cap - t1)
    budget = min(cfg.limits.max_per_run if max_this_run is None else max_this_run, room_total)

    fu_all = due_followups(conn, cfg, today)
    # this run's share of what is left today, so the day's mail is spread over the window
    share = math.ceil((len(fu_all) + room_t1) / runs) if runs > 1 else len(fu_all) + room_t1
    budget = min(budget, share)
    followups = fu_all[:budget]
    fresh = fresh_leads(conn, cfg, today, min(room_t1, budget - len(followups)))
    return Plan(today, cap, t1, total, followups, fresh, runs)
