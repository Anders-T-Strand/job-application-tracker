-- The US check now also recognizes spelled-out states, major US cities ("NYC", "Sunnyvale",
-- "Dallas, Texas") and a bare "US" in the location: re-filter its rejections.
UPDATE postings SET rules_status = 'pending', rules_reason = NULL
    WHERE is_live = 1 AND status = 'new' AND rules_reason = 'geo:no-us-signal';
