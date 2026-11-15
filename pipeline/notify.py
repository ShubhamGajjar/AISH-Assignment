"""Task 4 - Duplicate notification emails via MCP.

Dry run (always): writes one draft ``.eml`` per flagged duplicate to ``output/replies/``,
addressed to the duplicate's sender exactly as the spec's template shows.

Live (``--send-live``): for each duplicate group, sends a notice about the *latest* duplicate
(the primary target). The pipeline acts as an MCP **client**: it spawns the Gmail MCP server
over stdio, checks that the ``send_email`` tool is exposed, and calls it per message.
Every send attempt is appended to ``output/send_log.csv``; successes set
``notification_sent`` / ``notification_date`` in the DB, so re-runs never re-send.
Live mail is redirected to the operator's own address (the original Enron senders are real,
often external, domains); the intended recipient travels in the ``X-Original-To`` header.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEND_LOG_COLUMNS = ("timestamp", "recipient", "subject", "status", "error")
DRAFT_FROM = "Email Deduplication System <dedup-system@example.com>"

# Body text exactly as specified in section 4.2 of the assignment.
TEMPLATE = """\
This is an automated notification from the Email Deduplication System.

Your email has been identified as a potential duplicate:

  Your Email (Flagged):
    Message-ID:  {dup_message_id}
    Date Sent:   {dup_date}
    Subject:     {dup_subject}

  Original Email on Record:
    Message-ID:  {orig_message_id}
    Date Sent:   {orig_date}

  Similarity Score: {similarity}%

If this was NOT a duplicate and you intended to send this email,
please reply with CONFIRM to restore it to active status.

No action is required if this is indeed a duplicate.
"""

_LEADING_RE = re.compile(r"^\s*(?:re\s*(?:\[\d+\])?\s*:\s*)+", re.I)
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Notification:
    email_id: int
    group: int                 # 1-based group number (groups ordered by original date)
    is_latest: bool            # primary target of the group (sent in live mode)
    already_sent: bool
    to: str                    # sender of the flagged duplicate
    subject: str
    body: str
    references: str            # message_id of the flagged duplicate

    @property
    def filename(self) -> str:
        role = "latest" if self.is_latest else "flagged"
        safe_id = _UNSAFE_FILENAME.sub("_", self.references.strip("<>"))
        return f"g{self.group:04d}_{role}_{safe_id}.eml"


def notice_subject(original_subject: str) -> str:
    """'[Duplicate Notice] Re: <original_subject>' without stacking 'Re: Re:' (RFC 5322 3.6.5)."""
    core = _LEADING_RE.sub("", original_subject or "").strip() or "(no subject)"
    return f"[Duplicate Notice] Re: {core}"


def load_notifications(conn: sqlite3.Connection) -> list[Notification]:
    """One Notification per flagged duplicate, grouped by original and ordered like the report."""
    rows = conn.execute("""
        SELECT d.id, d.message_id, d.date, d.subject, d.from_address, d.similarity_score,
               d.notification_sent, o.message_id AS orig_id, o.date AS orig_date,
               o.subject AS orig_subject
        FROM emails d
        JOIN emails o ON o.message_id = d.duplicate_of
        WHERE d.is_duplicate = 1
        ORDER BY o.date, o.source_file, d.date, d.source_file
    """).fetchall()

    notifications: list[Notification] = []
    group, previous_original = 0, None
    for row in rows:
        if row["orig_id"] != previous_original:
            group, previous_original = group + 1, row["orig_id"]
            if notifications:
                notifications[-1].is_latest = True
        notifications.append(Notification(
            email_id=row["id"], group=group, is_latest=False,
            already_sent=bool(row["notification_sent"]),
            to=row["from_address"],
            subject=notice_subject(row["orig_subject"]),
            body=TEMPLATE.format(
                dup_message_id=row["message_id"], dup_date=f"{row['date']} UTC",
                dup_subject=row["subject"], orig_message_id=row["orig_id"],
                orig_date=f"{row['orig_date']} UTC", similarity=f"{row['similarity_score']:.2f}",
            ),
            references=row["message_id"],
        ))
    if notifications:
        notifications[-1].is_latest = True
    return notifications


def build_message(n: Notification, to: str, sender: str = DRAFT_FROM) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = n.subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["References"] = n.references
    msg.set_content(n.body)
    return msg


def write_drafts(notifications: list[Notification], out_dir: str) -> int:
    """Regenerate output/replies/: one .eml per flagged duplicate (dry-run output)."""
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    for old in path.glob("*.eml"):
        old.unlink()
    written = 0
    for n in notifications:
        try:
            (path / n.filename).write_bytes(build_message(n, to=n.to).as_bytes())
            written += 1
        except Exception as exc:  # one bad draft must not stop the rest
            log.warning("could not write draft for %s: %s", n.references, exc)
    return written


# =========================================================================================
# Live sending through the MCP server
# =========================================================================================
def load_server_config(config_path: str, server_name: str = "gmail") -> dict:
    with open(config_path, encoding="utf-8") as fh:
        servers = json.load(fh).get("mcpServers", {})
    if server_name not in servers:
        raise KeyError(f"server {server_name!r} not found under 'mcpServers' in {config_path}")
    return servers[server_name]


def _server_parameters(cfg: dict):
    from mcp.client.stdio import StdioServerParameters

    command = cfg["command"]
    if command in ("python", "python3"):
        command = sys.executable  # the interpreter running the pipeline (has `mcp` installed)
    elif not os.path.isabs(command) and os.sep in command:
        command = str(REPO_ROOT / command)
    args = [str(REPO_ROOT / a) if a.endswith(".py") and not os.path.isabs(a) else a
            for a in cfg.get("args", [])]
    env = {**os.environ, **cfg.get("env", {})}
    return StdioServerParameters(command=command, args=args, env=env, cwd=str(REPO_ROOT))


def _append_send_log(path: str, row: dict) -> None:
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SEND_LOG_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _result_text(result) -> str:
    texts = [getattr(c, "text", "") for c in (getattr(result, "content", None) or [])]
    return " ".join(t for t in texts if t).strip()


async def _send_via_mcp(cfg: dict, targets: list[Notification], recipient: str,
                        conn: sqlite3.Connection, send_log: str) -> tuple[int, int]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    sent = failed = 0
    async with stdio_client(_server_parameters(cfg)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            log.info("Connected to MCP server; tools exposed: %s", tools)
            if "send_email" not in tools:
                raise RuntimeError("MCP server does not expose a 'send_email' tool")

            for index, n in enumerate(targets):
                if index:
                    await asyncio.sleep(1.0)  # stay well inside Gmail's sending rate limits
                now = datetime.now(timezone.utc)
                status, error = "sent", ""
                try:
                    result = await session.call_tool("send_email", {
                        "to": recipient, "subject": n.subject, "body": n.body,
                        "references": n.references, "original_recipient": n.to,
                    })
                    if getattr(result, "is_error", False):
                        status, error = "failed", _result_text(result) or "send_email returned an error"
                except Exception as exc:
                    status, error = "failed", f"{type(exc).__name__}: {exc}"

                _append_send_log(send_log, {
                    "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "recipient": recipient,
                    "subject": n.subject, "status": status, "error": error,
                })
                if status == "sent":
                    sent += 1
                    conn.execute(
                        "UPDATE emails SET notification_sent = 1, notification_date = ? WHERE id = ?",
                        (now.strftime("%Y-%m-%d %H:%M:%S"), n.email_id),
                    )
                    log.info("  sent  group %d -> %s  %s", n.group, recipient, n.subject)
                else:
                    failed += 1
                    log.warning("  FAILED group %d: %s", n.group, error)
    return sent, failed


def send_live(conn: sqlite3.Connection, notifications: list[Notification], config_path: str,
              recipient: str | None, limit: int, send_log: str) -> tuple[int, int]:
    """Send notices for the latest duplicate of each group not yet notified (up to ``limit``)."""
    cfg = load_server_config(config_path)
    recipient = recipient or os.environ.get("NOTIFY_TO") or cfg.get("env", {}).get("GMAIL_ADDRESS")
    if not recipient:
        raise ValueError("no recipient: pass --notify-to, set NOTIFY_TO, or set GMAIL_ADDRESS in the MCP config")

    pending = [n for n in notifications if n.is_latest and not n.already_sent]
    targets = pending[:limit] if limit > 0 else pending
    log.info("Live mode: %d groups pending notification; sending %d (limit %s) to %s via MCP",
             len(pending), len(targets), limit if limit > 0 else "none", recipient)
    if not targets:
        return 0, 0
    return asyncio.run(_send_via_mcp(cfg, targets, recipient, conn, send_log))


def run_notify(conn: sqlite3.Connection, replies_dir: str, send_live_mode: bool = False,
               config_path: str = "mcp_config.json", recipient: str | None = None,
               limit: int = 5, send_log: str = "output/send_log.csv") -> bool:
    """Write the drafts and, in live mode, send the notices. Returns False if live sending
    was aborted or any send failed (main.py turns that into a non-zero exit code)."""
    notifications = load_notifications(conn)
    groups = sum(n.is_latest for n in notifications)
    drafts = write_drafts(notifications, replies_dir)
    log.info("=" * 64)
    log.info("NOTIFICATIONS")
    log.info("  Duplicate groups (one live notice each) : %6d", groups)
    log.info("  Draft .eml files written (all flagged)  : %6d  -> %s", drafts, replies_dir)
    if not send_live_mode:
        log.info("  Dry run: no email sent (use --send-live to send via the Gmail MCP server)")
        log.info("=" * 64)
        return True
    ok = False
    try:
        sent, failed = send_live(conn, notifications, config_path, recipient, limit, send_log)
        log.info("  Live sends: %d sent, %d failed  (logged to %s)", sent, failed, send_log)
        ok = failed == 0
    except Exception as exc:  # never crash the pipeline on an MCP/config problem
        log.error("  Live sending aborted: %s: %s", type(exc).__name__, exc)
    log.info("=" * 64)
    return ok
