-- =====================================================================================
-- Sample queries for enron.db
-- Run:  sqlite3 -header -column enron.db < sample_queries.sql
--
-- The "Expected output" notes are real results from a full run (python main.py --fresh)
-- over the 5 selected mailboxes: 22,596 emails, 12,082 addresses, 192,006 recipient links.
-- =====================================================================================


-- Q1. Count emails per sender (top 10 senders)
-- Expected output: 10 rows (from_address, emails_sent), highest count first. The mailbox
-- owners and their assistants lead:
--   david.delainey@enron.com 2949 | john.lavorato@enron.com 2400 | louise.kitchen@enron.com 1197
--   rosalee.fleming@enron.com 883 (Ken Lay's assistant) | sherri.sera@enron.com 676
--   (Jeff Skilling's assistant) | ... | markskilling@hotmail.com 154
SELECT from_address, COUNT(*) AS emails_sent
FROM emails
GROUP BY from_address
ORDER BY emails_sent DESC, from_address
LIMIT 10;


-- Q2. Find all emails in a date range: Enron's collapse, Oct-Dec 2001 (UTC), by month
-- Expected output: 3 rows (month, emails): 2001-10 1588 | 2001-11 1328 | 2001-12 720
-- (3,636 emails in total for the range)
SELECT strftime('%Y-%m', date) AS month, COUNT(*) AS emails
FROM emails
WHERE date >= '2001-10-01' AND date < '2002-01-01'
GROUP BY month
ORDER BY month;


-- Q2b. The first 5 emails of that range, in date order
-- Expected output: 5 rows (date, from_address, subject), all from louise.kitchen@enron.com
-- on 2001-10-01, starting with "Board presentation" at 03:08:35. Rows 4 and 5 are the same
-- "RE: Still got some bits to fill in" at the same second: one email stored in two folders.
-- Task 3 flags this kind of duplicate.
SELECT date, from_address, subject
FROM emails
WHERE date >= '2001-10-01' AND date < '2002-01-01'
ORDER BY date
LIMIT 5;


-- Q3. Find emails with CC recipients: total count, then the 5 widest CC lists
-- Expected output (first query): 1 row, emails_with_cc = 6089
SELECT COUNT(DISTINCT email_id) AS emails_with_cc
FROM email_recipients
WHERE recipient_type = 'cc';

-- Expected output (second query): 5 rows (date, from_address, subject, cc_count). The widest
-- is "Program Date Change" from jan_levine@travisintl.com with 171 CC recipients, followed by
-- three "Analyst & Associate Program - Summer Interns" emails (102 each) and "Thank you for the
-- memories" (95).
SELECT e.date, e.from_address, e.subject, COUNT(*) AS cc_count
FROM emails e
JOIN email_recipients r ON r.email_id = e.id AND r.recipient_type = 'cc'
GROUP BY e.id
ORDER BY cc_count DESC, e.date
LIMIT 5;


-- Q4. Rebuild the To / CC / BCC lists of one email from the normalized tables
-- Expected output: 3 rows (recipient_type, addresses), matching the raw file's headers:
--   bcc  bingham_alpheus@lilly.com, klay@enron.com, taurel_sidney@lilly.com
--   cc   bingham_alpheus@lilly.com, klay@enron.com, taurel_sidney@lilly.com
--   to   gwhalle@enron.com, lkitchen@enron.com
-- (In this corpus, Bcc: is generally a copy of Cc:. That is a property of the source data.)
SELECT r.recipient_type, group_concat(a.email, ', ') AS addresses
FROM (SELECT * FROM email_recipients ORDER BY recipient_type, position) r
JOIN addresses a ON a.id = r.address_id
WHERE r.email_id = (SELECT id FROM emails WHERE source_file = 'maildir/lay-k/all_documents/210.')
GROUP BY r.recipient_type;


-- Q5. Most frequently e-mailed addresses (any of to/cc/bcc), using the address table
-- Expected output: 5 rows (email, emails_received). The mailbox owners lead:
--   louise.kitchen@enron.com 4686 | john.lavorato@enron.com 3207 | kenneth.lay@enron.com 2719
--   jeff.skilling@enron.com 2379 | klay@enron.com 1960
SELECT a.email, COUNT(DISTINCT r.email_id) AS emails_received
FROM email_recipients r
JOIN addresses a ON a.id = r.address_id
GROUP BY a.id
ORDER BY emails_received DESC
LIMIT 5;


-- Q6. Duplicate detection summary (after Task 3)
-- Expected output: 1 row: duplicate_groups 4121 | flagged_duplicates 9215 | avg_group_size 3.24
SELECT COUNT(DISTINCT duplicate_of) AS duplicate_groups,
       COUNT(*) AS flagged_duplicates,
       ROUND(1.0 * (COUNT(*) + COUNT(DISTINCT duplicate_of)) / COUNT(DISTINCT duplicate_of), 2) AS avg_group_size
FROM emails
WHERE is_duplicate = 1;


-- Q7. Largest duplicate groups: original, group size, latest duplicate, lowest similarity
-- Expected output: 5 rows. Largest is "Approval for reviewer" from david.delainey@enron.com
-- (68 emails, min similarity 91.19, a templated approval notice forwarded many times), then
-- "New Kudlow Commentary" (29, 99.41), two "Your Approval is Overdue: Access Request ..."
-- groups (17 and 13, 99.61) and "Staff Meeting" from kay.chapman@enron.com (16, 90.08).
SELECT o.date AS original_date, o.from_address, o.subject,
       COUNT(*) + 1 AS group_size,
       MAX(d.date) AS latest_duplicate_date,
       MIN(d.similarity_score) AS min_similarity
FROM emails d
JOIN emails o ON o.message_id = d.duplicate_of
GROUP BY o.id
ORDER BY group_size DESC, o.date
LIMIT 5;


-- Q8. Confirm the date-range query uses the index
-- Expected output: one plan line: SEARCH emails USING INDEX idx_emails_date (date>? AND date<?)
EXPLAIN QUERY PLAN
SELECT date, from_address, subject FROM emails
WHERE date >= '2001-10-01' AND date < '2002-01-01';
