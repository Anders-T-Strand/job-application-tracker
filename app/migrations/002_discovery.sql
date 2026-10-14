-- Job discovery module: curated ATS boards polled nightly, postings filtered
-- (rules, then Haiku) and handed off to applications on "applied".

CREATE TABLE companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    ats_type      TEXT NOT NULL CHECK (ats_type IN ('greenhouse','lever','ashby')),
    board_token   TEXT NOT NULL,
    lca_filings   INTEGER NOT NULL DEFAULT 0,             -- DOL LCA certified count (soft rank signal)
    lca_year      INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    last_polled   TEXT,
    etag          TEXT                                    -- for conditional requests
);

CREATE TABLE postings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_id   TEXT,
    title         TEXT NOT NULL,
    location      TEXT,
    remote        INTEGER NOT NULL DEFAULT 0,
    url           TEXT NOT NULL,
    description   TEXT,
    content_hash  TEXT NOT NULL,                          -- sha256(company_id|title|location)
    first_seen    TEXT NOT NULL,                          -- ISO 8601 UTC
    last_seen     TEXT NOT NULL,
    is_live       INTEGER NOT NULL DEFAULT 1,             -- 0 once absent from a poll
    rules_status  TEXT NOT NULL DEFAULT 'pending' CHECK (rules_status IN ('pending','passed','rejected')),
    rules_reason  TEXT,                                   -- which rule fired, for filter tuning
    llm_score     INTEGER,                                -- 0-100, NULL until scored
    llm_rationale TEXT,
    status        TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','saved','dismissed','applied')),
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    UNIQUE (company_id, content_hash)
);

CREATE INDEX idx_postings_status ON postings (status, llm_score DESC);
CREATE INDEX idx_postings_live   ON postings (is_live, rules_status);
