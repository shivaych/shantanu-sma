from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sma import contacts, inbox, runner, scheduler, templates
from sma.config import Config, Limits, Paths
from sma.db import connect, get_meta, lead_by_email
from sma.mailer import Inbound, Mailer, SentRef

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
PDF = Path("C:/Users/HP/Downloads/CompanyWise HR contact (1).pdf")


class FakeMailer(Mailer):
    """Live-mode mailer that records instead of talking to Gmail."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg, dry_run=False)
        self.sent: list = []
        self.inbox: list[Inbound] = []
        self.fail_with: Exception | None = None

    def send(self, msg) -> SentRef:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(msg)
        to = str(msg["To"])
        return SentRef(msg_id=str(msg["Message-ID"]), thread_id=f"t-{to}")

    def inbound_since(self, since):
        return list(self.inbox)

    def close(self) -> None:
        pass


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        root=tmp_path,
        limits=Limits(warmup=[3, 5], hard_daily_total=6, max_per_run=10, bounce_min_sample=4, bounce_window=10,
                      bounce_pause_threshold=0.5),
        paths=Paths(templates=str(ROOT / "profile" / "templates"), resume=str(ROOT / "profile" / "Shantanu_resume.pdf")),
        dry_run=False,
    )


@pytest.fixture
def conn(cfg: Config):
    c = connect(cfg.path("db"))
    rows = [
        contacts.Contact(1, "Asha Rao", "asha@acme.com", "Head HR", "Acme"),
        contacts.Contact(2, "Bina Shah", "bina@acme.com", "TA Lead", "Acme"),        # same company as Asha
        contacts.Contact(3, "Dr. Chirag Mehta", "chirag@beta.io", "CHRO", "Beta,"),
        contacts.Contact(4, "Divya", "divya@gamma.in", "VP HR", "Gamma"),
        contacts.Contact(5, "Esha K", "esha@delta.com", "Head HR", "Delta"),
    ]
    contacts.import_contacts(c, rows, cfg.path("suppression"))
    return c


def at(day: str, hhmm: str = "11:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=IST)


def no_sleep(_s: float) -> None:
    pass


# --- contacts -----------------------------------------------------------------

@pytest.mark.parametrize("name,first", [
    ("Akanksha Puri", "Akanksha"), ("Dr. Anand K", "Anand"), ("K Ramesh", "Ramesh"), ("AMIT", "Amit"),
    ("Crp Saurabh", "Saurabh"), ("Ruchi Hr", "Ruchi"), ("Hrd Ltd", ""), ("Img Infotech", ""), ("Manu's Jobs", ""),
])
def test_first_name(name, first):
    assert contacts.first_name(name) == first


def test_repair_truncated_only_from_a_known_domain():
    rows = [
        contacts.Contact(1, "Ravi Kumar", "ravi@examplegroup.com", "Head HR", "EG"),
        contacts.Contact(2, "Meena R", "meena@examplegrou", "Director - HR", "EG"),
        contacts.Contact(3, "Neha N", "neha@sampleworks.c", "c Head- Human Resources", "SW"),
    ]
    out = contacts.repair_truncated(rows)
    assert out[1].email == "meena@examplegroup.com"
    assert out[2].email == "neha@sampleworks.c"          # no sibling shows the real domain: not guessed
    assert out[2].title == "Head- Human Resources"


def test_import_dedupes_skips_role_and_suppressed(cfg, tmp_path):
    cfg.path("suppression").parent.mkdir(parents=True, exist_ok=True)
    cfg.path("suppression").write_text("x@sup.com\n@blocked.com\n", encoding="utf-8")
    c = connect(cfg.path("db"))
    rows = [
        contacts.Contact(1, "Asha Rao", "asha@acme.com", "", "Acme"),
        contacts.Contact(2, "Asha Rao", "asha@acme.com", "", "Acme"),
        contacts.Contact(3, "Kajal Gupta", "hr@acme.com", "", "Acme"),
        contacts.Contact(4, "X Y", "x@sup.com", "", "Sup"),
        contacts.Contact(5, "Zed Zed", "zed@blocked.com", "", "Blocked"),
        contacts.Contact(6, "Bad", "bad@nodot", "", "Bad"),
    ]
    stats = contacts.import_contacts(c, rows, cfg.path("suppression"))
    assert stats["added"] == 4 and stats["duplicate"] == 1 and stats["invalid"] == 1
    assert stats["role_address"] == 1 and stats["suppressed"] == 2
    assert [r["email"] for r in c.execute("SELECT email FROM leads WHERE state = 'new'")] == ["asha@acme.com"]


def test_company_trailing_comma_cleaned(conn):
    assert lead_by_email(conn, "chirag@beta.io")["company"] == "Beta"
    assert lead_by_email(conn, "chirag@beta.io")["first_name"] == "Chirag"


@pytest.mark.skipif(not PDF.exists(), reason="HR contact PDF not on this machine")
def test_real_pdf_rows_stay_aligned():
    rows = {c.sno: c for c in contacts.read_pdf(PDF)}
    assert len(rows) == 1842
    # pdftotext's layout drifts around row 35; the table read must keep each email next to its own company
    assert rows[35].email.endswith("@" + rows[35].company.lower() + ".com")
    assert all(c.email and c.company for c in rows.values())


# --- templates ----------------------------------------------------------------

def test_all_templates_lint_clean(cfg):
    assert templates.check_all(cfg) == []


def test_touch1_uses_shantanus_copy(cfg, conn):
    subject, body = templates.render(lead_by_email(conn, "asha@acme.com"), 1, cfg)
    body = body.replace("**", "")
    assert subject == "Quick 1-min read inside, shantanu from iit kgp"
    assert body.startswith("Hi Asha,")
    assert "Fitsol - AI Engineering" in body and "1600+ rating on LeetCode/CodeChef" in body
    assert "relevant opportunities at Acme." in body
    assert body.rstrip().endswith("Best regards,\nShantanu Kumar\nIIT Kharagpur")
    assert "#" not in body


def test_lint_catches_problems():
    assert templates.lint("s", "Hi {first_name}, x")
    assert templates.lint("s", "Hi A, I hope this email finds you well")
    assert templates.lint("s", "Hi A, fine") == []


# --- scheduling + sending -----------------------------------------------------

def test_first_day_respects_warmup_and_one_per_company(cfg, conn):
    m = FakeMailer(cfg)
    stats = runner.run(conn, cfg, m, now=at("2026-10-05"), sleep=no_sleep, anytime=True, log=lambda s: None)
    to = [str(x["To"]) for x in m.sent]
    assert stats["touch1"] == 3                                    # warmup day 1 = 3
    assert to == ["asha@acme.com", "chirag@beta.io", "divya@gamma.in"]   # bina waits: Acme already mailed today
    assert all(x.get_content_type() == "multipart/mixed" for x in m.sent)   # resume attached
    lead = lead_by_email(conn, "asha@acme.com")
    assert lead["state"] == "in_sequence" and lead["touches_sent"] == 1 and lead["thread_id"] == "t-asha@acme.com"
    # a second run the same day sends nothing more: the cap is used up
    m2 = FakeMailer(cfg)
    runner.run(conn, cfg, m2, now=at("2026-10-05", "15:00"), sleep=no_sleep, anytime=True, log=lambda s: None)
    assert m2.sent == []


def test_window_blocks_weekend_and_evening(cfg, conn):
    m = FakeMailer(cfg)
    assert runner.run(conn, cfg, m, now=at("2026-10-04"), sleep=no_sleep, log=lambda s: None)["outside_window"]  # Sunday
    assert runner.run(conn, cfg, m, now=at("2026-10-05", "19:00"), sleep=no_sleep, log=lambda s: None)["outside_window"]
    assert m.sent == []


def test_spread_over_remaining_runs(cfg, conn):
    cfg.limits.warmup = [4]
    p = scheduler.plan(conn, cfg, at("2026-10-05", "10:00"))       # 7.5 h left, hourly runs -> 8 runs, 4 to send
    assert p.size == 1


def test_followups_threaded_and_stop_on_reply(cfg, conn):
    m = FakeMailer(cfg)
    runner.run(conn, cfg, m, now=at("2026-10-05"), sleep=no_sleep, anytime=True, log=lambda s: None)
    # Chirag replies; Asha and Divya don't
    reply = Inbound("g1", "t-chirag@beta.io", "Chirag <chirag@beta.io>", "Re: x", "Send me your github please",
                    datetime.now(timezone.utc), {})
    m.inbox = [reply]
    m.sent.clear()
    runner.run(conn, cfg, m, now=at("2026-10-09"), sleep=no_sleep, anytime=True, log=lambda s: None)   # day 4
    assert lead_by_email(conn, "chirag@beta.io")["state"] == "replied"
    fu = [x for x in m.sent if str(x["Subject"]).startswith("Re: ")]
    assert sorted(str(x["To"]) for x in fu) == ["asha@acme.com", "divya@gamma.in"]
    first_id = conn.execute("SELECT first_msg_id FROM leads WHERE email = 'asha@acme.com'").fetchone()[0]
    asha_fu = next(x for x in fu if str(x["To"]) == "asha@acme.com")
    assert asha_fu["In-Reply-To"] == first_id
    assert not any(p.get_filename() for p in asha_fu.walk())        # no resume on follow-ups
    # the reply is not processed twice
    assert inbox.sync(conn, cfg, m)["reply"] == 0


def test_bounces_suppress_and_autopause(cfg, conn):
    cfg.limits.warmup = [10]
    cfg.limits.max_per_company_per_day = 5
    m = FakeMailer(cfg)
    runner.run(conn, cfg, m, now=at("2026-10-05"), sleep=no_sleep, anytime=True, log=lambda s: None)
    assert len(m.sent) == 5
    now = datetime.now(timezone.utc)
    m.inbox = [
        Inbound("b1", "x1", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)",
                "Address not found. Your message wasn't delivered to asha@acme.com", now, {}),
        Inbound("b2", "x2", "mailer-daemon@googlemail.com", "Undeliverable", "",
                now, {"x-failed-recipients": "bina@acme.com, esha@delta.com"}),
        Inbound("o1", "t-divya@gamma.in", "divya@gamma.in", "Automatic reply: SDE internship", "I am out of office",
                now, {"auto-submitted": "auto-replied"}),
    ]
    stats = inbox.sync(conn, cfg, m)
    assert stats["bounce"] == 3 and stats["ooo"] == 1
    assert lead_by_email(conn, "divya@gamma.in")["state"] == "in_sequence"     # OOO keeps the sequence going
    assert "asha@acme.com" in cfg.path("suppression").read_text()
    assert get_meta(conn, "paused") == "1"                                     # 3/5 >= 50%


def test_gmail_refusal_pauses_campaign(cfg, conn):
    m = FakeMailer(cfg)
    m.fail_with = smtplib.SMTPDataError(550, b"5.7.0 Mail sending denied")
    stats = runner.run(conn, cfg, m, now=at("2026-10-05"), sleep=no_sleep, anytime=True, log=lambda s: None)
    assert stats["paused"] and get_meta(conn, "paused") == "1"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_dry_run_leaves_db_untouched(cfg, conn):
    m = Mailer(cfg, dry_run=True)
    stats = runner.run(conn, cfg, m, now=at("2026-10-05"), sleep=no_sleep, anytime=True, log=lambda s: None)
    assert stats["sent"] == 3
    assert len(list(cfg.path("outbox").glob("*.eml"))) == 3
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE state = 'new'").fetchone()[0] == 5


def test_warmup_advances_by_sending_days(cfg, conn):
    m = FakeMailer(cfg)
    runner.run(conn, cfg, m, now=at("2026-10-05"), sleep=no_sleep, anytime=True, log=lambda s: None)
    assert scheduler.touch1_cap(conn, cfg, "2026-10-05") == 3
    assert scheduler.touch1_cap(conn, cfg, "2026-10-06") == 5
    assert scheduler.touch1_cap(conn, cfg, (datetime(2026, 10, 30)).date().isoformat()) == 5


@pytest.mark.parametrize("subject,body,headers,label", [
    ("Delivery Status Notification (Failure)", "", {}, "bounce"),
    ("Out of Office", "I am away", {}, "ooo"),
    ("Re: SDE", "Please unsubscribe me", {}, "optout"),
    ("Automatic reply", "Asha is no longer with the company", {"auto-submitted": "auto-replied"}, "optout"),
    ("Re: SDE", "Sure, can you share your availability?", {}, "reply"),
])
def test_classify(subject, body, headers, label):
    msg = Inbound("1", "t", "someone@acme.com", subject, body, datetime.now(timezone.utc) - timedelta(minutes=1), headers)
    assert inbox.classify(msg) == label


def test_bold_markup_becomes_html_and_plain_is_clean(cfg, conn):
    m = Mailer(cfg, dry_run=True)
    msg = runner.build_for(conn, cfg, m, lead_by_email(conn, "asha@acme.com"), 1)
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "**" not in plain and "IIT Kharagpur" in plain
    assert "<b>IIT Kharagpur</b>" in html and "<b>Acme</b>" in html and "<br>• <b>DSA</b>" in html
    assert any(p.get_filename() == "Shantanu_resume.pdf" for p in msg.walk())
