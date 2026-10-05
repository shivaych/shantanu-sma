"""No-LLM drafting from profile/templates/touch{N}.txt.

touch1.txt: subject line (variants split by " || "), blank line, body.
touchN.txt (N > 1): body only; follow-ups are replies in the touch-1 thread ("Re: <subject>").
Lines starting with '#' are notes and are dropped. Unknown placeholders render empty and fail lint.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .config import Config

PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")
BANNED = [
    "i hope this email finds you well", "i hope you're doing well", "to whom it may concern", "i am writing to",
    "touch base", "circle back", "bumping this", "dear sir/madam", "dear sir", "dear madam",
]
MAX_WORDS = 300


class _Missing(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"           # left in place so lint catches it instead of sending a gap


def touch_count(cfg: Config) -> int:
    return len(cfg.sequence.touch_days)


def _read(cfg: Config, n: int) -> str:
    p = cfg.path("templates") / f"touch{n}.txt"
    if not p.exists():
        raise FileNotFoundError(f"{p} missing")
    lines = [ln.rstrip() for ln in p.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines() if not ln.startswith("#")]
    return "\n".join(lines).strip()


def subject_variants(cfg: Config) -> list[str]:
    head = _read(cfg, 1).partition("\n\n")[0]
    return [v.strip() for v in head.split("||") if v.strip()]


def context(lead: sqlite3.Row | dict, cfg: Config) -> _Missing:
    return _Missing(
        first_name=lead["first_name"], company=lead["company"] or "your company", title=lead["title"],
        my_name=cfg.me.name, my_first=cfg.me.first, my_college=cfg.me.college,
    )


def render(lead: sqlite3.Row | dict, touch_n: int, cfg: Config) -> tuple[str, str]:
    """(subject, body). Subject of a follow-up is 'Re: ' + the subject actually used for touch 1."""
    raw = _read(cfg, touch_n)
    ctx = context(lead, cfg)
    variants = subject_variants(cfg)
    first_subject = variants[(lead["sno"] or lead["id"] or 0) % len(variants)].format_map(ctx)
    if touch_n == 1:
        subject = first_subject
        body = raw.partition("\n\n")[2]
    else:
        first = lead["subject"] or first_subject       # the subject touch 1 actually went out with
        subject = first if first.lower().startswith("re:") else f"Re: {first}"
        body = raw
    body = re.sub(r"\n{3,}", "\n\n", body.format_map(ctx)).strip()
    return subject.strip(), body


def lint(subject: str, body: str) -> list[str]:
    problems = []
    text = f"{subject}\n{body}"
    if PLACEHOLDER_RE.search(text):
        problems.append(f"unfilled placeholder {PLACEHOLDER_RE.search(text).group(0)}")
    low = text.lower()
    problems += [f"banned phrase: {b!r}" for b in BANNED if b in low]
    words = len(body.split())
    if words > MAX_WORDS:
        problems.append(f"body is {words} words (max {MAX_WORDS})")
    if not body.lower().startswith("hi ") or body.lower().startswith("hi ,"):
        problems.append("body must open with 'Hi <name>,'")
    if not subject:
        problems.append("empty subject")
    return problems


def check_all(cfg: Config) -> list[str]:
    """Lint every template against a sample lead."""
    sample = {"id": 1, "sno": 1, "first_name": "Asha", "company": "Acme", "title": "Head HR", "subject": ""}
    out = []
    for n in range(1, touch_count(cfg) + 1):
        try:
            s, b = render(sample, n, cfg)
        except FileNotFoundError as e:
            out.append(str(e))
            continue
        out += [f"touch{n}: {p}" for p in lint(s, b)]
    return out


def resume_path(cfg: Config) -> Path | None:
    p = cfg.path("resume")
    return p if p.exists() else None
