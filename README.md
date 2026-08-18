# exoda-email-autoscout

Scouts your email for **έξοδα** — the invoices and receipts that count as
business expenses — files them by year, mirrors them to a second folder
(OneDrive, Dropbox, a NAS — anywhere the filesystem can reach), and emails
you a summary of each run.

*έξοδα (éxoda)* is Greek for expenses. The tool goes looking for them so
you don't have to, which matters more than it sounds: it handles Greek
invoice mail correctly, and that turns out to be genuinely hard.

Built for the annoying half-hour before you send anything to an
accountant: the invoices exist, they're just scattered across two months
of mail from a dozen vendors, some as PDFs and some behind a "view your
invoice" link.

```
Invoice Sync                          15 August 2026 → 19 August 2026
┌──────────┬──────────────┬───────────┬──────────┐
│  3 New   │ 19 Duplicates│ 7 Flagged │ 0 Errors │
└──────────┴──────────────┴───────────┴──────────┘
              Run cost: $0.00 (no API calls) | 164 messages in 94.7s
```

## What it does

- Searches Gmail using its own query syntax, over IMAP
- Downloads invoice attachments and writes them to two places at once
- Skips anything already filed, so re-running is always safe
- Flags invoices delivered as a **download link** instead of an
  attachment, and puts that link in the report
- Emails an HTML summary with a row per invoice, each linking back to the
  original email

Filenames land as `<Vendor>_<YYYY-MM-DD>_<original name>.pdf` under
`invoices/<year>/` and `<mirror_root>/<year>/`.

## Design notes

**It is a plain script, not an AI agent.** It began as one — an LLM
driving the whole loop — and that turned out to be the wrong tool.
Building the query, filtering attachments, computing dedup keys, counting
results and rendering HTML are mechanical jobs with exactly one right
answer. Asking a model to do them as a side effect of a long tool-use loop
produced a run that correctly skipped four already-filed attachments and
then reported *"0 duplicates"*. Correctness became a function of which
model was configured, which is not a property you want in an expense
record.

Rewritten as Python, every counter increments at the point the action
happens, so the report cannot disagree with what actually occurred. A run
costs nothing, takes seconds instead of a minute and a half, and produces
byte-identical output for identical input.

**One job is still worth a model.** Picking the invoice link out of an
arbitrary marketing email is genuine judgment on a small input. Hand-written
heuristics were tried first and were wrong in embarrassing ways — they
picked the email's own stylesheet, then its marketing homepage. So for
attachment-less mail only, the script asks a model.

Its answer is accepted **only if it exactly matches a URL already
extracted from that message**. The model reads untrusted email and its
output becomes a clickable link in a report, so it could otherwise invent
a URL, or be talked into emitting one by text planted in the email body.
That check bounds both failure modes to "picked a different link that was
really in the email" — and is why a cheap model is a safe default here.
Disable it entirely with `--no-llm`.

## Setup

Requires Python 3.9+ (standard library only — no pip install) and a Gmail
account.

**1. Create a Gmail App Password.** Enable 2-Step Verification, then
generate one at <https://myaccount.google.com/apppasswords>. Save it as
`invoices/.credentials/gmail_imap.json`:

```json
{ "email": "you@gmail.com", "app_password": "abcd efgh ijkl mnop" }
```

No OAuth, no Google Cloud project, no consent screen. The same credential
reads mail over IMAP and sends the report over SMTP.

**2. Copy the example configs** (the real ones are git-ignored):

```bash
cp invoices/_config.example.json  invoices/_config.json
cp invoices/_vendors.example.json invoices/_vendors.json
cp invoices/_exclude.example.json invoices/_exclude.json
```

Set `notify_email` and `mirror_root` in `_config.json`.

**3. Try it without touching anything:**

```bash
python scripts/sync_invoices.py --dry-run
```

## Usage

```bash
python scripts/sync_invoices.py --dry-run          # report only; writes and sends nothing
python scripts/sync_invoices.py                    # normal run
python scripts/sync_invoices.py --since 2026-01-01 # backfill after adding a vendor
python scripts/sync_invoices.py --no-llm           # fully deterministic
python scripts/sync_invoices.py --help             # full reference
```

Nothing is ever deleted or overwritten. Attachments below
`min_attachment_bytes` are never written in the first place, so there is
no cleanup step that could remove a real file.

## Configuration

| File | Purpose |
|---|---|
| `invoices/_vendors.json` | Senders to always collect: `{"match": "acme.com", "name": "Acme"}` |
| `invoices/_exclude.json` | Senders to always ignore — genuine receipts that are personal spending |
| `invoices/_config.json` | Interval, notify address, mirror path, size floor, model settings |
| `invoices/_manifest.json` | Machine-owned. What's been filed; this is what prevents duplicates |

`match` is a plain substring tested against the sender address, so
`acme.com` catches `billing@mail.acme.com` too.

## Scheduling (Windows)

`scripts/run-invoice-sync.ps1` is the unattended entry point: it
reconciles the scheduled interval against `run_interval_days`, logs to
`logs/`, and emails you if the run fails so a breakage isn't silent for
two weeks.

```powershell
$action  = New-ScheduledTaskAction -Execute 'pwsh.exe' `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\scripts\run-invoice-sync.ps1"'
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00 -DaysInterval 15
Register-ScheduledTask -TaskName 'InvoiceSync' -Action $action -Trigger $trigger
```

Use PowerShell 7 (`pwsh.exe`). Windows PowerShell 5.1 writes redirected
output as UTF-16 and needs a BOM to parse non-ASCII source correctly.

## Gotchas worth knowing

**Gmail matches Greek accents exactly.** It folds English case but not
Greek diacritics: `subject:(τιμολόγιο)` and `subject:(τιμολόγιό)` return
different, non-overlapping sets, because possessive phrasing shifts the
accent (*το τιμολόγι**ό** σου*). A real invoice was invisible to the
search for months because of this. Every inflected form is enumerated in
`SUBJECT_KEYWORDS`; prefer adding the sender to `_vendors.json` over
relying on subject keywords.

**No `has:attachment` in the query.** It seems obviously right and is
exactly wrong: an invoice delivered as a link has no attachment, so the
clause excludes precisely the mail the link detection exists to find.
Attachment-less mail is filtered later instead.

**The tool's own reports are excluded.** They carry "invoice" in the
subject and would otherwise be rediscovered forever.

## Tests

```bash
python -m unittest discover -s tests
```

27 tests over attachment filtering, vendor matching, filename sanitising,
link extraction (including Greek wording, `javascript:` rejection, and
refusing to report an unsubscribe link as an invoice) and report
rendering.

## License

MIT
