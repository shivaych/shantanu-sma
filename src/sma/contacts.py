"""Read the CompanyWise HR contact PDF (table: SNo | Name | Email | Title | Company) into leads.

The PDF's plain-text layer drifts: from page 2 on, the Title/Company columns of `pdftotext` output are
shifted against Name/Email, so a text parse would greet people with another company's name. pdfplumber's
table extraction reads the ruled cells instead and keeps each row intact.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db import utcnow_iso

EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")
ROLE_LOCALS = {"hr", "careers", "career", "jobs", "job", "info", "contact", "hello", "admin", "talent", "recruitment",
               "recruiting", "recruiter", "hiring", "people", "support", "office", "team", "mail", "enquiry", "sales"}
HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "er", "ca", "adv", "shri", "smt"}
# A "name" holding any of these is a company or a shared inbox, not a person to greet.
NON_PERSON = {"ltd", "pvt", "llc", "inc", "infotech", "info", "jobs", "job", "careers", "career", "team",
              "recruitment", "recruiter", "technologies", "solutions", "admin", "office", "group", "systems", "img"}


@dataclass
class Contact:
    sno: int
    name: str
    email: str
    title: str
    company: str

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1]

    @property
    def first_name(self) -> str:
        return first_name(self.name)


def _clean(cell: object) -> str:
    return re.sub(r"\s+", " ", str(cell or "")).strip()


def first_name(name: str) -> str:
    """'Dr. Anand K' -> 'Anand', 'K Ramesh' -> 'Ramesh', 'AMIT' -> 'Amit'. Empty when nothing usable."""
    tokens = [t for t in re.split(r"[\s]+", name.strip()) if t]
    if any(t.lower().strip(".,'s") in NON_PERSON or t.lower().endswith("'s") for t in tokens):
        return ""
    tokens = [t for t in tokens if t.lower().strip(".") not in HONORIFICS | {"hr", "hrd"}]   # "Ruchi Hr" -> Ruchi
    for t in tokens:
        bare = t.strip(".,")
        if not re.search(r"[aeiouy]", bare, re.I):          # "Crp Saurabh": an initialism, not the first name
            continue
        if len(bare) > 2 and bare.replace("-", "").replace("'", "").isalpha():
            return bare if any(c.islower() for c in bare[1:]) else bare.capitalize()
    return ""


def clean_company(company: str) -> str:
    return company.strip().rstrip(",;").strip()


def is_role_address(email: str) -> bool:
    return email.split("@", 1)[0].lower() in ROLE_LOCALS


def read_pdf(path: Path) -> list[Contact]:
    import pdfplumber

    out: list[Contact] = []
    cols: dict[str, int] = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    cells = [_clean(c) for c in row]
                    low = [c.lower() for c in cells]
                    if "email" in low:                       # header row (repeated or not, re-read each time)
                        cols = {name: low.index(name) for name in ("sno", "name", "email", "title", "company") if name in low}
                        continue
                    if not cols or not any(cells):
                        continue
                    get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""  # noqa: E731
                    sno = get("sno")
                    out.append(Contact(
                        sno=int(sno) if sno.isdigit() else 0,
                        name=get("name"),
                        email=get("email").lower().replace(" ", ""),
                        title=get("title"),
                        company=clean_company(get("company")),
                    ))
    return repair_truncated(out)


def repair_truncated(rows: list[Contact]) -> list[Contact]:
    """The PDF clips long addresses ("...@examplegrou", "...@sampletech.co") and the clipped letter sometimes leaks
    into the Title cell ("o Head of ..."). Complete an address only when another row shows a full domain that starts
    with the clipped one; anything else stays invalid and is skipped rather than guessed (a guess is a bounce)."""
    domains = {c.domain for c in rows if EMAIL_RE.match(c.email)}
    for c in rows:
        if EMAIL_RE.match(c.email) or "@" not in c.email:
            continue
        if re.match(r"^[a-z] \S", c.title):
            c.title = c.title[2:]
        local, _, dom = c.email.partition("@")
        matches = sorted(d for d in domains if d.startswith(dom) and d != dom)
        if len(matches) == 1:
            c.email = f"{local}@{matches[0]}"
    return rows


def load_suppression(path: Path) -> tuple[set[str], set[str]]:
    """(addresses, domains). A line '@acme.com' suppresses the whole domain."""
    emails, domains = set(), set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if not line or line.startswith("#"):
                continue
            (domains.add(line[1:]) if line.startswith("@") else emails.add(line))
    return emails, domains


def add_to_suppression(path: Path, addresses: list[str]) -> int:
    emails, _ = load_suppression(path)
    new = [a.strip().lower() for a in addresses if a.strip() and a.strip().lower() not in emails]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for a in dict.fromkeys(new):
                f.write(a + "\n")
    return len(new)


def import_contacts(conn: sqlite3.Connection, contacts: list[Contact], suppression: Path, skip_role: bool = True) -> dict[str, int]:
    """Insert new contacts; refresh name/title/company of leads not yet mailed. Never touches mailed leads."""
    sup_emails, sup_domains = load_suppression(suppression)
    stats = dict(rows=len(contacts), added=0, updated=0, invalid=0, duplicate=0, role_address=0, suppressed=0, no_name=0)
    seen: set[str] = set()
    now = utcnow_iso()
    for c in contacts:
        c.company = clean_company(c.company)
        if not EMAIL_RE.match(c.email):
            stats["invalid"] += 1
            continue
        if c.email in seen:
            stats["duplicate"] += 1
            continue
        seen.add(c.email)
        state, note = "new", ""
        if c.email in sup_emails or c.domain in sup_domains:
            state, note, stats["suppressed"] = "skipped", "import: suppressed", stats["suppressed"] + 1
        elif skip_role and is_role_address(c.email):
            state, note, stats["role_address"] = "skipped", "import: role address", stats["role_address"] + 1
        elif not c.first_name:      # "Hi ," is worse than no email
            state, note, stats["no_name"] = "skipped", "import: no usable first name", stats["no_name"] + 1
        existing = conn.execute("SELECT id, state, note, touches_sent FROM leads WHERE email = ?", (c.email,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO leads(sno, email, name, first_name, title, company, domain, state, note, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (c.sno, c.email, c.name, c.first_name, c.title, c.company, c.domain, state, note, now),
            )
            stats["added"] += 1
        elif existing["touches_sent"] == 0 and (existing["state"] == "new" or existing["note"].startswith("import:")):
            # a lead skipped by hand (`sma set`) stays skipped; only import's own decisions are re-made
            conn.execute(
                "UPDATE leads SET sno=?, name=?, first_name=?, title=?, company=?, domain=?, state=?, note=? WHERE id=?",
                (c.sno, c.name, c.first_name, c.title, c.company, c.domain, state, note, existing["id"]),
            )
            stats["updated"] += 1
    conn.commit()
    return stats
