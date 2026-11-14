"""Single entry point for the Enron email pipeline.

    python main.py            # full pipeline, dry-run (no email is sent)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from pipeline.extract import run_extraction
from pipeline import storage
from pipeline.dedup import DEFAULT_THRESHOLD, run_dedup

log = logging.getLogger("pipeline")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enron email extraction, storage, duplicate detection and notification pipeline.")
    parser.add_argument("--maildir", default="data/maildir",
                        help="root of the Enron maildir tree (default: data/maildir)")
    parser.add_argument("--error-log", default="error_log.txt",
                        help="parse-failure log (default: error_log.txt)")
    parser.add_argument("--db", default="enron.db",
                        help="SQLite database file (default: enron.db)")
    parser.add_argument("--fresh", action="store_true",
                        help="delete and rebuild the database instead of loading incrementally")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="minimum body similarity (0-100) for duplicates (default: 90)")
    parser.add_argument("--report", default="duplicates_report.csv",
                        help="duplicate summary report (default: duplicates_report.csv)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    if not os.path.isdir(args.maildir):
        log.error("maildir not found: %s (see README for dataset download)", args.maildir)
        return 1

    started = time.perf_counter()
    log.info("[1/4] Extracting emails from %s", args.maildir)
    records, stats = run_extraction(args.maildir, args.error_log)
    log.info("Extraction finished in %.1fs; %d failures written to %s",
             time.perf_counter() - started, stats.failed, args.error_log)

    step = time.perf_counter()
    if args.fresh:
        log.info("--fresh: removing existing database %s", args.db)
        storage.reset_database(args.db)
    log.info("[2/4] Storing %d records in %s", len(records), args.db)
    conn = storage.connect(args.db)
    try:
        storage.init_db(conn)
        storage.store_records(conn, records)
        log.info("Storage finished in %.1fs", time.perf_counter() - step)
        del records  # free memory before the dedup scan

        step = time.perf_counter()
        log.info("[3/4] Detecting duplicates (threshold %.0f%%)", args.threshold)
        run_dedup(conn, args.report, args.threshold)
        log.info("Duplicate detection finished in %.1fs; report written to %s",
                 time.perf_counter() - step, args.report)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
