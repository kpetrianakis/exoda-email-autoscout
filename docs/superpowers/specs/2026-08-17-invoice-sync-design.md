# Gmail Invoice Sync — Design

Date: 2026-08-17. Substantially revised 2026-08-19.

> **Architecture change (2026-08-19).** The sync originally ran as an
> LLM agent (`claude -p` driving `.claude/commands/invoice-sync.md`).
> It now runs as a plain Python script, `scripts/sync_invoices.py`.
> The agent was doing mechanical bookkeeping — building the query,
> filtering attachments, dedup, counting, rendering HTML — and got the
> counts wrong in a way that depended on which model was configured
> (a run skipped four already-filed attachments and reported "0
> duplicates"). Sections below describe the current design; the
> implementation plan is a historical record of how it got here.

## Purpose

Every N days (default 15, user-configurable), unattended, scan Gmail for
invoices from development-related subscriptions (Anthropic, OpenAI,
GitHub, Microsoft 365, and other dev tools), save each invoice attachment
into this project folder and mirror a copy into a second, user-designated
folder — without ever saving the same invoice twice — and send a summary
email after each run.

## Scope

In scope:
- Recurring local automation (every N days, default 15, configurable via a
  local config file — see "Configurable run interval"), starting from
  2026-06-01.
- Identifying candidate emails via a sender allowlist + invoice/receipt
  keywords, restricted to messages with attachments.
- Downloading attachments, deduplicating across runs, filing into two
  folders.
- A summary email after each run, including a list of matching emails that
  had no attachment (e.g. vendors that only link to a hosted invoice page).
- A vendor allowlist that's a plain data file the user edits directly to
  add a new sender/domain, without touching the command file (see "Vendor
  list maintenance").

Out of scope (deferred):
- Actually fetching invoices that are only available via a web link (no
  attachment). These are flagged with enough detail to look at case-by-case
  later and decide whether a per-vendor fetcher is worth building.
- OCR/parsing invoice contents (amounts, line items).
- Cloud-hosted execution — ruled out because the destination folders are
  local Windows paths (including a OneDrive-synced folder) that a cloud
  sandbox cannot reach; see Architecture.

## Architecture

```
Windows Task Scheduler (task "InvoiceSync", every N days)
  -> pwsh.exe scripts/run-invoice-sync.ps1
       - reconciles the scheduled interval against invoices/_config.json
       - logs to logs/run-<timestamp>.log
       -> python scripts/sync_invoices.py
            -> Gmail IMAP (imap.gmail.com:993, App Password)
                 - search via X-GM-RAW, which accepts Gmail's own query syntax
                 - fetch message + attachment bytes over the same connection
            -> writes invoices/<year>/ and <mirror_root>/<year>/
            -> appends invoices/_manifest.json
            -> [only for attachment-less mail] `claude -p` to pick the
               invoice link out of the body; answer accepted only if it
               matches a URL already extracted from that message
            -> python scripts/send_summary_email.py
                 -> Gmail SMTP (smtp.gmail.com:587, same App Password)
```

Everything except the optional link-resolution step is deterministic: the
same mailbox state produces byte-identical output. There is no model in
the filing path, so no run costs money, no API spend limit can block a
run, and no tool-permission rules are involved.

A single IMAP connection does both search and attachment retrieval, so
there is no second round trip per attachment. Gmail's `X-GM-RAW` search
extension is what removes the need for an agent to do discovery: it takes
the identical query syntax the Gmail web UI accepts.

Why an App Password rather than OAuth: it needs no Google Cloud Console
project, no OAuth consent-screen branding, and no token refresh. The same
credential serves IMAP (read) and SMTP (send).

## Folder layout & manifest

- Primary copy: `invoices/<year>/<Vendor>_<YYYY-MM-DD>_<original-filename>`
  (this project folder)
- Mirror copy: `<mirror_root>\<year>\<Vendor>_<YYYY-MM-DD>_<original-filename>`
  (`<year>` = the invoice's email date year, so future years land in their
  own folder automatically, e.g. 2027 invoices go to `...\Dropbox\2027\`)
- Dedup ledger: `invoices/_manifest.json`, one entry per downloaded
  attachment keyed by `(gmail_message_id, attachment_filename)`,
  recording vendor, resolved date, both destination paths, and timestamp
  downloaded. Also stores `last_successful_run` (date, used to compute the
  next scan window) and `last_summary_thread_id` (the summary email's
  thread, set once the wrapper sends it — see "Same-email cost
  reporting").
- Vendor allowlist: `invoices/_vendors.json`, list of
  `{match: "domain-or-email", name: "Display Name"}` entries, seeded at
  setup and edited directly by the user to add new vendors over time (see
  "Vendor list maintenance").
- Run config: `invoices/_config.json`, `{"run_interval_days": 15}` — see
  "Configurable run interval".
- Small/non-document attachments (inline images, signature logos — e.g.
  under ~5KB or common image extensions with no "invoice/receipt" cue in
  the filename) are skipped.

## Gmail matching

Query shape (Gmail search syntax, sent over IMAP via `X-GM-RAW`):

```
(from:(<every "match" in invoices/_vendors.json, OR-joined>)
 OR subject:(invoice OR receipt OR "payment confirmation"
             OR τιμολόγιο OR τιμολόγιό OR τιμολογίου OR τιμολόγια
             OR απόδειξη OR απόδειξή OR απόδειξης OR αποδείξεις
             OR ΤΠΥ OR ΑΠΥ))
after:<since>
```

- `<since>` = `last_successful_run - 3 days` (overlap buffer for
  late-arriving mail; the manifest prevents duplicate filing regardless).
  First run uses `backfill_start` from `_config.json` (2026-06-01).
- **Greek accents must be listed explicitly.** Gmail does *not* fold Greek
  diacritics the way it folds English case. Verified against this mailbox:
  `subject:(τιμολόγιο)` and `subject:(τιμολόγιό)` return different,
  non-overlapping sets, because possessive phrasing shifts the accent
  ("το τιμολόγι**ό** σου"). A real invoice was invisible to the
  search for exactly this reason. Every inflected form is enumerated.
- **No `has:attachment` clause.** It was there originally and was wrong: an
  invoice delivered as a download link has no attachment by definition, so
  the clause excluded precisely the mail the link-detection feature exists
  to find (16 such messages in this mailbox since June). Attachment-less
  mail is instead discarded during processing unless it shows invoice-link
  wording, so the wider query does not make the report noisier.
- The sync's own summary emails are skipped — they carry "invoice" in the
  subject and would otherwise be rediscovered on every future run,
  growing without limit.
- An accountant or bookkeeper is worth listing explicitly when their mail
  comes from a personal address rather than a company domain.
- Senders in `invoices/_exclude.json` are dropped before processing — for
  genuine receipts that are personal spending rather than company
  expenses (food delivery, for example).
- Vendor display name comes from the matching `_vendors.json` entry; for a
  sender that only matched the keyword clause, it is derived from the
  domain's first label (`billing@example.com` → `Example`).

## Vendor list maintenance

**Dropped the reply-based enrichment mechanism** originally specced here
(a run would check its own summary-email thread for a trusted-sender
reply naming a new vendor, and append it automatically). The user decided
the added complexity (an extra Gmail call every run, a second trust
boundary to reason about) wasn't worth it for something simple to do by
hand, and asked instead for direct instructions.

**To add a vendor:** edit `invoices/_vendors.json` directly and add an
entry in the same `{"match": "domain-or-email", "name": "Display Name"}`
shape as the existing ones (`match` is matched case-insensitively against
the sender; can be a full address or a bare domain). No restart needed —
the next scheduled run reads the file fresh. See Task 12 for the exact
command-file changes this required (removing the reply-checking step,
the corresponding Trust-rule carve-out, and the "reply to add a vendor"
line from the summary email).

## Attachment retrieval

Attachments come over **Gmail IMAP using a Google Account App Password**,
on the same connection used for searching.

Why not the Gmail MCP connector, which the first design assumed: it never
returns attachment *bytes*. All 26 `mcp__claude_ai_Gmail__*` tools were
enumerated and tested against 16 real matching threads — the connector
exposes only metadata (`filename`, `id`, `mimeType`) under every field
name and format mode, and has no attachment-download RPC at all. (The MCP
connector is no longer used for anything: search moved to IMAP and sending
moved to SMTP.)

Why not OAuth: an App Password needs no Google Cloud project, no consent
screen, and no token refresh, and does not carry the 7-day expiry an
unpublished OAuth app would impose on an unattended job.

- **One-time setup:** 2-Step Verification on the account, then an App
  Password from `myaccount.google.com/apppasswords`, stored in
  `invoices/.credentials/gmail_imap.json` (`{"email", "app_password"}`),
  git-ignored and never committed. The same credential is reused for SMTP.
- **Mechanism:** `scripts/sync_invoices.py` (standard library only —
  `imaplib`, `email`, `smtplib`) searches with `X-GM-RAW`, fetches
  `(X-GM-MSGID X-GM-THRID RFC822)` per message, and walks the MIME parts
  to pull decoded attachment bytes. Python's `email` module handles
  base64/quoted-printable internally.
- **Manifest keys use the hex Gmail message ID.** `X-GM-MSGID` is the same
  identifier space as the Gmail API's message IDs — decimal over IMAP, hex
  in the API — so the decimal value is converted to hex, keeping dedup
  continuity with entries filed by the earlier MCP-based implementation.
- **Filenames:** RFC 2047 encoded-words are decoded explicitly.
  `get_filename()` only handles RFC 2231 continuations, while Greek
  e-invoicing platforms (observed: Elorus) put an encoded-word directly in
  the filename parameter.
- **Size floor:** attachments below `min_attachment_bytes` (5120) are never
  written, which is how near-empty decorative attachments are rejected
  without anything ever needing to be deleted.
- Attachment bytes never pass through a model's context.

## Run cost reporting

A run costs **$0.00**: no model is involved in scanning, filing or
reporting. The report carries a line stating that, plus how many messages
were scanned and how long it took, e.g.

```
Run cost: $0.00 (no API calls) | 164 messages scanned in 94.7s
```

When the optional link-resolution step runs, the line instead notes the
number of AI lookups made, since those are the only billable work.

Historical note: an elaborate mechanism used to exist for this — the agent
wrote its finished email to `invoices/.pending_summary.json` with a
`{{COST_LINE}}` placeholder, because a `claude -p` run cannot know its own
final cost while still executing; a wrapper substituted the real figure
after exit. All of that is gone, along with the cost it was reporting.

## Use of a model

The filing path uses no model at all. The single exception is choosing the
invoice link inside an attachment-less email, configured by
`use_llm_for_links` (default true) and `llm_model` (default
`claude-haiku-4-5-20251001`) in `invoices/_config.json`, and disableable
per-run with `--no-llm`.

That job is worth a model where the earlier ones were not: it is genuine
judgment on one small input, it runs only on the few flagged messages per
run, and it cannot affect any count. Hand-written heuristics were tried
first and were repeatedly wrong in embarrassing ways — they picked the
email's own stylesheet (`.../css/email.css`), then a marketing homepage,
before ranking was tightened.

**The answer is accepted only if it exactly matches a URL already
extracted from that message.** The model reads untrusted email content and
its output becomes a clickable link in a report, so it could otherwise
invent a plausible URL or be induced to emit one by text planted in the
body. The membership check bounds both failure modes to "picked a
different link that genuinely was in the email". That containment is also
why the cheap model is an acceptable default here: its failure mode is a
worse choice, not a fabricated one.

On any error, timeout, or unrecognised answer the step silently falls back
to the regex ranking, and every flagged row carries a link to the email
itself regardless.

## Summary email

Sent via Gmail `send_message` to the configured notify_email at the end of every
run (whether or not new invoices were found), as both `htmlBody` (a
formatted report — see below) and a plain-text `body` fallback (same
content, unformatted, for clients that don't render HTML), containing:
- Date range scanned.
- New invoices saved: vendor, filename, both destination paths.
- Duplicates skipped (already in manifest).
- Matching emails with **no attachment** (flagged for manual review),
  each with vendor/sender, subject, Gmail thread ID, and a Gmail link —
  enough detail to revisit individually later and decide whether to build a
  fetcher for that vendor's flow.
- Any errors encountered during the run.
- A footer naming both destination folders once (see "Vendor list
  maintenance" for how to add a vendor — it's a manual `_vendors.json`
  edit, not something driven by this email).

**HTML formatting** (added after the first two real runs arrived as
unformatted plain text): a clean, professional-looking report — a header
band, four stat tiles (New/Duplicates/Flagged/Errors), a table for new
invoices, a table for flagged items with real clickable Gmail links, and
an errors list, built with inline CSS (email clients don't reliably
support external stylesheets) and only the sections that have content
(e.g. no empty "Errors" table on a clean run). See Task 11 for the exact
template.

## Unattended-run safety

No one is present to approve prompts, and email content is untrusted
external input. The dominant safety property now is structural: **the
filing path runs no model**, so for scanning, downloading, mirroring,
counting and reporting there is no prompt to inject into and no tool
permission to escape. Email content is parsed as data by ordinary code.

Remaining considerations:

- **The one model call** (choosing an invoice link in attachment-less
  mail) does read untrusted email text. It is invoked with
  `--allowedTools ""` — no tools at all, pure text in, text out — so it
  cannot touch the filesystem, network or mailbox. Its answer is accepted
  only if it exactly matches a URL already extracted from that same
  message, bounding both hallucination and injection to "picked a
  different link that was really in the email". On any error it falls back
  to regex ranking.
- **Links in the report** are HTML-escaped, restricted to `http(s)`, and
  the extracted URL is printed as visible text as well as being an `href`,
  so the destination is inspectable before anyone clicks. Every flagged
  row also carries a link to the email itself, which always works.
- **All interpolated email content** (vendor, sender, subject, filename,
  error text) is HTML-escaped before rendering.
- **Nothing is ever deleted or overwritten.** The size floor prevents bad
  writes rather than cleaning up after them.
- **The credential** is git-ignored and read only by the scripts that need
  it; it is never echoed into logs or reports.
- **Failures are visible:** a non-zero exit triggers a failure
  notification email from the wrapper, so a broken run does not stay
  silent until someone checks 15 days later.

Historical note: when the run was an agent, safety depended on pinning
`--allowedTools` and `--permission-mode`. That turned out to be fragile —
`Write(path)` is not valid `--allowedTools` syntax (only `Edit(path)` is),
so a rule believed to be scoping writes was silently granting nothing,
masked by a global auto-approve setting. Removing the model removed that
entire class of failure.

## Configurable run interval

The recurrence interval is not hardcoded in the Task Scheduler
registration command or in any script — it lives in
`invoices/_config.json` (`{"run_interval_days": 15}`, tracked in git like
`_vendors.json`) and is reconciled automatically:

- At the very start of every run, `scripts/run-invoice-sync.ps1` reads
  `run_interval_days` from `invoices/_config.json` and compares it to the
  currently registered `InvoiceSync` Task Scheduler trigger's
  `DaysInterval`. If they differ, it updates the trigger in place
  (`Set-ScheduledTask`, preserving the existing time-of-day) before
  proceeding with the rest of the run.
- To change the cadence, the user edits `run_interval_days` in
  `invoices/_config.json` — no need to touch Task Scheduler directly, and
  no need to re-run the registration command. The change takes effect
  starting from the very next run (which fires on the *old* interval one
  last time, then reconciles itself onto the new one).
- This is a plain config edit, same pattern as adding a vendor to
  `_vendors.json` — no email, reply, or run-time instruction channel is
  involved anywhere in this automation.

## Task Scheduler configuration

- Trigger: recurring, every `run_interval_days` days (default 15, from
  `invoices/_config.json`), starting 2026-08-17 (or the day the task is
  created).
- Action: run `scripts/run-invoice-sync.ps1` via
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File ...`
- "Wake the computer to run this task" + "run as soon as possible after a
  scheduled start is missed" enabled, so sleep/shutdown near the trigger
  time doesn't silently skip a run.
- Default: "only run when user is logged on" (no stored credentials
  needed). Can switch to "run whether logged on or not" later if desired,
  at the cost of storing the Windows account password in the task.
- Each run's output is logged to `logs/run-<timestamp>.log` for
  troubleshooting.

## Open questions for later (not blocking)

- Vendor allowlist will likely need additions over time as new
  subscriptions appear — the manifest/summary email will surface unmatched
  "invoice"-keyword emails if the keyword clause is later loosened.
- Whether to eventually build fetchers for specific link-only invoice
  vendors will be decided case-by-case from summary email data, not
  upfront.
