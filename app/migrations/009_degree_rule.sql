-- The rules now reject PhD-/MBA-only roles (rules.degree_required, with DEGREES set), and
-- the candidate profile behind scoring was corrected: re-filter and re-score open postings.
UPDATE postings SET rules_status = 'pending', rules_reason = NULL,
                    llm_score = NULL, llm_rationale = NULL
    WHERE is_live = 1 AND status = 'new' AND rules_status = 'passed';
