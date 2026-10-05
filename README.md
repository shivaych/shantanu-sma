# sma: Shantanu's mass mail agent

Cold outreach for **Shantanu Kumar** (IIT Kharagpur) asking HR heads about **SDE internships**. Modelled on
`D:\Intern\mass mail agent` (`mma`): plain-text, template-based emails (no LLM), Gmail SMTP + IMAP with an
App Password, resume attached, warm-up caps, threaded follow-ups, reply and bounce detection, and auto-pause.

**Mailing database:** only `C:\Users\HP\Downloads\CompanyWise HR contact (1).pdf` (1,842 HR contacts:
SNo, Name, Email, Title, Company). Nothing from the reference agent's `mail data/` is used.

## Setup (once)

```powershell
cd D:\shantanu
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
copy .env.example .env      # put Shantanu's Gmail App Password in it
```

The App Password: on `shantanukumar.iitkgp@gmail.com` turn on 2-step verification, then create one at
https://myaccount.google.com/apppasswords. IMAP must be enabled in Gmail settings (it is by default).

## Daily flow

```powershell
.venv\Scripts\sma import            # read the PDF into data\leads.db (safe to re-run; --inspect to just look)
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
40-110 s random gap between emails, only inside the send window (10:00-17:30 IST, Mon-Fri). The laptop has to be
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
`suppress`) do the same open -> command -> save. Email copy in `profile/templates/` is changed by a normal push.

## How it behaves

| | |
|---|---|
| Email copy | `profile/templates/touch1.txt` (subject + body), `touch2.txt`, `touch3.txt` (follow-up bodies) |
| Resume | `profile/Shantanu_resume.pdf`, attached to touch 1 only |
| Sequence | touch 1 on day 0, follow-ups on days 4 and 10, in the same Gmail thread; any reply stops it |
| Daily caps | new emails ramp 30, 50, 75, 100, 125, then 150 per sending day; 180 total including follow-ups |
| Spacing | at most 1 new email per company (email domain) per day; follow-ups go before new emails |
| Replies | `replied` (stop and read it), out-of-office ignored, "not interested / unsubscribe / has left" -> `closed` + suppressed |
| Bounces | `bounced` + added to `data/suppression.txt`; 10%+ bounces over the last 50 first emails auto-pauses |
| Gmail refusal | any SMTP "sending denied" / limit error pauses the campaign immediately |
| Never | sends to an address twice for the same touch, to a role inbox (hr@, careers@), or to a name it can't greet |

All limits are in `config.yaml`. With 1,842 contacts and the default ramp, touch 1 reaches everyone in about
3 weeks of weekdays.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
