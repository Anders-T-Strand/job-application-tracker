-- The prefilter now lets real application mail from job-alert domains through ("Next Steps
-- for Your Application" from hiring@jobalerts.<employer>.com). Re-walk the backfill window
-- so mail it dropped before gets picked up. Mail already stored is skipped (deduped on
-- message id).
DELETE FROM sync_state WHERE key IN ('backfill_done', 'backfill_link');
