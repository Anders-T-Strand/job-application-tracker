-- Zapply feed (app/discovery/zapply.py): companies gain a 'zapply' source type, and
-- postings carry a source's visa-sponsorship estimate. SQLite can't relax a CHECK in
-- place, so companies is rebuilt — with foreign keys off, or dropping it would cascade
-- and delete every posting.
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE companies_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    ats_type      TEXT NOT NULL CHECK (ats_type IN ('greenhouse','lever','ashby','zapply')),
    board_token   TEXT NOT NULL,
    lca_filings   INTEGER NOT NULL DEFAULT 0,             -- DOL LCA certified count (soft rank signal)
    lca_year      INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    last_polled   TEXT,
    etag          TEXT                                    -- for conditional requests
);
INSERT INTO companies_new
    (id, name, ats_type, board_token, lca_filings, lca_year, active, last_polled, etag)
    SELECT id, name, ats_type, board_token, lca_filings, lca_year, active, last_polled, etag
    FROM companies;
DROP TABLE companies;
ALTER TABLE companies_new RENAME TO companies;
ALTER TABLE postings ADD COLUMN visa_sponsor INTEGER NOT NULL DEFAULT 0;  -- 1 = source says likely
COMMIT;
PRAGMA foreign_keys = ON;
