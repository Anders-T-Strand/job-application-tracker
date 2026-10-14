CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    role_title TEXT,
    source TEXT NOT NULL DEFAULT 'email',          -- email | manual
    ats_domain TEXT,
    applied_at TEXT NOT NULL,                      -- ISO 8601 UTC
    status TEXT NOT NULL DEFAULT 'applied',        -- applied|oa|screen|interview|offer|rejected|ghosted
    last_activity_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    internet_message_id TEXT NOT NULL UNIQUE,      -- stable dedupe key (Graph ids change on move)
    graph_id TEXT NOT NULL,
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    received_at TEXT NOT NULL,
    from_addr TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    classification TEXT,                           -- JSON from the classifier, NULL until classified
    confidence REAL,
    processed INTEGER NOT NULL DEFAULT 0,          -- 0 = awaiting classification
    needs_review INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    triggered_by TEXT NOT NULL                     -- 'email:<id>' or 'manual'
);

CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX idx_emails_application ON emails(application_id);
CREATE INDEX idx_emails_pending ON emails(processed) WHERE processed = 0;
CREATE INDEX idx_history_application ON status_history(application_id);
