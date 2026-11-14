-- =====================================================================================
-- Enron email database schema (SQLite 3)
--
--   emails            one row per successfully parsed email (mandatory + optional fields,
--                     duplicate flags, notification status)
--   addresses         every distinct email address, stored once
--   email_recipients  junction table: which address received which email, as to/cc/bcc
--
-- Dates are stored as UTC text 'YYYY-MM-DD HH:MM:SS'. This sorts chronologically and works
-- with SQLite's date functions. Booleans are 0/1 integers enforced by CHECK constraints.
-- =====================================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS emails (
    id                 INTEGER PRIMARY KEY,
    -- mandatory fields ------------------------------------------------------------------
    message_id         TEXT     NOT NULL UNIQUE,      -- prevents exact duplicates on insert
    date               DATETIME NOT NULL,             -- normalized to UTC
    from_address       TEXT     NOT NULL,             -- bare address, lowercased
    subject            TEXT     NOT NULL,             -- may be '' (header present but empty)
    body               TEXT     NOT NULL,             -- primary body (forwarded/quoted removed)
    source_file        TEXT     NOT NULL UNIQUE,      -- e.g. maildir/lay-k/inbox/45.
    -- optional fields (NULL when absent) --------------------------------------------------
    x_from             TEXT,
    x_to               TEXT,
    x_cc               TEXT,
    x_bcc              TEXT,
    x_folder           TEXT,
    x_origin           TEXT,
    content_type       TEXT,
    has_attachment     BOOLEAN  NOT NULL DEFAULT 0 CHECK (has_attachment IN (0, 1)),
    forwarded_content  TEXT,
    quoted_content     TEXT,
    headings           TEXT,                          -- newline-separated
    -- duplicate detection (Task 3) ----------------------------------------------------------
    is_duplicate       BOOLEAN  NOT NULL DEFAULT 0 CHECK (is_duplicate IN (0, 1)),
    duplicate_of       TEXT     REFERENCES emails (message_id),   -- original's message_id
    similarity_score   REAL     CHECK (similarity_score BETWEEN 0 AND 100),
    -- notification (Task 4) -------------------------------------------------------------------
    notification_sent  BOOLEAN  NOT NULL DEFAULT 0 CHECK (notification_sent IN (0, 1)),
    notification_date  DATETIME,
    -- a row is either an original (no reference) or a flagged duplicate (with reference)
    CHECK ((is_duplicate = 0 AND duplicate_of IS NULL) OR (is_duplicate = 1 AND duplicate_of IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS addresses (
    id     INTEGER PRIMARY KEY,
    email  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS email_recipients (
    email_id        INTEGER NOT NULL REFERENCES emails (id) ON DELETE CASCADE,
    address_id      INTEGER NOT NULL REFERENCES addresses (id),
    recipient_type  TEXT    NOT NULL CHECK (recipient_type IN ('to', 'cc', 'bcc')),
    position        INTEGER NOT NULL,                 -- order within the original header
    PRIMARY KEY (email_id, recipient_type, address_id)
);

-- Indexes required by the spec ---------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_emails_date         ON emails (date);
CREATE INDEX IF NOT EXISTS idx_emails_from_address ON emails (from_address);
CREATE INDEX IF NOT EXISTS idx_emails_subject      ON emails (subject);

-- Supporting indexes ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_recipients_address  ON email_recipients (address_id, recipient_type);
CREATE INDEX IF NOT EXISTS idx_emails_duplicate_of ON emails (duplicate_of);
