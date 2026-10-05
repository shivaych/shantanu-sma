"""sma: Shantanu's mass mail agent.  `sma --help` for the commands."""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config as config_mod
from . import contacts, inbox, runner, scheduler, templates
from .db import STATES, connect, get_meta, lead_by_email, set_meta, set_state, utcnow_iso
from .mailer import Mailer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Cold outreach for Shantanu Kumar to the HR contact PDF.")
for _stream in (sys.stdout, sys.stderr):      # Windows consoles default to cp1252, which can't print "→" or "²"
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
con = Console()


def _ctx():
    cfg = config_mod.load()
    return cfg, connect(cfg.path("db"))


def _lead_or_exit(conn, email: str):
    lead = lead_by_email(conn, email)
    if lead is None:
        con.print(f"[red]no lead {email}[/]")
        raise typer.Exit(1)
    return lead


@app.command("import")
def import_cmd(pdf: Optional[Path] = typer.Option(None, help="defaults to contacts.pdf in config.yaml"),
               inspect: bool = typer.Option(False, help="show what would be imported, write nothing")):
    """Read the HR contact PDF into the lead database (safe to re-run)."""
    cfg, conn = _ctx()
    path = pdf or Path(cfg.contacts.pdf)
    if not path.exists():
        con.print(f"[red]{path} not found[/]")
        raise typer.Exit(1)
    rows = contacts.read_pdf(path)
    if inspect:
        t = Table("SNo", "Name", "First", "Email", "Title", "Company")
        for c in rows[:25] + rows[-5:]:
            t.add_row(str(c.sno), c.name, c.first_name, c.email, c.title, c.company)
        con.print(t)
        con.print(f"{len(rows)} rows read from {path.name}")
        return
    stats = contacts.import_contacts(conn, rows, cfg.path("suppression"), cfg.contacts.skip_role_addresses)
    con.print(stats)


@app.command()
def preview(n: int = typer.Option(3, help="how many leads"), touch: int = typer.Option(1), email: Optional[str] = None):
    """Print rendered emails for the next leads in the queue (or one lead) without sending."""
    cfg, conn = _ctx()
    problems = templates.check_all(cfg)
    if problems:
        con.print("[red]template problems:[/]\n  " + "\n  ".join(problems))
    leads = [_lead_or_exit(conn, email)] if email else conn.execute(
        "SELECT * FROM leads WHERE state = 'new' ORDER BY sno, id LIMIT ?", (n,)).fetchall()
    for lead in leads:
        subject, body = templates.render(lead, touch, cfg)
        con.rule(f"{lead['email']}  |  {lead['title']} @ {lead['company']}")
        con.print(f"[bold]Subject:[/] {subject}\n", highlight=False)
        con.print(body, highlight=False, markup=False)
        if touch == 1:
            r = templates.resume_path(cfg)
            con.print(f"\n[dim]attachment: {r.name if r else 'NONE (resume file missing!)'}[/]")


@app.command()
def check():
    """Log in to Gmail SMTP + IMAP with the App Password and lint the templates."""
    cfg, _ = _ctx()
    problems = templates.check_all(cfg)
    con.print("[green]templates ok[/]" if not problems else "[red]" + "\n".join(problems) + "[/]")
    con.print(f"resume: {templates.resume_path(cfg) or '[red]missing[/]'}")
    m = Mailer(cfg, dry_run=False)
    try:
        m.check()
        con.print(f"[green]Gmail SMTP + IMAP login ok for {cfg.me.email}[/]")
    finally:
        m.close()
    con.print(f"dry_run is {'ON (nothing will be sent)' if cfg.dry_run else '[bold red]OFF: sma run sends real email[/]'}")


@app.command("test-mail")
def test_mail(to: str, touch: int = typer.Option(1), email: Optional[str] = typer.Option(None, help="lead to render (default: first in queue)")):
    """Send ONE real email (rendered for a lead) to your own address, to see exactly what HR will get."""
    cfg, conn = _ctx()
    lead = _lead_or_exit(conn, email) if email else conn.execute("SELECT * FROM leads WHERE state = 'new' ORDER BY sno, id").fetchone()
    if lead is None:
        con.print("[red]no leads; run `sma import` first[/]")
        raise typer.Exit(1)
    m = Mailer(cfg, dry_run=False)
    try:
        msg = runner.build_for(conn, cfg, m, lead, touch, to=to)
        m.send(msg)
    finally:
        m.close()
    con.print(f"[green]sent '{msg['Subject']}' (as for {lead['email']}) to {to}[/]")


@app.command()
def run(anytime: bool = typer.Option(False, help="ignore the send window and the spreading across runs"),
        max: Optional[int] = typer.Option(None, "--max", help="cap this run (still within the daily caps)"),
        dry: Optional[bool] = typer.Option(None, "--dry/--live", help="override dry_run from config.yaml")):
    """Sync replies/bounces, then send this run's share of today's emails. Schedule it hourly."""
    cfg, conn = _ctx()
    m = Mailer(cfg, dry_run=dry)
    try:
        stats = runner.run(conn, cfg, m, anytime=anytime, max_this_run=max, log=lambda s: con.print(s, highlight=False))
    finally:
        m.close()
    set_meta(conn, "last_run_at", utcnow_iso())
    con.print(stats)
    if m.dry_run and stats.get("sent"):
        con.print(f"[yellow]dry run: emails written to {cfg.path('outbox')}, database unchanged[/]")


@app.command()
def sync():
    """Fetch replies and bounces only."""
    cfg, conn = _ctx()
    m = Mailer(cfg, dry_run=False)
    try:
        con.print(inbox.sync(conn, cfg, m))
    finally:
        m.close()


@app.command()
def pause():
    """Stop sending (replies are still synced)."""
    _, conn = _ctx()
    set_meta(conn, "paused", "1")
    set_meta(conn, "pause_reason", "paused by hand")
    con.print("paused")


@app.command()
def resume():
    """Continue sending. The bounce-rate window restarts from now."""
    _, conn = _ctx()
    set_meta(conn, "paused", "0")
    set_meta(conn, "resumed_at", utcnow_iso())
    con.print("resumed")


@app.command()
def suppress(emails: list[str]):
    """Never mail these addresses (or @domain.com)."""
    cfg, conn = _ctx()
    n = contacts.add_to_suppression(cfg.path("suppression"), emails)
    for e in emails:
        lead = lead_by_email(conn, e)
        if lead is not None and lead["state"] == "new":
            set_state(conn, lead["id"], "skipped", "suppressed by hand")
        if e.startswith("@"):
            conn.execute("UPDATE leads SET state = 'skipped', note = 'suppressed by hand' WHERE domain = ? AND state = 'new'",
                         (e[1:].lower(),))
            conn.commit()
    con.print(f"{n} added to {cfg.path('suppression')}")


@app.command()
def leads(state: Optional[str] = typer.Option(None, help=" | ".join(STATES)), limit: int = 50):
    """List leads."""
    _, conn = _ctx()
    q, args = "SELECT * FROM leads", []
    if state:
        q, args = q + " WHERE state = ?", [state]
    t = Table("SNo", "Email", "Name", "Company", "State", "Touches", "Note")
    for r in conn.execute(q + " ORDER BY sno, id LIMIT ?", (*args, limit)):
        t.add_row(str(r["sno"]), r["email"], r["name"], r["company"], r["state"], str(r["touches_sent"]), r["note"][:50])
    con.print(t)


@app.command()
def show(email: str):
    """Everything sent to and received from one lead."""
    _, conn = _ctx()
    lead = _lead_or_exit(conn, email)
    con.print(dict(lead))
    for m in conn.execute("SELECT * FROM messages WHERE lead_id = ? ORDER BY at", (lead["id"],)):
        con.rule(f"{m['direction']} {m['kind']} touch {m['touch_n']}  {m['at']}")
        con.print(f"Subject: {m['subject']}\n\n{m['body']}", highlight=False, markup=False)


@app.command("set")
def set_cmd(email: str, state: str, note: str = ""):
    """Change a lead's state by hand, e.g. `sma set x@y.com skipped`."""
    _, conn = _ctx()
    lead = _lead_or_exit(conn, email)
    set_state(conn, lead["id"], state, note or f"set by hand to {state}")
    con.print(f"{email}: {lead['state']} -> {state}")


@app.command()
def report():
    """Funnel, today's numbers, recent replies."""
    cfg, conn = _ctx()
    now = scheduler.now_local(cfg)
    today = now.date().isoformat()
    counts = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM leads GROUP BY state")}
    t = Table("State", "Leads")
    for s in STATES:
        t.add_row(s, str(counts.get(s, 0)))
    con.print(t)
    t1, total = scheduler.sent_today(conn, today)
    cap = scheduler.touch1_cap(conn, cfg, today)
    mailed = conn.execute("SELECT COUNT(*) AS n FROM leads WHERE touches_sent > 0").fetchone()["n"]
    replied = counts.get("replied", 0) + counts.get("closed", 0)
    bounced = counts.get("bounced", 0)
    con.print(f"today {today}: {t1}/{cap} new, {total}/{cfg.limits.hard_daily_total} total;  "
              f"follow-ups due: {len(scheduler.due_followups(conn, cfg, today))}")
    if mailed:
        con.print(f"mailed {mailed}: reply rate {replied / mailed:.1%}, bounce rate {bounced / mailed:.1%}")
    b, n = inbox.bounce_rate(conn, cfg)
    con.print(f"recent bounce window: {b}/{n}  (auto-pause at {cfg.limits.bounce_pause_threshold:.0%} once {cfg.limits.bounce_min_sample}+ sends)")
    con.print(f"paused: {get_meta(conn, 'paused') == '1'} {get_meta(conn, 'pause_reason') if get_meta(conn, 'paused') == '1' else ''}")
    con.print(f"dry_run: {cfg.dry_run};  last run: {get_meta(conn, 'last_run_at', 'never')}")
    days = conn.execute("SELECT day, SUM(touch_n = 1) AS t1, COUNT(*) AS n FROM messages WHERE direction = 'out' "
                        "GROUP BY day ORDER BY day DESC LIMIT 14").fetchall()
    if days:
        t = Table("Day", "New", "All sends")
        for d in days:
            t.add_row(d["day"], str(d["t1"]), str(d["n"]))
        con.print(t)
    replies = conn.execute("SELECT l.email, l.name, l.company, m.subject, m.body, m.at FROM messages m JOIN leads l ON l.id = m.lead_id "
                           "WHERE m.direction = 'in' AND m.kind = 'reply' ORDER BY m.at DESC LIMIT 10").fetchall()
    for r in replies:
        con.rule(f"reply from {r['name']} ({r['company']}) {r['at']}")
        con.print(r["body"][:600], highlight=False, markup=False)


@app.command()
def export(out: Path = Path("data/leads_export.csv")):
    """Write every lead with its state to CSV."""
    _, conn = _ctx()
    rows = conn.execute("SELECT sno, name, email, title, company, state, touches_sent, first_sent_day, last_sent_day, note "
                        "FROM leads ORDER BY sno, id").fetchall()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys() if rows else [])
        w.writerows([tuple(r) for r in rows])
    con.print(f"{len(rows)} leads -> {out}")


if __name__ == "__main__":
    app()
