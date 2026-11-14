"""Task 1 - Data extraction.

Discovers every raw email file under an Enron ``maildir`` tree, parses each one into a
structured record (mandatory + optional fields), logs parse failures, and reports
extraction statistics. A malformed file never stops the run: every exception is caught,
recorded with its reason, and the file is skipped.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.policy import compat32
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterator, Optional

from dateutil import parser as date_parser
from dateutil.tz import tzoffset

log = logging.getLogger(__name__)

MANDATORY_FIELDS = (
    "message_id", "date", "from_address", "to_addresses", "subject", "body", "source_file",
)
OPTIONAL_FIELDS = (
    "cc_addresses", "bcc_addresses", "x_from", "x_to", "x_cc", "x_bcc", "x_folder",
    "x_origin", "content_type", "has_attachment", "forwarded_content", "quoted_content",
    "headings",
)
ALL_FIELDS = MANDATORY_FIELDS + OPTIONAL_FIELDS

# Fallback map for timezone abbreviations (used only when the stdlib parser yields no offset).
TZ_ABBREVIATIONS = {
    name: tzoffset(name, hours * 3600)
    for name, hours in {
        "UT": 0, "UTC": 0, "GMT": 0, "Z": 0,
        "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
        "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
        "AKST": -9, "AKDT": -8, "HST": -10,
        "BST": 1, "CET": 1, "CEST": 2,
    }.items()
}

# --- Header unfolding -------------------------------------------------------------------
# The Enron conversion wrapped long headers two ways: "\n\t" inside a word
# ("Phi\n\tllip" -> "Phillip") and "\r\n " at a word boundary (the space is real).
_FOLD_TAB_AFTER_SEPARATOR = re.compile(r"([,;])\r?\n\t")
_FOLD_TAB = re.compile(r"\r?\n\t")
_FOLD_NEWLINE = re.compile(r"\r?\n")

# --- Body section markers ---------------------------------------------------------------
_FORWARD_MARKERS = [
    re.compile(r"^[ \t]*-+[ \t]*Forwarded (?:by|message)\b.*$", re.I | re.M),
    re.compile(r"^[ \t]*Begin forwarded message:.*$", re.I | re.M),
]
_REPLY_MARKERS = [
    re.compile(r"^[ \t]*-+[ \t]*Original Message[ \t]*-+.*$", re.I | re.M),
    re.compile(r"^[ \t]*On [^\n]{1,200} wrote:[ \t]*$", re.M),
    # Outlook reply header without the "Original Message" separator.
    re.compile(r"^[ \t]*From:[ \t][^\n]+\n[ \t]*(?:Sent|Date):[ \t].*$", re.M),
    # Lotus Notes: 'Name <addr> on 05/14/2001 07:36:00 PM' followed by a To: line.
    re.compile(
        r"^[^\n]{1,120} on \d{1,2}/\d{1,2}/\d{2,4} \d{1,2}:\d{2}(?::\d{2})?[ \t]*[AP]M[ \t]*\n"
        r"(?:[^\n]*\n){0,2}?[ \t]*To:", re.M,
    ),
    # Lotus Notes: name line, then date line, then (optional 'Sent by:') To: line.
    re.compile(
        r"^[^\n]{1,80}\n[ \t]*\d{1,2}/\d{1,2}/\d{2,4} \d{1,2}:\d{2}(?::\d{2})?[ \t]*[AP]M[ \t]*\n"
        r"(?:[ \t]*Sent by:[^\n]*\n)?[ \t]*To:", re.M,
    ),
]

# --- Attachment indicators --------------------------------------------------------------
_ATTACHMENT_PATTERNS = [
    re.compile(r"<<[^<>\n]*\.[A-Za-z0-9]{2,5}[ \t]*>>"),  # Outlook placeholder <<file.doc>>
    re.compile(r"<<[ \t]*File:[^<>\n]+>>", re.I),
    re.compile(  # Lotus Notes attachment listing: " - Memo.doc"
        r"^[ \t]*-[ \t]*\S[^\n]{0,100}\.(?:docx?|xlsx?|pptx?|pdf|zip|txt|csv|rtf|wpd|"
        r"jpe?g|gif|tiff?|bmp|png|html?|msg|mpp|vsd)[ \t]*$", re.I | re.M,
    ),
    re.compile(r"^Content-Disposition:[ \t]*attachment", re.I | re.M),
]

# --- Heading heuristics -----------------------------------------------------------------
_HEADER_LIKE = re.compile(
    r"^(?:to|cc|bcc|from|sent|date|subject|re|fw|fwd|importance|sent by)\s*:", re.I,
)
_SEPARATOR = re.compile(r"^[\W_]+$")
_SALUTATION = re.compile(r"^(?:dear|hi|hello|hey|greetings|to whom)\b", re.I)
_UNDERLINE = re.compile(r"^[=\-~*_]{3,}$")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}[ \t]+\S")
_NUMBERED_HEADING = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2})*[.)]|[IVX]{1,4}[.)]|[A-H][.)])[ \t]+[A-Z]")
_LABEL_HEADING = re.compile(r"^[A-Z][A-Za-z0-9&/()' \-]{1,48}:$")
_SENTENCE_END = (".", ",", ";", "?", "!")
# A one-word "Label:" is usually a salutation ("Dave:"), so only accept common section words.
_SECTION_WORDS = {
    "agenda", "summary", "background", "overview", "purpose", "objective", "objectives",
    "attendees", "participants", "notes", "note", "location", "action", "actions", "issues",
    "highlights", "conclusion", "conclusions", "recommendation", "recommendations",
    "discussion", "status", "update", "updates", "contacts", "disclaimer", "disclaimers",
    "questions", "results", "details", "schedule", "timeline", "logistics", "deliverables",
}

_EMAIL_IN_TEXT = re.compile(r"[\w.'+\-]+@[\w\-]+(?:\.[\w\-]+)+")


class ParseError(Exception):
    """Raised when an email cannot be turned into a valid record (reason in ``str(exc)``)."""


@dataclass
class ExtractionStats:
    files_found: int = 0
    parsed: int = 0
    failed: int = 0
    failure_reasons: Counter = field(default_factory=Counter)
    field_present: Counter = field(default_factory=Counter)

    def completeness(self) -> dict[str, float]:
        if not self.parsed:
            return {name: 0.0 for name in ALL_FIELDS}
        return {name: 100.0 * self.field_present[name] / self.parsed for name in ALL_FIELDS}


# =========================================================================================
# Discovery
# =========================================================================================
def discover_files(maildir: str) -> Iterator[str]:
    """Yield every email file under ``maildir`` in a deterministic (sorted) order."""
    for dirpath, dirnames, filenames in os.walk(maildir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith("."):
                yield os.path.join(dirpath, name)


def to_source_file(path: str, maildir: str) -> str:
    """Path relative to the maildir's parent, e.g. ``maildir/lay-k/inbox/45.``."""
    base = os.path.dirname(os.path.abspath(maildir))
    return os.path.relpath(os.path.abspath(path), base).replace(os.sep, "/")


# =========================================================================================
# Low-level decoding helpers
# =========================================================================================
def decode_bytes(data: bytes, declared_charset: Optional[str] = None) -> str:
    """Decode bytes with the declared charset, then utf-8, cp1252, and latin-1 (never fails)."""
    candidates = []
    if declared_charset and declared_charset.lower() not in ("unknown-8bit", "x-unknown"):
        candidates.append(declared_charset)
    candidates += ["utf-8", "cp1252"]
    for charset in candidates:
        try:
            return data.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1")


def _decode_header_value(raw) -> str:
    """Turn a compat32 header (``str`` or ``Header`` for 8-bit values) into clean text,
    decoding RFC 2047 encoded-words such as ``=?ANSI_X3.4-1968?Q?...?=``."""
    try:
        parts = decode_header(raw)
    except Exception:  # malformed encoded-word: fall back to the raw text
        return str(raw)
    out = []
    for chunk, charset in parts:
        out.append(decode_bytes(chunk, charset) if isinstance(chunk, bytes) else chunk)
    return "".join(out)


def unfold(text: str) -> str:
    text = _FOLD_TAB_AFTER_SEPARATOR.sub(r"\1 ", text)
    text = _FOLD_TAB.sub("", text)
    return _FOLD_NEWLINE.sub("", text)


def header_text(msg: Message, name: str) -> Optional[str]:
    """Decoded, unfolded, stripped header value; ``None`` if the header is absent."""
    raw = msg.get(name)
    if raw is None:
        return None
    return unfold(_decode_header_value(raw)).strip()


def _empty_to_none(value: Optional[str]) -> Optional[str]:
    return value if value else None


# =========================================================================================
# Field parsers
# =========================================================================================
def parse_date(value: Optional[str]) -> str:
    """Parse an email Date header and return UTC as ``YYYY-MM-DD HH:MM:SS``."""
    if not value:
        raise ParseError("missing mandatory field: date (no Date header)")
    dt: Optional[datetime]
    try:
        dt = parsedate_to_datetime(value)  # handles offsets and US abbreviations
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            cleaned = value.replace("(", " ").replace(")", " ")
            dt = date_parser.parse(cleaned, tzinfos=TZ_ABBREVIATIONS, fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            raise ParseError(f"invalid mandatory field: date (unparseable: {value!r})") from None
    if dt.tzinfo is None:
        abbrev = re.search(r"\(?([A-Z]{1,5})\)?\s*$", value)
        dt = dt.replace(tzinfo=TZ_ABBREVIATIONS.get(abbrev.group(1)) if abbrev else None)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # no zone information at all: assume UTC
    try:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        raise ParseError(f"invalid mandatory field: date (out of range: {value!r})") from None


def _clean_address(addr: str) -> Optional[str]:
    addr = re.sub(r"\s+", "", addr).strip("<>\"'").lower()
    local, at, domain = addr.rpartition("@")
    local, domain = local.strip("\"'"), domain.strip("\"'>")
    if not at or not local or not domain:
        return None
    return f"{local}@{domain}"


def parse_address_list(value: Optional[str]) -> list[str]:
    """Extract bare, lowercased, de-duplicated addresses from an address header."""
    if not value:
        return []
    try:
        pairs = getaddresses([value], strict=False)
    except TypeError:  # Python builds without the ``strict`` parameter
        pairs = getaddresses([value])
    addresses: list[str] = []
    for _, addr in pairs:
        cleaned = _clean_address(addr) if addr else None
        if cleaned and cleaned not in addresses:
            addresses.append(cleaned)
    if not addresses:  # getaddresses gave up on a malformed list: regex rescue
        for match in _EMAIL_IN_TEXT.findall(value):
            cleaned = _clean_address(match)
            if cleaned and cleaned not in addresses:
                addresses.append(cleaned)
    return addresses


def extract_body(msg: Message) -> str:
    """Decoded text body (quoted-printable/base64 handled) with normalized newlines."""
    if msg.is_multipart():
        texts = [
            _payload_text(part) for part in msg.walk()
            if part.get_content_type() == "text/plain" and not part.get_filename()
        ]
        text = "\n".join(t for t in texts if t)
    else:
        text = _payload_text(msg)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _payload_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    return decode_bytes(payload, part.get_content_charset())


def split_body(text: str) -> tuple[str, Optional[str], Optional[str]]:
    """Split a body into (primary body, forwarded_content, quoted_content).

    The earliest forward/reply marker cuts the body; everything from that marker on goes
    to the matching section (nested forwards/replies stay inside it). Lines starting with
    '>' left in the primary part are moved to quoted_content.
    """
    cut_at, cut_kind = len(text), None
    for kind, patterns in (("forward", _FORWARD_MARKERS), ("reply", _REPLY_MARKERS)):
        for pattern in patterns:
            match = pattern.search(text)
            if match and match.start() < cut_at:
                cut_at, cut_kind = match.start(), kind
    primary, rest = text[:cut_at], text[cut_at:].strip()

    primary_lines, quoted_lines = [], []
    for line in primary.split("\n"):
        (quoted_lines if line.lstrip().startswith(">") else primary_lines).append(line)

    quoted_parts = ["\n".join(quoted_lines).strip()]
    if cut_kind == "reply":
        quoted_parts.append(rest)
    forwarded = rest if cut_kind == "forward" else ""
    quoted = "\n\n".join(p for p in quoted_parts if p)
    return "\n".join(primary_lines).strip(), _empty_to_none(forwarded), _empty_to_none(quoted)


def detect_attachment(content_type: Optional[str], body: str) -> bool:
    if content_type and (content_type.lower().startswith("multipart/") or "boundary=" in content_type.lower()):
        return True
    return any(p.search(body) for p in _ATTACHMENT_PATTERNS)


def _looks_like_data_row(line: str) -> bool:
    """Table rows / timestamps / amounts are not headings."""
    chars = [c for c in line if not c.isspace()]
    digits = sum(c.isdigit() for c in chars)
    return "\t" in line or "$" in line or (bool(chars) and digits / len(chars) > 0.2)


def _is_label_heading(line: str) -> bool:
    if not _LABEL_HEADING.match(line):
        return False
    words = line[:-1].split()
    return len(words) > 1 or line[:-1].lower() in _SECTION_WORDS


def _is_all_caps(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return (
        line[:1].isalpha()
        and len(letters) >= 4
        and all(c.isupper() for c in letters)
        and any(len(w) >= 3 and w.isalpha() for w in re.split(r"[^A-Za-z]+", line))
        and "@" not in line
        and "http" not in line.lower()
    )


def extract_headings(text: str) -> Optional[str]:
    """Heuristic headings: underlined lines, markdown/numbered section titles, ALL-CAPS
    title lines and standalone labels ending in ':'. Returns newline-joined text or None."""
    lines = text.split("\n")
    found: list[str] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if (not 3 <= len(line) <= 80 or _HEADER_LIKE.match(line) or _SEPARATOR.match(line)
                or _SALUTATION.match(line)):
            continue
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        is_heading = False
        if _UNDERLINE.match(next_line) and len(next_line) >= max(3, len(line) // 2):
            is_heading = True
        elif _MARKDOWN_HEADING.match(line):
            line, is_heading = line.lstrip("#").strip(), True
        elif (len(line.split()) <= 8 and not line.endswith(_SENTENCE_END)
              and not _looks_like_data_row(line)):
            is_heading = bool(
                _NUMBERED_HEADING.match(line) or _is_label_heading(line) or _is_all_caps(line)
            )
        if is_heading and line not in found:
            found.append(line)
    return "\n".join(found) if found else None


# =========================================================================================
# Record assembly
# =========================================================================================
def parse_email(raw: bytes, source_file: str) -> dict:
    """Parse raw RFC 2822 bytes into a record dict. Raises ParseError with a reason."""
    msg = message_from_bytes(raw, policy=compat32)
    missing: list[str] = []

    message_id = header_text(msg, "Message-ID")
    if not message_id:
        missing.append("message_id (no Message-ID header)")

    date = None
    try:
        date = parse_date(header_text(msg, "Date"))
    except ParseError as exc:
        missing.append(str(exc).split(": ", 1)[1])

    from_header = header_text(msg, "From")
    from_list = parse_address_list(from_header)
    if not from_list:
        missing.append("from_address (no From header)" if from_header is None
                       else f"from_address (no valid address in From: {from_header!r})")

    to_header = header_text(msg, "To")
    to_addresses = parse_address_list(to_header)
    if not to_addresses:
        missing.append("to_addresses (no To header)" if to_header is None
                       else f"to_addresses (no valid address in To: {to_header[:80]!r})")

    subject = header_text(msg, "Subject")
    if subject is None:
        missing.append("subject (no Subject header)")

    full_body = extract_body(msg)

    if missing:
        raise ParseError("missing mandatory field: " + "; ".join(missing))

    body, forwarded, quoted = split_body(full_body)
    content_type = _empty_to_none(header_text(msg, "Content-Type"))
    return {
        "message_id": message_id,
        "date": date,
        "from_address": from_list[0],
        "to_addresses": to_addresses,
        "subject": subject,
        "body": body,
        "source_file": source_file,
        "cc_addresses": parse_address_list(header_text(msg, "Cc")),
        "bcc_addresses": parse_address_list(header_text(msg, "Bcc")),
        "x_from": _empty_to_none(header_text(msg, "X-From")),
        "x_to": _empty_to_none(header_text(msg, "X-To")),
        "x_cc": _empty_to_none(header_text(msg, "X-cc")),
        "x_bcc": _empty_to_none(header_text(msg, "X-bcc")),
        "x_folder": _empty_to_none(header_text(msg, "X-Folder")),
        "x_origin": _empty_to_none(header_text(msg, "X-Origin")),
        "content_type": content_type,
        "has_attachment": detect_attachment(content_type, full_body),
        "forwarded_content": forwarded,
        "quoted_content": quoted,
        "headings": extract_headings(full_body),
    }


def _is_present(value) -> bool:
    if isinstance(value, bool):
        return value  # has_attachment: counted when True
    return bool(value)


# =========================================================================================
# Runner
# =========================================================================================
def run_extraction(maildir: str, error_log_path: str) -> tuple[list[dict], ExtractionStats]:
    """Parse every file under ``maildir``. Returns (records, stats); writes the error log."""
    stats = ExtractionStats()
    records: list[dict] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(error_log_path, "w", encoding="utf-8") as error_log:
        error_log.write("# timestamp\tsource_file\treason\n")
        for path in discover_files(maildir):
            stats.files_found += 1
            source_file = to_source_file(path, maildir)
            try:
                with open(path, "rb") as fh:
                    record = parse_email(fh.read(), source_file)
            except ParseError as exc:
                reason = str(exc)
            except Exception as exc:  # never crash on malformed input
                reason = f"unexpected error: {type(exc).__name__}: {exc}"
            else:
                records.append(record)
                stats.parsed += 1
                for name in ALL_FIELDS:
                    if _is_present(record[name]):
                        stats.field_present[name] += 1
                continue
            stats.failed += 1
            stats.failure_reasons[_reason_category(reason)] += 1
            error_log.write(f"{now}\t{source_file}\t{reason}\n")

    log_stats(stats)
    return records, stats


def _reason_category(reason: str) -> str:
    """Collapse a detailed reason into a category for the summary (drop quoted values)."""
    return re.sub(r" \((?:no valid address in|unparseable|out of range)[^)]*\)", "", reason)


def log_stats(stats: ExtractionStats) -> None:
    log.info("=" * 64)
    log.info("EXTRACTION STATISTICS")
    log.info("  Total files found : %6d", stats.files_found)
    log.info("  Parsed OK         : %6d", stats.parsed)
    log.info("  Failed            : %6d", stats.failed)
    for reason, count in stats.failure_reasons.most_common():
        log.info("      %6d  %s", count, reason)
    log.info("  Field completeness (share of parsed records with a non-empty value):")
    for name, pct in stats.completeness().items():
        kind = "mandatory" if name in MANDATORY_FIELDS else "optional "
        note = "  (share = True)" if name == "has_attachment" else ""
        log.info("      %-18s %s %6.2f%%%s", name, kind, pct, note)
    log.info("=" * 64)
