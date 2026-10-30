-- Feed postings get their description fetched from the employer's job system
-- (app/discovery/details.py): ok | closed | empty | failed, and when it was tried.
ALTER TABLE postings ADD COLUMN detail_status TEXT;
ALTER TABLE postings ADD COLUMN detail_fetched_at TEXT;

-- Postdoc titles are now excluded (rules.STAGE_EXCLUDE); re-check the ones that passed.
UPDATE postings SET rules_status = 'pending', rules_reason = NULL
    WHERE is_live = 1 AND status = 'new' AND rules_status = 'passed'
    AND lower(title) LIKE '%postdoc%';
