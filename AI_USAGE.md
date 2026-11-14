# AI Tool Usage

## 1. Tool used
- **Claude Code** v2.1.325 (CLI), model **Claude Opus 5.5** (`claude-opus-5-5`).
- Claude Code features used:
  - Plan mode, to get a design approved before any code was written.
  - A project `CLAUDE.md`, holding the requirements checklist and the working agreement.
  - Structured clarifying questions, used for decisions with real trade-offs.
  - Bash tool runs, for profiling the data and verifying the code.

---

## 2. Prompting strategy

**Approach: give the full spec once, then build one task at a time, with human checkpoints.**
1. I gave the AI the whole assignment document, but asked for **requirements extraction and a dataset
   check first**, with no code. It turned the spec into a checklist in `CLAUDE.md`. That file loads into
   every session, so the requirements stay in front of the model throughout.
2. I asked for **plan mode** covering all four tasks before any code. The AI profiled the real data
   first, so the plan rests on measured facts rather than guesses. For example, 93% of duplicate groups
   share identical timestamps, and two kinds of header folding exist.
3. Implementation goes **one task at a time, with a stop after each** so I can verify the code, the
   output and the numbers before moving on.


### Example prompts

| # | Prompt (lightly edited) | Why it was structured this way |
|---|---|---|
| 1 | "Read the assignment document, extract the relevant information, and check whether the dataset is properly there. Look for rules like .py or .ipynb requirements. Make a CLAUDE.md. There are 4 tasks, Follow in order, stopping at each one so I can verify the task, code and performance. Once all tasks are finished then focus on optimizing, but first need a working prototype. Explain the decisions you take: when there are multiple paths, why you chose one and its pros & cons." | Front-loads the full context but **forbids coding**. Requirements and data checks come first, so the AI finds ambiguities and data problems (for example, 1,239 emails with no `To:` header) before they turn into bugs. The checkpoint rule stops it from producing one unreviewable block of code. |
| 2 | "Now start plan mode for tasks 1, 2, 3 and 4. First I read the plan, then focus on execution." | Plan mode separates design from execution. The AI profiled the dataset during planning and asked targeted questions (MCP server choice, live-send volume) instead of assuming answers. |
| … | *(Tasks 2–4 prompts to be added at their checkpoints)* | |

---

## 3. Iterations & debugging

### 3.1 compat32 returns `Header` objects for 8-bit headers (Task 1)
- **What went wrong:** The AI's first data-profiling script crashed with
  `TypeError: argument of type 'Header' is not iterable`.
- **Root cause:** Under the lenient `compat32` policy, the stdlib `email` package returns an
  `email.header.Header` object instead of a `str` when a header holds raw non-ASCII bytes. That happened in
  exactly one file (`kitchen-l/_americas/regulatory/113.`).
- **Fix:** The parser now sends every header through `decode_header()`, which accepts both `str` and
  `Header`, and decodes each chunk with a fallback chain of declared charset, UTF-8, cp1252, then latin-1.
  The address `rené.lazure@enron.com` now comes through correctly.

### 3.2 The headings heuristic produced false positives (Task 1)
- **What went wrong:** On the first version, random sampling showed salutations (`Dave:`, `Jim:`), table
  rows (`TOTAL 2001 MEXICO CITY\t $954,929`) and newswire timestamps
  (`00:20AM EST 12-DEC-00 MERRILL LYNCH …`) being reported as headings.
- **Fix:**
  - A one-word `Label:` only counts as a heading if it's a known section word (Agenda, Summary, …).
  - `Dear…`/`Hi…` salutations are excluded.
  - Lines with tabs, `$`, or more than 20% digits are rejected as data rows.
  - ALL-CAPS headings must start with a letter.
- **Result:** Heading coverage fell from 22.61% to 16.16% of emails, and the samples now show mostly real
  section titles.

### 3.3 Exact dependency pins broke the "Python 3.10+" requirement (Task 1)
- **What went wrong:** The AI pinned the versions installed under Python 3.12 (`rapidfuzz==3.14.6`). A
  fresh Python 3.10.20 environment then failed:
  `No matching distribution found for rapidfuzz==3.14.6 (Requires-Python >=3.11)`.
- **Fix:** `requirements.txt` now uses compatible ranges (`rapidfuzz>=3.9,<4`, `python-dateutil>=2.9`,
  `mcp>=2.0,<3`).
- **Verification:** The extraction produces **identical parse results and error logs** (ignoring the
  timestamp column) on Python 3.10.20 and 3.12.14.

### 3.4 Profiling miscounted CRLF files; the parser was right (Task 1)
- **What went wrong:** Early profiling predicted 1,239 emails without a `To:` header, but the parser
  reported 1,276.
- **Root cause:** The profiling script found the end of the header block by splitting on `\n\n`. That
  misses 37 files with CRLF line endings, so their bodies were scanned as headers and their forwarded
  `To:` lines were counted. The parser, which follows RFC 5322, was correct.
- **Lesson:** Check AI-generated numbers against a second, independent method.

---

## 4. What I wrote vs. what the AI wrote
*(Finalized at the end of the project.)*

---

## 5. Lessons learned
*(Finalized at the end of the project.)*

---

## 6. MCP integration
*(Written at Task 4: server choice and rationale, setup steps, example prompts, issues, and live-send
evidence.)*
