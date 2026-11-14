# Enron Email Extraction, Deduplication & MCP Notification Pipeline

This pipeline parses raw Enron emails (RFC 2822) into structured records and stores them in a normalized
SQLite database. It then flags fuzzy duplicates and sends duplicate notices through a Gmail MCP server.
Everything runs from one command: `python main.py`.

> Status: **All 4 tasks complete**, including a verified live send through the Gmail MCP server.

---

## 1. Dataset & subset selection

| | |
|---|---|
| Source | [Enron Email Dataset (CMU)](https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz) |
| Subset | 5 mailboxes, **23,873 emails**. The spec's minimum is 5 mailboxes and 10,000 emails. |

| Mailbox | Person / role | Emails |
|---|---|---:|
| `lay-k` | Kenneth Lay, Chairman & CEO | 5,937 |
| `kitchen-l` | Louise Kitchen, President, Enron Americas | 5,546 |
| `lavorato-j` | John Lavorato, CEO, Enron Americas | 4,685 |
| `skilling-j` | Jeffrey Skilling, CEO | 4,139 |
| `delainey-d` | David Delainey, CEO, Enron Energy Services | 3,566 |

**Why these five:**
- **They're senior executives who email each other**, so the same message often appears in more than one
  mailbox.
- **The folder structures vary.** Four mailboxes use Lotus Notes-style `all_documents` and
  `discussion_threads` folders, which hold copies of inbox and sent mail. That gives duplicate detection
  real duplicates to find. `kitchen-l` uses an Outlook-style layout with deeply nested folders
  (`_americas/*`), which exercises the recursive traversal.
- **The data covers many edge cases.** Lay's public profile brings in many external senders, which
  means more varied header formats and encodings: 7 of the 8 files that aren't valid UTF-8 come from Lay's
  and Skilling's mailboxes.
- **It's 2.4× the minimum volume**, yet the full pipeline still runs in seconds.

---

## 2. Setup

```bash
# Python 3.10+ (tested on 3.10.20 and 3.12.14)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Dataset: download and extract only the 5 selected mailboxes into data/
mkdir -p data && cd data
curl -O https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz
tar -xzf enron_mail_20150507.tar.gz maildir/lay-k maildir/skilling-j maildir/lavorato-j \
    maildir/delainey-d maildir/kitchen-l
cd ..
```

### Gmail MCP server (needed only for `--send-live`)
1. Turn on **2-Step Verification** for your Google account, then create an **App Password** at
   <https://myaccount.google.com/apppasswords>.
2. `cp mcp_config.json.example mcp_config.json` and fill in `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and
   `GMAIL_ALLOWED_RECIPIENTS` (your own address). `mcp_config.json` is gitignored.
3. Optional: register the same server with Claude Code:
   ```bash
   claude mcp add gmail --scope local \
     -e GMAIL_ADDRESS=you@gmail.com -e GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx' \
     -e GMAIL_ALLOWED_RECIPIENTS=you@gmail.com \
     -- "$PWD/.venv/bin/python" "$PWD/mcp_server/gmail_server.py"
   ```
The full walkthrough is in `AI_USAGE.md` §6.

## 3. Running

```bash
python main.py                 # full pipeline, dry run: writes output/replies/*.eml, sends nothing
python main.py --send-live     # also send up to 5 notices through the Gmail MCP server
python main.py --send-live --limit 1     # send just one (--limit 0 = no limit)
python main.py --send-live --notify-to me@gmail.com   # recipient override (default: GMAIL_ADDRESS)
python main.py --fresh         # delete and rebuild enron.db (also resets notification_sent)
python main.py --threshold 95  # stricter duplicate similarity (default 90)
python main.py --help          # all options (paths for the DB, logs, report, replies, MCP config)

sqlite3 -header -column enron.db < sample_queries.sql   # run the sample queries
```

Without `--fresh`, loading is incremental. Emails whose `message_id` is already stored are skipped by
the UNIQUE constraint, so a second run inserts 0 rows. Live sends are recorded in the DB
(`notification_sent`), so later `--send-live` runs continue with the next groups and never send the
same notice twice.

---

## 4. Architecture

```
data/maildir/**  ──►  [1] extract.py  ──►  [2] storage.py  ──►  [3] dedup.py  ──►  [4] notify.py ──► MCP server ──► Gmail
                         │                     │ enron.db            │                  │
                         ▼                     ▼                     ▼                  ▼
                   error_log.txt         (SQLite, schema.sql)  duplicates_report.csv  output/replies/*.eml
                                                                                       output/send_log.csv
```

| Module | Responsibility |
|---|---|
| `main.py` | CLI entry point that runs the stages in order |
| `pipeline/extract.py` | Task 1: find files, parse fields, log failures, report statistics |
| `pipeline/storage.py` + `schema.sql` | Task 2: create the SQLite schema and load records with normalized recipients |
| `sample_queries.sql` | Task 2: example queries, each with its expected output |
| `pipeline/dedup.py` | Task 3: fuzzy duplicate detection, flagging in the DB, `duplicates_report.csv` |
| `pipeline/notify.py` | Task 4: notification template, `.eml` drafts, MCP client for live sends, `send_log.csv` |
| `mcp_server/gmail_server.py` | Task 4: custom Gmail MCP server (stdio) exposing `send_email` |
| `mcp_config.json.example` | Task 4: MCP server config template with placeholder credentials |

### Task 1: extraction design notes
- **Parser.** Uses the stdlib `email` package with the lenient `compat32` policy, so malformed headers
  never raise. Header values are RFC 2047-decoded and may arrive as `Header` objects when they contain
  8-bit bytes.
- **Enron-specific header unfolding.** The source wraps long headers in two different ways. A `\n\t`
  wrap splits a word in the middle (`Commit\n\ttee` → `Committee`), so it's removed entirely. A `\r\n `
  wrap falls at a word boundary, so the space is kept.
- **Dates.** `parsedate_to_datetime` handles numeric offsets and US zone abbreviations. If it fails,
  `dateutil` takes over with an explicit abbreviation map (PST/PDT/CST/CDT/EST/EDT/…). Everything is stored
  as UTC `YYYY-MM-DD HH:MM:SS`.
- **Addresses.** Parsed with `getaddresses` (with `strict=False` where the Python version supports it),
  lowercased and deduplicated. When a list is too malformed to parse, a regex recovers the addresses.
- **Body charset.** Decoded with the declared charset, then UTF-8, then cp1252, then latin-1, so decoding
  can never fail.
- **Body sections.** The earliest forward or reply marker splits the body. Forward markers are Lotus
  `Forwarded by` lines, `Forwarded message` and `Begin forwarded message`. Reply markers are
  `-----Original Message-----`, `On … wrote:`, Outlook `From:/Sent:` blocks and Lotus reply headers.
  Everything after the marker goes to `forwarded_content` or `quoted_content`, and nested messages stay
  inside that section. `>` lines are moved to `quoted_content`.
- **`has_attachment`.** True for a multipart content type, a MIME boundary, `<<file.ext>>` placeholders or
  Lotus `- file.doc` listing lines. The word "attached" on its own doesn't count because it produces too
  many false positives.
- **`headings`.** Found heuristically: underlined lines, numbered or markdown section titles, ALL-CAPS
  title lines, and section labels such as `Agenda:`. Salutations, table rows and timestamps are excluded.
- **Mandatory-field policy.** A missing header means a failed parse, logged with its reason. An empty
  `Subject:` header counts as present, because 1,620 emails really have no subject.
- **Robustness.** Each file is parsed inside its own try/except, so the run never stops on bad input. This
  was tested with empty, binary, header-only, invalid-date and 5 MB single-line files.

### Task 2: database design notes
```
emails (1) ──< email_recipients >── (1) addresses
  id PK                email_id FK → emails.id         id PK
  message_id UNIQUE    address_id FK → addresses.id    email UNIQUE
  date, from_address,  recipient_type ∈ {to,cc,bcc}
  subject, body, …     position (order in header)
  is_duplicate, duplicate_of → emails.message_id,
  similarity_score, notification_sent, notification_date
```
- **SQLite.** The spec names it as the preferred option. It needs no server, the whole database is one
  file, and Python's built-in `sqlite3` module drives it, so no extra dependency is needed.
- **Normalization.** Each distinct address is stored once in `addresses`. `email_recipients` links
  emails to addresses with a role (`to`/`cc`/`bcc`) and a `position`, so the original order of each list
  can be rebuilt (Q4 in `sample_queries.sql`). `from_address` stays a column on `emails`, because the spec
  requires an index on it there.
- **Integrity.**
  - `UNIQUE(message_id)` blocks exact duplicates, and `UNIQUE(source_file)` gives traceability back to
    the raw file.
  - `CHECK` constraints keep the boolean columns at 0/1 and the score between 0 and 100.
  - A row-level `CHECK` means a duplicate flag can never exist without its `duplicate_of` reference.
  - Foreign keys are enforced (`PRAGMA foreign_keys = ON`).
- **Dates.** Stored as UTC text `YYYY-MM-DD HH:MM:SS`, which sorts chronologically. Range queries use
  `idx_emails_date`, as Q8 in `sample_queries.sql` shows with `EXPLAIN QUERY PLAN`.
- **Indexes.** `date`, `from_address` and `subject` are required by the spec. `email_recipients
  (address_id, recipient_type)` supports address lookups, and `duplicate_of` supports the Task 3 and 4
  joins.
- **Transactions.** The whole load runs in one transaction, with a SAVEPOINT for each email. If an insert
  fails, only that email and its recipient rows are rolled back, the error is logged, and the load
  continues.

### Task 3: duplicate detection design notes
- **Definition (from the spec).** Two emails are duplicates when they have the same `from_address`, the
  same subject after stripping leading `Re:`/`Fw:`/`Fwd:` prefixes (repeated forms such as
  `RE: FW: Re[2]:` included), and content that is **≥ 90% similar**.
- **Fuzzy library: [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)** (MIT licence, C++ core).
  `fuzz.ratio` is the normalized Indel similarity, `2 × LCS / (len(a) + len(b)) × 100`. It's like
  Python's `difflib.SequenceMatcher.ratio`, but exact rather than a heuristic, and much faster. Its
  `score_cutoff` parameter skips pairs that can't reach the threshold.
- **What gets compared.** The whole email content: the primary body plus forwarded and quoted content,
  whitespace-normalized. Comparing only the primary body would score two pure forwards (both with an empty
  primary body) as 100% similar even when they forward completely different emails.
- **Blocking.** Only emails with the same (sender, normalized subject) can be duplicates, so the 22,596
  emails split into 11,195 blocks, and fuzzy matching runs only inside the 3,924 blocks that hold 2 or more
  emails. That is about 151k comparisons instead of about 255M for every possible pair, and the whole
  step takes under 1 second.
- **Clustering against the original.** Within a block, emails are taken in `(date, source_file)` order.
  Each one is compared with the **original** (earliest email) of every existing group and joins the
  best-scoring group at 90% or above. Otherwise it becomes the original of a new group. Every
  `similarity_score` is therefore a score against the original, as the report requires. It also stops
  groups from chaining (A≈B and B≈C while A≉C).
- **Tie-break.** 93% of groups are folder copies with *identical* timestamps, so when dates are equal the
  order is `source_file` ascending. The result is deterministic, and re-runs produce an identical report.
- **Flagging.** The earliest email is the original. Every other member gets `is_duplicate = 1`,
  `duplicate_of = <original message_id>` and `similarity_score`. The latest member is the notification
  target (Task 4). Re-runs reset and recompute the dedup columns but never touch the notification
  columns.
- **Known limitation (an effect of the spec's definition, not a bug).** At 90% character similarity,
  short templated notices that differ in a name or date also match. Examples are "Approval for reviewer"
  forwarded for different employees (91%), and a "Staff Meeting" reminder vs. its cancellation (90.1%).
  The Task 4 notification covers this: the sender can reply `CONFIRM` to restore the email. The
  threshold can be changed with `--threshold`.

### Task 4: notification design notes
- **Template.** The body follows the spec's §4.2 template character for character (checked against the
  assignment document). The headers are `To`, `Subject`, `Date` (the time of sending) and
  `References: <message_id of the flagged duplicate>`.
- **Subject.** `[Duplicate Notice] Re: <original subject>`. Any leading `Re:` on the original is removed
  first, so the result never says `Re: Re:`. RFC 5322 §3.6.5 says only one `Re: ` should be used.
- **Dry run (the default).** Writes `output/replies/` with **one `.eml` per flagged duplicate**, as the
  deliverable requires. Each is addressed to that email's real sender, as in the template. File names
  show the group and role: `g0001_latest_<message-id>.eml` is the group's notification target, and
  `g0001_flagged_<message-id>.eml` is any other flagged member.
- **Live (`--send-live`).** Sends **one notice per group, about its latest duplicate**, as spec §4.2
  says. The pipeline is an MCP client: it launches `mcp_server/gmail_server.py` over stdio, checks the
  tool list, and calls `send_email`.
  - Each attempt is appended to `output/send_log.csv`.
  - A success sets `notification_sent = 1` and `notification_date`.
  - Failures are logged with their exact reason, and the run continues.
- **Safety and quota.**
  - Live notices go to the operator's Gmail (`--notify-to`, `$NOTIFY_TO`, or `GMAIL_ADDRESS`), never to
    the real Enron-era senders. The intended recipient is kept in `X-Original-To`.
  - The server can also enforce a recipient allowlist.
  - `--limit` (default 5) and a 1-second gap between sends keep a run well inside Gmail's quota of about
    500 messages per day.
- **Robustness.** A missing or invalid MCP config, a failed login or a network error is logged, and the
  rest of the pipeline still completes.

## 5. Results

### Task 1: extraction (23,873 files, about 6 s)

| Metric | Value |
|---|---:|
| Files found | 23,873 |
| Parsed successfully | 22,596 (94.65%) |
| Failed (logged to `error_log.txt`) | 1,277 |
| ↳ no `To:` header (calendar entries, notes, drafts) | 1,276 |
| ↳ no valid address in `From:` (`pep <performance.>`) | 1 |

| Field | Completeness | Field | Completeness |
|---|---:|---|---:|
| message_id, date, from_address, to_addresses, source_file | 100% | x_cc | 27.05% |
| subject (non-empty) | 92.88% | x_bcc | 0.07% |
| body (non-empty primary body) | 95.10% | content_type | 100% |
| cc_addresses | 26.95% | has_attachment (= True) | 8.76% |
| bcc_addresses | 26.95%¹ | forwarded_content | 14.00% |
| x_from, x_to, x_folder, x_origin | 100% | quoted_content | 23.05% |
| | | headings | 16.16% |

¹ In this corpus the `Bcc:` header is generally a copy of `Cc:`, a known artifact of how the dataset was
converted. Both are stored exactly as parsed.

### Task 2: database (load about 2 s, DB about 72 MB)

| Check | Result |
|---|---|
| `emails` rows | 22,596 (equal to the parsed count) |
| `addresses` (distinct) | 12,082 |
| `email_recipients` links | 192,006 |
| Second run without `--fresh` | 0 inserted, 22,596 skipped by `UNIQUE(message_id)` |
| Inserting a duplicate `message_id` by hand | Rejected: `UNIQUE constraint failed: emails.message_id` |
| Recipient lists rebuilt from the DB vs. parser output | 22,596 of 22,596 match, including order |
| `PRAGMA integrity_check` / `foreign_key_check` | ok / 0 violations |
| Date range (UTC) | 1980-01-01 → 2002-03-12. The earliest are 23 source emails stamped `1979-12-31 16:00 -0800`, a known placeholder date in this corpus. |

### Task 3: duplicate detection (about 1 s)

| Metric | Value |
|---|---:|
| Emails scanned | 22,596 |
| (sender, subject) blocks / blocks with 2+ emails | 11,195 / 3,924 |
| Fuzzy comparisons | 151,558 |
| **Duplicate groups found** | **4,121** |
| **Emails flagged as duplicate** | **9,215** (rows in `duplicates_report.csv`) |
| ↳ exact copies (100%) | 8,440 (mostly the same email stored in `inbox`, `all_documents` and `discussion_threads`) |
| ↳ fuzzy (90–99.99%) | 775 (98–99.99%: 257 · 95–97.99%: 185 · 90–94.99%: 333) |
| **Average group size** | **3.24** emails (original + duplicates) |
| Groups with more than 2 emails / largest group | 3,128 / 68 |

Checks:
- Flagged rows in the DB equal the report rows (9,215).
- Every `duplicate_of` points to an original that is not itself flagged.
- No duplicate is older than its original.
- No duplicate has a different sender from its original.
- The report is identical across re-runs.

### Task 4: notifications

| Check | Result |
|---|---|
| Draft `.eml` files in `output/replies/` | 9,215 (one per flagged duplicate) |
| Notification targets (`*_latest_*.eml`, one per group) | 4,121, each matching the latest duplicate in the DB |
| Draft body vs. the spec's §4.2 template | Identical structure, character for character |
| MCP round trip (pipeline → server → `send_email`) | Works. Offline tests return clear errors for missing credentials, a recipient not on the allowlist, and an unreachable SMTP server, with DB state unchanged. |
| Live send (`--send-live --limit 1`) | ✅ Sent 2026-11-14 19:06:12 UTC. Group 1's notice (intended for david.delainey@enron.com) was redirected to the operator's Gmail. `notification_sent = 1` is set, and the row is in `output/send_log.csv`. |
| Live send from Claude Code (same MCP server) | ✅ Group 2's notice, Message-ID `<179468551110.13604.8733311295844959285@gmail.com>` (see `AI_USAGE.md` §6.3) |
