"""SQLite state: one row per HR contact (leads), every email in and out (messages), and flags (meta).

Lead states:
  new          imported, never mailed
  in_sequence  touch 1 sent, follow-ups pending
  done         every touch sent, no reply
  replied      a human answered: the sequence stops, read it with `sma show <email>`
  bounced      the address does not exist (also added to the suppression file)
  closed       asked not to be contacted / not interested (also suppressed)
  skipped      never mail (suppressed, role address, set by hand)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

STATES = ("new", "in_sequence", "done", "replied", "bounced", "closed", "skipped")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY,
  sno INTEGER,
  email TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  first_name TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  company TEXT NOT NULL DEFAULT '',
  domain TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'new',
  touches_sent INTEGER NOT NULL DEFAULT 0,
  subject TEXT NOT NULL DEFAULT '',
  thread_id TEXT NOT NULL DEFAULT '',
  first_msg_id TEXT NOT NULL DEFAULT '',
  last_msg_id TEXT NOT NULL DEFAULT '',
  first_sent_day TEXT,
  last_sent_day TEXT,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_leads_state ON leads(state);
CREATE INDEX IF NOT EXISTS ix_leads_thread ON leads(thread_id);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  touch_n INTEGER NOT NULL DEFAULT 0,
  direction TEXT NOT NULL,          -- out | in
  kind TEXT NOT NULL DEFAULT '',    -- out: touch;  in: reply | bounce | ooo | optout
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  msg_id TEXT NOT NULL DEFAULT '',  -- RFC Message-ID
  gm_msgid TEXT NOT NULL DEFAULT '',-- Gmail X-GM-MSGID (inbound dedupe)
  thread_id TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL,                 -- UTC ISO
  day TEXT NOT NULL                 -- campaign-local date, YYYY-MM-DD
);
CREATE INDEX IF NOT EXISTS ix_msg_day ON messages(direction, day);
CREATE INDEX IF NOT EXISTS ix_msg_gm ON messages(gm_msgid);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def lead_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leads WHERE email = ?", (email.strip().lower(),)).fetchone()


def set_state(conn: sqlite3.Connection, lead_id: int, state: str, note: str = "") -> None:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; one of {', '.join(STATES)}")
    if note:
        conn.execute("UPDATE leads SET state = ?, note = ? WHERE id = ?", (state, note, lead_id))
    else:
        conn.execute("UPDATE leads SET state = ? WHERE id = ?", (state, lead_id))
    conn.commit()
