"""Task 2 - Database storage.

Creates the SQLite database from ``schema.sql`` and loads extracted records into it:
one ``emails`` row per record, with to/cc/bcc addresses normalized into ``addresses``
and ``email_recipients``. ``INSERT OR IGNORE`` on the UNIQUE ``message_id`` means an
email that is already stored is skipped rather than duplicated, so re-runs are safe.
``stored_source_files`` lets a re-run skip parsing the files that are already loaded.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

EMAIL_COLUMNS = (
    "message_id", "date", "from_address", "subject", "body", "source_file",
    "x_from", "x_to", "x_cc", "x_bcc", "x_folder", "x_origin", "content_type",
    "has_attachment", "forwarded_content", "quoted_content", "headings",
)
RECIPIENT_TYPES = ("to", "cc", "bcc")

_INSERT_EMAIL = (
    f"INSERT OR IGNORE INTO emails ({', '.join(EMAIL_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in EMAIL_COLUMNS)})"
)


@dataclass
class StorageStats:
    inserted: int = 0
    skipped_existing: int = 0
    failed: int = 0
    recipient_links: int = 0


def connect(db_path: str) -> sqlite3.Connection:
    """Open the DB in autocommit mode; transactions are managed explicitly."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def reset_database(db_path: str) -> None:
    """Delete the database file (and SQLite side files) for a fresh rebuild."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)


def init_db(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def stored_source_files(conn: sqlite3.Connection) -> frozenset[str]:
    """``source_file`` of every stored email (UNIQUE column, so the lookup uses its index)."""
    return frozenset(row[0] for row in conn.execute("SELECT source_file FROM emails"))


def _address_cache(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["email"]: row["id"] for row in conn.execute("SELECT id, email FROM addresses")}


def _address_id(conn: sqlite3.Connection, email: str, cache: dict[str, int]) -> int:
    address_id = cache.get(email)
    if address_id is None:
        conn.execute("INSERT OR IGNORE INTO addresses (email) VALUES (?)", (email,))
        address_id = conn.execute("SELECT id FROM addresses WHERE email = ?", (email,)).fetchone()[0]
        cache[email] = address_id
    return address_id


def store_records(conn: sqlite3.Connection, records: list[dict]) -> StorageStats:
    """Insert records in one transaction. Each record is wrapped in a SAVEPOINT, so a
    failure rolls back that email and its recipient rows together and the load continues."""
    stats = StorageStats()
    cache = _address_cache(conn)
    conn.execute("BEGIN")
    try:
        for record in records:
            conn.execute("SAVEPOINT record")
            try:
                values = [record[c] for c in EMAIL_COLUMNS]
                values[EMAIL_COLUMNS.index("has_attachment")] = int(record["has_attachment"])
                cursor = conn.execute(_INSERT_EMAIL, values)
                if cursor.rowcount == 0:  # message_id already stored: UNIQUE constraint
                    stats.skipped_existing += 1
                else:
                    email_id = cursor.lastrowid
                    for rtype in RECIPIENT_TYPES:
                        for position, email in enumerate(record[f"{rtype}_addresses"]):
                            link = conn.execute(
                                "INSERT OR IGNORE INTO email_recipients "
                                "(email_id, address_id, recipient_type, position) VALUES (?, ?, ?, ?)",
                                (email_id, _address_id(conn, email, cache), rtype, position),
                            )
                            stats.recipient_links += link.rowcount
                    stats.inserted += 1
                conn.execute("RELEASE record")
            except sqlite3.Error as exc:  # keep going: one bad record must not stop the load
                conn.execute("ROLLBACK TO record")
                conn.execute("RELEASE record")
                stats.failed += 1
                log.warning("DB insert failed for %s: %s", record.get("source_file"), exc)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    _log_stats(conn, stats)
    return stats


def _log_stats(conn: sqlite3.Connection, stats: StorageStats) -> None:
    totals = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("emails", "addresses", "email_recipients")
    }
    log.info("=" * 64)
    log.info("STORAGE STATISTICS")
    log.info("  Inserted this run              : %6d", stats.inserted)
    log.info("  Skipped (message_id exists)    : %6d", stats.skipped_existing)
    log.info("  Failed inserts                 : %6d", stats.failed)
    log.info("  Recipient links written        : %6d", stats.recipient_links)
    log.info("  DB totals: emails=%d  addresses=%d  email_recipients=%d",
             totals["emails"], totals["addresses"], totals["email_recipients"])
    log.info("=" * 64)
