-- The no-sponsorship rule now recognizes far more wordings (CPT/OPT excluded, permanent
-- authorization or U.S.-person status required, ...): re-filter open postings. Long
-- descriptions are also re-scored, since the scorer's excerpt now carries their
-- work-authorization notes.
UPDATE postings SET rules_status = 'pending', rules_reason = NULL
    WHERE is_live = 1 AND status = 'new' AND rules_status = 'passed';
UPDATE postings SET llm_score = NULL, llm_rationale = NULL
    WHERE is_live = 1 AND status = 'new' AND rules_status = 'pending'
      AND length(description) > 2400;
