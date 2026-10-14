-- Several feeds (app/discovery/feeds.py): the 'zapply' company type becomes the generic
-- 'feed' (feed-only employers, never polled), and postings record their feed plus any
-- explicit term tags ("Winter 2027, Spring 2027", "Internship", "New Grad"). companies is
-- rebuilt again to change the CHECK — foreign keys off, or the drop cascades to postings.
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE companies_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    ats_type      TEXT NOT NULL CHECK (ats_type IN ('greenhouse','lever','ashby','feed')),
    board_token   TEXT NOT NULL,
    lca_filings   INTEGER NOT NULL DEFAULT 0,             -- DOL LCA certified count (soft rank signal)
    lca_year      INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    last_polled   TEXT,
    etag          TEXT                                    -- for conditional requests
);
INSERT INTO companies_new
    (id, name, ats_type, board_token, lca_filings, lca_year, active, last_polled, etag)
    SELECT id, name, CASE ats_type WHEN 'zapply' THEN 'feed' ELSE ats_type END,
           board_token, lca_filings, lca_year, active, last_polled, etag
    FROM companies;
DROP TABLE companies;
ALTER TABLE companies_new RENAME TO companies;

ALTER TABLE postings ADD COLUMN feed TEXT;   -- NULL = an ATS board
ALTER TABLE postings ADD COLUMN terms TEXT;  -- what the feed says about the hiring cycle
UPDATE postings SET feed = 'zapply-intern', terms = 'Internship'
    WHERE company_id IN (SELECT id FROM companies WHERE ats_type = 'feed');
CREATE INDEX idx_postings_feed ON postings (feed, is_live) WHERE feed IS NOT NULL;
DELETE FROM sync_state WHERE key = 'zapply_etag';

-- Rules and scoring now follow the May 2027 timeline (winter/spring internships, new-grad
-- roles; summer internships score low): re-filter and re-score the open postings.
UPDATE postings SET rules_status = 'pending', rules_reason = NULL,
                    llm_score = NULL, llm_rationale = NULL
    WHERE is_live = 1 AND status = 'new';
COMMIT;
PRAGMA foreign_keys = ON;
