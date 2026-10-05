"""config.yaml -> typed Config. Paths in the yaml are relative to the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("SMA_ROOT") or Path(__file__).resolve().parents[2])


@dataclass
class Me:
    name: str = "Shantanu Kumar"
    email: str = ""
    college: str = "IIT Kharagpur"

    @property
    def first(self) -> str:
        return self.name.split()[0] if self.name else ""


@dataclass
class Contacts:
    pdf: str = ""
    skip_role_addresses: bool = True


@dataclass
class Limits:
    warmup: list[int] = field(default_factory=lambda: [30, 50, 75, 100, 125, 150])
    hard_daily_total: int = 180
    max_per_company_per_day: int = 1
    timezone: str = "Asia/Kolkata"
    send_window: dict[str, str] = field(default_factory=lambda: {"start": "10:00", "end": "17:30"})
    send_days: list[str] = field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])
    run_interval_minutes: int = 60
    max_per_run: int = 30
    gap_seconds: list[float] = field(default_factory=lambda: [40, 110])
    bounce_pause_threshold: float = 0.10
    bounce_window: int = 50
    bounce_min_sample: int = 20


@dataclass
class Sequence:
    touch_days: list[int] = field(default_factory=lambda: [0, 4, 10])


@dataclass
class Paths:
    db: str = "data/leads.db"
    suppression: str = "data/suppression.txt"
    outbox: str = "outbox"
    templates: str = "profile/templates"
    resume: str = "profile/Shantanu_resume.pdf"


@dataclass
class Config:
    me: Me = field(default_factory=Me)
    contacts: Contacts = field(default_factory=Contacts)
    limits: Limits = field(default_factory=Limits)
    sequence: Sequence = field(default_factory=Sequence)
    paths: Paths = field(default_factory=Paths)
    dry_run: bool = True
    root: Path = ROOT

    def path(self, name: str) -> Path:
        p = Path(getattr(self.paths, name))
        return p if p.is_absolute() else self.root / p


def _section(cls, data: dict | None):
    known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
    return cls(**known)


def load(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return Config(
        me=_section(Me, raw.get("me")),
        contacts=_section(Contacts, raw.get("contacts")),
        limits=_section(Limits, raw.get("limits")),
        sequence=_section(Sequence, raw.get("sequence")),
        paths=_section(Paths, raw.get("paths")),
        dry_run=bool(raw.get("dry_run", True)),
        root=path.parent,
    )
