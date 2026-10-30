-- Bank-style internships ("Summer Associate/Analyst"), apprenticeships and student jobs
-- listed in new-grad feeds were tagged "New Grad" (see terms.INTERN); retag them.
UPDATE postings SET terms = 'Internship'
    WHERE terms = 'New Grad' AND (
        lower(title) LIKE '%summer associate%' OR lower(title) LIKE '%summer analyst%'
        OR lower(title) LIKE '%apprentice%' OR lower(title) LIKE '%for students%'
        OR lower(title) LIKE '%student employee%' OR lower(title) LIKE '%student assistant%'
        OR lower(title) LIKE '%student worker%' OR lower(title) LIKE '%student researcher%');

-- The rules gained a graduation-window check and the scorer stopped giving title-only
-- postings the benefit of the doubt: re-filter and re-score the open postings.
UPDATE postings SET rules_status = 'pending', rules_reason = NULL,
                    llm_score = NULL, llm_rationale = NULL
    WHERE is_live = 1 AND status = 'new';
