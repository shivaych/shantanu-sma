"""Read every .xlsx / .csv in the mailing-data folder into Contacts.

The files come from many sources (Apollo exports, Name/Firm/Email sheets, hand-made trackers, sheets with no header
at all), so each sheet is mapped on its own: a header row within the first 30 rows names the columns; without one, the
email column is the one holding the most addresses and the name column the one that reads most like people's names.
One address per row (the first in the preferred email column): the other addresses in a row are usually guessed
variants of the same person, and a guess is a bounce. The same address in several files keeps its most complete row.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from .contacts import FREE_MAIL, Contact, clean_company, first_name

EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Words that make a cell a job title or an organisation, not a person (used only for sheets without a header).
NOT_A_NAME = {
    "manager", "product", "director", "head", "lead", "partner", "partners", "vp", "president", "founder", "co-founder",
    "analyst", "associate", "consultant", "consulting", "capital", "ventures", "venture", "fund", "advisors", "advisory",
    "bank", "india", "limited", "private", "services", "financial", "finance", "global", "hr", "senior", "chief",
    "officer", "executive", "engineer", "intern", "strategy", "operations", "marketing", "sales", "business", "pm",
    "apm", "spm", "ceo", "cto", "coo", "cfo", "investments", "investment", "securities", "management", "company",
    "growth", "national", "grid", "digital", "media", "labs", "tech", "team", "talent", "acquisition", "recruitment",
}
# In a Name column these mean an organisation or a role inbox: no greeting, so the row is skipped on import.
ORG_WORDS = {"capital", "ventures", "venture", "partners", "fund", "funds", "advisors", "advisory", "bank", "limited",
             "ltd", "private", "pvt", "services", "consulting", "securities", "investments", "group", "llp", "inc"}
JUNK_COMPANY = {"vc", "pe", "ib", "na", "n/a", "-", "--", "other", "others", "nil", "none", "startup", "company", "firm",
                "india", "big4", "big 4", "consult", "consulting", "work email", "email", "hr", "product", "linkedin",
                "advisory firm", "fintech companies", "indian vcs", "ed-tech", "startups", "cosmetics", "optics",
                "electronics", "stationary", "health & wellness", "travel & tourism", "beauty & personal care",
                "software & online tools", "travelling & goods accessories", "counsultancy", "ai startups"}
# Never put these in an email, whatever a sheet calls a tab.
OFFENSIVE = re.compile(r"nigg|fuck|shit|bitch|chutiya|\b(?:bc|mc)\b", re.I)
TITLE_IN_COMPANY = re.compile(r"\b(?:director|manager|vice president|head of|analyst|associate|senior)\b")
NAME_TAIL = re.compile(r"\s[-|–,]\s|\(")
MAX_EMPTY_RUN = 200          # some sheets are padded with a million empty rows


def _cell(v: object) -> str:
    return re.sub(r"\s+", " ", "" if v is None else str(v)).strip()


def _sheets(path: Path) -> Iterator[tuple[str, list[list[str]]]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
            yield path.stem, [[_cell(c) for c in r] for r in csv.reader(f)]
        return
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows, empty = [], 0
            for r in ws.iter_rows(values_only=True):
                cells = [_cell(c) for c in r[:60]]
                if any(cells):
                    empty = 0
                    rows.append(cells)
                else:
                    empty += 1
                    if empty > MAX_EMPTY_RUN:
                        break
            yield ws.title, rows
    finally:
        wb.close()


def _emails(cell: str) -> list[str]:
    return [e.lower().strip(".'") for e in EMAIL_FIND.findall(cell)]


def _header_map(row: list[str]) -> dict[str, int] | None:
    """Column indexes for email/name/first/last/title/company, or None when this row is not a header."""
    low = [c.lower().strip(" :") for c in row]
    if any(_emails(c) for c in row):
        return None
    cols: dict[str, int] = {}
    email_cols: list[tuple[int, int]] = []          # (priority, index); lower priority wins
    for i, h in enumerate(low):
        if not h:
            continue
        if "mail" in h and not re.search(r"status|source|confidence|catch|verif|last|date|sent|open|count", h):
            email_cols.append((2 if "personal" in h else 0 if re.search(r"work|professional|company|primary|official", h)
                               else 1, i))
        elif "first name" in h or h in ("first", "firstname"):
            cols.setdefault("first", i)
        elif "last name" in h or h in ("last", "lastname", "surname"):
            cols.setdefault("last", i)
        elif re.search(r"compan|firm|organi[sz]ation|employer|name of the vc|^vc$|^fund", h):
            cols.setdefault("company", i)
        elif re.search(r"title|designation|position|^role|^who$", h):
            cols.setdefault("title", i)
        elif re.search(r"name|^poc$|contact person", h):
            cols.setdefault("name", i)
    if not email_cols:
        return None
    cols["email"] = sorted(email_cols)[0][1]
    cols["email_alt"] = [i for _, i in sorted(email_cols)[1:]]  # type: ignore[assignment]
    return cols


def _looks_like_name(cell: str) -> bool:
    words = cell.replace(".", " ").split()
    if not 1 <= len(words) <= 4 or any(not w.replace("-", "").replace("'", "").isalpha() for w in words):
        return False
    return not any(w.lower() in NOT_A_NAME for w in words) and bool(first_name(cell))


def _infer_map(rows: list[list[str]]) -> dict[str, int] | None:
    width = max((len(r) for r in rows), default=0)
    counts = [sum(1 for r in rows if i < len(r) and _emails(r[i])) for i in range(width)]
    if not counts or max(counts) == 0:
        return None
    email = counts.index(max(counts))
    best, best_score = None, 0.6
    for i in range(width):
        if i == email:
            continue
        vals = [r[i] for r in rows if i < len(r) and r[i]]
        if len(vals) < max(1, len(rows) // 3):
            continue
        score = sum(map(_looks_like_name, vals)) / len(vals)
        if score > best_score:
            best, best_score = i, score
    cols: dict = {"email": email, "email_alt": []}
    if best is not None:
        cols["name"] = best
    return cols


def read_sheet(rows: list[list[str]]) -> list[Contact]:
    head_at, cols = None, None
    for n, r in enumerate(rows[:30]):
        cols = _header_map(r)
        if cols:
            head_at = n
            break
    body = rows[head_at + 1:] if head_at is not None else rows
    if cols is None:
        cols = _infer_map(body)
        if cols is None:
            return []
    get = lambda r, k: r[cols[k]] if k in cols and cols[k] < len(r) else ""  # noqa: E731
    out = []
    for r in body:
        found = _emails(get(r, "email"))
        for alt in cols["email_alt"]:
            if not found and alt < len(r):
                found = _emails(r[alt])
        if not found:
            continue
        name = get(r, "name") or " ".join(x for x in (get(r, "first"), get(r, "last")) if x)
        out.append(tidy(Contact(sno=0, name=name, email=found[0], title=get(r, "title"), company=get(r, "company"))))
    return out


def tidy(c: Contact) -> Contact:
    """Blank out values the merged lists got wrong, so they are filled from a better row or not used at all."""
    local, _, domain = c.email.partition("@")
    label = domain.split(".")[0]
    c.company = clean_company(c.company.split(" | ")[0]).rstrip(".").strip()      # "Gartner | IIT KGP" -> "Gartner"
    c.name = NAME_TAIL.split(c.name)[0].strip()          # "Ravi Nair - Invest Team" -> "Ravi Nair"
    if c.company.isupper():                              # "URBAN COMPANY" -> "Urban Company"; KPMG, EY stay
        c.company = " ".join(w.capitalize() if len(w) > 4 and w.isalpha() else w for w in c.company.split())
    words = [w.lower() for w in re.split(r"[\s.\-]+", c.name) if w]
    comp = c.company.lower()
    if comp in JUNK_COMPANY or "@" in comp or OFFENSIVE.search(comp) or TITLE_IN_COMPANY.search(comp):
        c.company = ""
    elif comp and all(w in local for w in comp.split()):   # ira.sethi@bain.com, "Ira Sethi": the person
        c.name, c.company = c.name or c.company, ""
    elif comp and " " not in comp and (comp in words[1:] or (len(comp) >= 3 and comp in local and comp not in domain)):
        c.company = ""                                   # "Rohit | Verma | rohit.verma@acme.com": a surname
    if (any(w in ORG_WORDS or w in NOT_A_NAME for w in words) or OFFENSIVE.search(c.name)
            or (len(words) == 1 and (words[0] == comp or (words[0] in label and words[0] not in local)))):
        c.name = ""                                      # "Zenith Capital", "Brightline" @brightline.com
    return c


def _completeness(c: Contact) -> int:
    return 2 * bool(c.first_name) + bool(c.company) + bool(c.title)


def _labels(out: list[Contact]) -> set[str]:
    """Company values that are really a tab, collector or surname ("Software & Online Tools", "Priya", "Malhotra").
    Either used on 4+ domains with almost none of them carrying the name (a real firm's name shows up in its domain:
    KPMG), or made only of words that are part of people's names elsewhere in the data and not in its own domains."""
    domains: dict[str, set[str]] = {}
    for c in out:
        if c.company:
            domains.setdefault(c.company, set()).add(c.domain)
    name_words = {w for c in out for w in c.name.lower().split() if len(c.name.split()) >= 2}
    labels = set()
    for comp, doms in domains.items():
        words = comp.lower().split()
        if all(w in name_words for w in words) and not any(w in d for w in words for d in doms):
            labels.add(comp)                             # "Malhotra", "Ira Sethi" on bain.com rows
    for comp, doms in domains.items():
        if len(doms) < 4:
            continue
        word = next((w for w in re.findall(r"[a-z0-9]+", comp.lower()) if len(w) >= 3), "")
        if not word or sum(word in d for d in doms) / len(doms) < 0.2:
            labels.add(comp)
    return labels


def _plausible(company: str, domain: str) -> bool:
    """The company name shows up in its email domain: KPMG India @kpmg.com, EY @in.ey.com, Toddle @toddleapp.com."""
    words = re.findall(r"[a-z0-9]+", company.lower())
    return any(w in domain for w in words if len(w) >= 3) or "".join(words) in domain.split(".")


def read_folder(folder: Path) -> tuple[list[Contact], list[tuple[str, int]]]:
    """(contacts in file order, one row per address; (file / sheet, rows read) for every sheet)."""
    best: dict[str, Contact] = {}
    sources: list[tuple[str, int]] = []
    for path in sorted(p for p in folder.iterdir() if p.suffix.lower() in (".xlsx", ".xlsm", ".csv") and not p.name.startswith("~$")):
        for sheet, rows in _sheets(path):
            got = read_sheet(rows)
            sources.append((f"{path.name} / {sheet}" if path.suffix.lower() != ".csv" else path.name, len(got)))
            for c in got:
                old = best.get(c.email)
                if old is None or _completeness(c) > _completeness(old):
                    best[c.email] = c                   # a replaced key keeps its first position
    out = list(best.values())
    labels = _labels(out)
    for c in out:
        if c.company in labels:
            c.company = ""
    # On a company domain, the plausible name most of its rows use wins: it fills blanks (nkapur@deloitte.com ->
    # Deloitte) and, once 3+ rows agree, overrides other values ("KPMG India" vs "KPMG"). Without one, a value that is
    # not plausible and not used by most of a 5+ row domain is dropped ("Malhotra" on 2 of 248 bain.com rows).
    plausible: dict[str, Counter] = {}
    per_domain: dict[str, Counter] = {}
    for c in out:
        if c.domain in FREE_MAIL:
            continue
        per_domain.setdefault(c.domain, Counter())[c.company] += 1
        if c.company and _plausible(c.company, c.domain):
            plausible.setdefault(c.domain, Counter())[c.company] += 1
    for n, c in enumerate(out, 1):
        c.sno = n
        if c.domain in plausible:
            top, votes = plausible[c.domain].most_common(1)[0]
            if not c.company or votes >= 3:
                c.company = top
        elif c.company and c.domain in per_domain:
            rows = per_domain[c.domain]
            if rows.total() >= 5 and rows[c.company] / rows.total() < 0.5:
                c.company = ""
    return out, sources
