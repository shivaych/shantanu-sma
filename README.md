# sma: Shantanu's mass mail agent

Cold outreach for **Shantanu Kumar** (IIT Kharagpur) asking about **SDE / AI-ML internships**. Modelled on
`D:\Intern\mass mail agent` (`mma`): plain-text, template-based emails (no LLM), Gmail SMTP + IMAP with an
App Password, resume attached, warm-up caps, threaded follow-ups, reply and bounce detection, and auto-pause.

**Sender:** `shantanukumar@kgpian.iitkgp.ac.in` (Google Workspace, App Password), since 2026-10-09.

**Mailing database:** every `.xlsx` / `.csv` in `E:\mail data\New folder` (84 files, ~22,000 unique addresses, about
17,000 with a name to greet). `sma import` maps each sheet's columns on its own (header row, or inferred when there is
none), keeps one address per row, merges duplicates across files, and cleans company names the merged lists got
wrong (tab names, collectors' names, surnames). It also writes `data/contacts_import.csv` to check the mapping by eye.

Until 2026-10-08 the campaign sent from `shantanukumar.iitkgp@gmail.com` to the HR PDF
(`C:\Users\HP\Downloads\CompanyWise HR contact (1).pdf`); it auto-paused at 57% bounces (27 of 47). Its database is
kept in the vault as `data/leads_gmail_campaign.db`, and everyone it mailed is on the suppression list.

## Setup (once)

```powershell
cd D:\shantanu
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
copy .env.example .env      # put Shantanu's Gmail App Password in it
```

The App Password: on `shantanukumar@kgpian.iitkgp.ac.in` turn on 2-step verification, then create one at
https://myaccount.google.com/apppasswords. IMAP must be enabled in Gmail settings (it is by default).

## Daily flow

```powershell
.venv\Scripts\sma import            # read the spreadsheets into data\leads.db (safe to re-run; --inspect to just look)
.venv\Scripts\sma preview --n 5     # read the emails exactly as they will go out
.venv\Scripts\sma check             # Gmail login + template lint + resume present
.venv\Scripts\sma test-mail you@x   # one real email to yourself, with the resume attached
.venv\Scripts\sma run --anytime     # with dry_run: true, writes .eml files to outbox\ (database untouched)
```

When the outbox looks right, set `dry_run: false` in `config.yaml` and schedule `run.bat` every hour:

```powershell
schtasks /Create /TN "sma-run" /SC HOURLY /MO 1 /ST 10:00 /TR "D:\shantanu\run.bat"
```

Each `sma run` first syncs replies and bounces, then sends its share of what is left of today's cap with a
20-45 s random gap between emails, only inside the send window (10:00-17:30 IST, Mon-Fri). The laptop has to be
on and online for scheduled runs; a missed run is simply made up by the next ones.

```powershell
.venv\Scripts\sma report            # funnel, today vs cap, bounce rate, latest replies
.venv\Scripts\sma leads --state replied
.venv\Scripts\sma show someone@company.com
.venv\Scripts\sma set someone@company.com closed
.venv\Scripts\sma pause   /   sma resume
.venv\Scripts\sma suppress a@b.com @wholecompany.com
.venv\Scripts\sma export            # data\leads_export.csv
```

## Running on GitHub (current setup)

`.github/workflows/sma-run.yml` runs `sma run` every hour, 10:00-17:00 IST, Mon-Fri, so the laptop can stay off.
The repo is public, so the lead database, suppression list and resume are never committed in plain form: they live
encrypted on the branch `state` (`scripts/vault.sh`, AES-256). Secrets: `GMAIL_APP_PASSWORD`, `SMA_VAULT_KEY`
(the same key is in the local `.env`; losing it loses the campaign state). The laptop task `sma-run` is disabled.

```bash
bash scripts/vault.sh open           # pull the latest state locally, then sma report / leads / show as usual
gh workflow run sma-run              # an extra run now (still respects the send window and caps)
```

To change the resume: `bash scripts/vault.sh open`, replace `profile/Shantanu_resume.pdf`, then
`bash scripts/vault.sh save`, outside the send window. To change any state locally (`sma set`, `pause`,
`suppress`, `import`) do the same open -> command -> save. Email copy in `profile/templates/` is changed by a normal push.

## How it behaves

| | |
|---|---|
| Email copy | `profile/templates/touch1.txt` (subject + body), `touch2.txt`, `touch3.txt` (follow-up bodies) |
| Resume | `profile/Shantanu_resume.pdf`, attached to touch 1 only |
| Sequence | touch 1 on day 0, follow-ups on days 4 and 10, in the same Gmail thread; any reply stops it |
| Daily caps | new emails ramp 100, 200, 300, 400, then 500 per sending day; 500 total including follow-ups |
| Spacing | at most 10 new emails per company (email domain) per day, Gmail/Yahoo etc. exempt; follow-ups go first |
| Replies | `replied` (stop and read it), out-of-office ignored, "not interested / unsubscribe / has left" -> `closed` + suppressed |
| Bounces | `bounced` + added to `data/suppression.txt`; 50%+ bounces over the last 50 first emails auto-pauses (once 20 are out); a first email to a domain with no mail server (DNS) is skipped |
| Gmail refusal | any SMTP "sending denied" / limit error pauses the campaign immediately |
| Never | sends to an address twice for the same touch, to a role inbox (hr@, careers@), or to a name it can't greet |

All limits are in `config.yaml`. Follow-ups share the 500, so once they start (day 5) fewer than 500 of each day's
emails are new; touch 1 reaches all ~17,000 contacts in roughly 3 months of weekdays.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
