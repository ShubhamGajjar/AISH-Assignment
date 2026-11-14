"""Task 3 - Duplicate detection & flagging.

Definition (from the spec): two or more emails are duplicates when they share the same
``from_address``, the same subject after normalizing Re:/Fwd: prefixes, and body content
with at least 90% fuzzy similarity. In each group the earliest email is the original; every
other member is flagged ``is_duplicate = 1`` with ``duplicate_of`` set to the original's
message_id. The latest member is the primary notification target (Task 4).

Approach:
  1. Blocking: only emails with the same (from_address, normalized subject) can be
     duplicates, so fuzzy matching runs only inside those blocks (not across all ~22k² pairs).
  2. Leader clustering inside a block, in (date, source_file) order: each email is compared
     with the *original* of every existing group using ``rapidfuzz.fuzz.ratio`` and joins the
     best group scoring >= threshold; otherwise it starts a new group. Scoring against the
     original keeps ``similarity_score`` meaningful (duplicate vs. original) and prevents
     chaining (A~B, B~C, but A !~ C).
"""
from __future__ import annotations

import csv
import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 90.0
REPORT_COLUMNS = (
    "duplicate_message_id", "original_message_id", "subject", "from_address",
    "duplicate_date", "original_date", "similarity_score",
)

# One or more leading reply/forward prefixes: "Re:", "RE[2]:", "Fwd:", "FW: RE:" ...
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*(?:\[\d+\])?\s*:\s*)+", re.I)
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Email:
    id: int
    message_id: str
    date: str
    source_file: str
    from_address: str
    subject: str
    text: str  # whitespace-normalized full content used for similarity


@dataclass
class Group:
    original: Email
    duplicates: list[tuple[Email, float]] = field(default_factory=list)  # (email, score)

    @property
    def size(self) -> int:
        return 1 + len(self.duplicates)

    @property
    def latest(self) -> Email:
        return self.duplicates[-1][0] if self.duplicates else self.original


@dataclass
class DedupStats:
    emails_scanned: int = 0
    blocks: int = 0
    candidate_blocks: int = 0
    comparisons: int = 0
    groups: int = 0
    flagged: int = 0
    exact: int = 0
    fuzzy: int = 0
    groups_over_two: int = 0
    largest_group: int = 0

    @property
    def average_group_size(self) -> float:
        return (self.groups + self.flagged) / self.groups if self.groups else 0.0


def normalize_subject(subject: str) -> str:
    """Strip leading Re:/Fw:/Fwd: prefixes (repeatedly), collapse whitespace, lowercase."""
    return _WHITESPACE.sub(" ", _SUBJECT_PREFIX.sub("", subject or "")).strip().lower()


def comparison_text(body: str, forwarded: str | None, quoted: str | None) -> str:
    """The email's full content (primary + forwarded + quoted), whitespace-normalized.

    Comparing only the primary body would make two pure forwards (empty primary body)
    score 100% even when they forward completely different messages."""
    parts = [body or "", forwarded or "", quoted or ""]
    return _WHITESPACE.sub(" ", "\n".join(parts)).strip()


def load_emails(conn: sqlite3.Connection) -> list[Email]:
    rows = conn.execute(
        "SELECT id, message_id, date, source_file, from_address, subject, body, "
        "forwarded_content, quoted_content FROM emails ORDER BY date, source_file"
    )
    return [
        Email(r["id"], r["message_id"], r["date"], r["source_file"], r["from_address"],
              r["subject"], comparison_text(r["body"], r["forwarded_content"], r["quoted_content"]))
        for r in rows
    ]


def find_duplicate_groups(emails: list[Email], threshold: float = DEFAULT_THRESHOLD,
                          stats: DedupStats | None = None) -> list[Group]:
    """Return groups with at least one duplicate, ordered by the original's (date, source_file)."""
    stats = stats if stats is not None else DedupStats()
    blocks: dict[tuple[str, str], list[Email]] = defaultdict(list)
    for email in emails:
        blocks[(email.from_address, normalize_subject(email.subject))].append(email)
    stats.emails_scanned, stats.blocks = len(emails), len(blocks)

    result: list[Group] = []
    for members in blocks.values():
        if len(members) < 2:
            continue
        stats.candidate_blocks += 1
        members.sort(key=lambda e: (e.date, e.source_file))
        groups: list[Group] = []
        for email in members:
            best_group, best_score = None, -1.0
            for group in groups:
                stats.comparisons += 1
                score = fuzz.ratio(email.text, group.original.text, score_cutoff=threshold)
                if score >= threshold and score > best_score:
                    best_group, best_score = group, score
            if best_group is None:
                groups.append(Group(original=email))
            else:
                best_group.duplicates.append((email, round(best_score, 2)))
        result.extend(g for g in groups if g.duplicates)

    result.sort(key=lambda g: (g.original.date, g.original.source_file))
    for group in result:
        stats.groups += 1
        stats.flagged += len(group.duplicates)
        stats.exact += sum(1 for _, s in group.duplicates if s >= 100.0)
        stats.fuzzy += sum(1 for _, s in group.duplicates if s < 100.0)
        stats.groups_over_two += group.size > 2
        stats.largest_group = max(stats.largest_group, group.size)
    return result


def flag_duplicates(conn: sqlite3.Connection, groups: list[Group]) -> None:
    """Reset previous dedup results, then flag every non-original member of each group.
    Notification columns are deliberately left untouched so re-runs don't lose send state."""
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE emails SET is_duplicate = 0, duplicate_of = NULL, similarity_score = NULL "
                     "WHERE is_duplicate = 1 OR similarity_score IS NOT NULL")
        conn.executemany(
            "UPDATE emails SET is_duplicate = 1, duplicate_of = ?, similarity_score = ? WHERE id = ?",
            [(g.original.message_id, score, email.id) for g in groups for email, score in g.duplicates],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def write_report(groups: list[Group], path: str) -> int:
    """duplicates_report.csv: one row per flagged email (all members except the original)."""
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(REPORT_COLUMNS)
        for g in groups:
            for email, score in g.duplicates:
                writer.writerow([email.message_id, g.original.message_id, email.subject,
                                 email.from_address, email.date, g.original.date, f"{score:.2f}"])
                rows += 1
    return rows


def run_dedup(conn: sqlite3.Connection, report_path: str,
              threshold: float = DEFAULT_THRESHOLD) -> tuple[list[Group], DedupStats]:
    stats = DedupStats()
    groups = find_duplicate_groups(load_emails(conn), threshold, stats)
    flag_duplicates(conn, groups)
    write_report(groups, report_path)
    _log_stats(stats, groups, threshold)
    return groups, stats


def _log_stats(stats: DedupStats, groups: list[Group], threshold: float) -> None:
    log.info("=" * 64)
    log.info("DUPLICATE DETECTION STATISTICS (similarity threshold %.0f%%)", threshold)
    log.info("  Emails scanned                 : %6d", stats.emails_scanned)
    log.info("  (sender, subject) blocks       : %6d  (%d with 2+ emails)",
             stats.blocks, stats.candidate_blocks)
    log.info("  Fuzzy comparisons performed    : %6d", stats.comparisons)
    log.info("  Duplicate groups found         : %6d", stats.groups)
    log.info("  Emails flagged as duplicate    : %6d  (exact 100%%: %d, fuzzy %.0f-99.99%%: %d)",
             stats.flagged, stats.exact, threshold, stats.fuzzy)
    log.info("  Average group size             : %6.2f  (original + duplicates)", stats.average_group_size)
    log.info("  Groups with more than 2 emails : %6d  (largest: %d)",
             stats.groups_over_two, stats.largest_group)
    log.info("=" * 64)
