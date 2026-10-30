-- amazon.jobs descriptions now come from its search API (details._amazon); retry the
-- Amazon postings that found nothing before.
UPDATE postings SET detail_status = NULL, detail_fetched_at = NULL
    WHERE detail_status = 'empty' AND url LIKE '%amazon.jobs/%';

-- The rules now reject required experience beyond early career (rules.required_years):
-- re-check open postings that have a description to read.
UPDATE postings SET rules_status = 'pending', rules_reason = NULL
    WHERE is_live = 1 AND status = 'new' AND rules_status = 'passed'
    AND length(description) > 200;
