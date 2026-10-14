-- Lifecycle: applications rejected for 60+ days are hidden from the board and table but
-- kept for stats. NULL = visible; new activity (a matched email or manual status change)
-- clears it.
ALTER TABLE applications ADD COLUMN hidden_at TEXT;
