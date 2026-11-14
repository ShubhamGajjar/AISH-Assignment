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

log = logging.getLogger("pipeline")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enron email extraction, storage, duplicate detection and notification pipeline.")
    parser.add_argument("--maildir", default="data/maildir",
                        help="root of the Enron maildir tree (default: data/maildir)")
    parser.add_argument("--error-log", default="error_log.txt",
                        help="parse-failure log (default: error_log.txt)")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
