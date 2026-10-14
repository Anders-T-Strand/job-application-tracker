# Job Discovery Module — Claude Code Handoff Spec

Drop this file in the repo root of the existing job-application-tracker project
and point Claude Code at it ("read DISCOVERY_SPEC.md and implement the discovery
module"). It contains all context and decisions from the design conversation.

## Context

The owner has an existing personal job application tracker:
- Email-first pipeline using the Gmail API
- Claude Haiku for classification and fuzzy matching
- htmx dashboard frontend
- Deployed on an Oracle Cloud instance, secured via Tailscale

This spec adds a **job discovery module** as the front end of that tracker:
it finds relevant postings, filters and scores them, and hands them off to
the existing application-tracking flow. **It shares the tracker's database** —
do not build it as a separate system.

## What the user is looking for

1. US-based roles (or US-remote)
2. Early career or internship level
3. AI/ML or data science

The candidate will need visa sponsorship, so postings that explicitly exclude
candidates needing sponsorship must be filtered out, and companies with a
history of H-1B sponsorship should be surfaced preferentially.

## Key design decisions (already made — do not relitigate)

1. **No scraping of job boards.** LinkedIn/Indeed block it. Instead, poll
   public ATS board APIs from a curated company list:
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
   - Lever: `https://api.lever.co/v0/postings/{token}?mode=json`
   - Ashby: `https://api.ashbyhq.com/posting-api/job-board/{token}`
   Seed the company list from the SimplifyJobs GitHub internship/new-grad repos.

2. **Sponsorship handled two ways:**
   - *Negative signal (posting-level, hard reject):* regex for explicit
     "no sponsorship" / "must be US citizen" / "security clearance" language.
   - *Positive signal (company-level, soft rank):* `lca_filings` count loaded
     from the DOL LCA disclosure files (public quarterly CSVs). Sort/filter by
     it in the UI; never hard-reject on zero filings.

3. **Two-stage filtering to keep LLM costs near zero:**
   - Stage 1: cheap rules filter (regexes below) kills ~90% of postings.
   - Stage 2: Claude Haiku scores survivors 0-100 for relevance against the
     user's resume, with a one-line rationale. Batch the calls.

4. **Lifecycle handoff:** posting `status` goes `new → saved/dismissed → applied`;
   `applied` sets `application_id` as an FK into the existing applications table.

5. Nightly cron poll per company. Track `last_seen` per posting and flip
   `is_live = 0` when a posting disappears from a poll. Dedupe on
   `sha256(company_id|title|location)`.

6. Log `rules_reason` on every rejection so the filter can be tuned. The
   stage regex will need iteration — some companies title internships as
   "Student Researcher" or "Fellowship".

## Schema

```sql
-- SQLite assumed; adjust for the tracker's actual DB if different.

CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    ats_type      TEXT NOT NULL CHECK (ats_type IN ('greenhouse','lever','ashby')),
    board_token   TEXT NOT NULL,
    lca_filings   INTEGER DEFAULT 0,
    lca_year      INTEGER,
    active        INTEGER DEFAULT 1,
    last_polled   TEXT,
    etag          TEXT
);

CREATE TABLE IF NOT EXISTS postings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    external_id   TEXT,
    title         TEXT NOT NULL,
    location      TEXT,
    remote        INTEGER DEFAULT 0,
    url           TEXT NOT NULL,
    description   TEXT,
    content_hash  TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    is_live       INTEGER DEFAULT 1,
    rules_status  TEXT DEFAULT 'pending' CHECK (rules_status IN ('pending','passed','rejected')),
    rules_reason  TEXT,
    llm_score     INTEGER,
    llm_rationale TEXT,
    status        TEXT DEFAULT 'new' CHECK (status IN ('new','saved','dismissed','applied')),
    application_id INTEGER,
    UNIQUE (company_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_postings_status ON postings (status, llm_score DESC);
CREATE INDEX IF NOT EXISTS idx_postings_live   ON postings (is_live, rules_status);
```

## Rules filter (implement as-is, then tune)

```python
import re

STAGE_INCLUDE = re.compile(
    r"\b(intern(ship)?|co[- ]?op|new grad|university|early career|entry[- ]level|graduate)\b",
    re.I,
)
STAGE_EXCLUDE = re.compile(
    r"\b(senior|staff|principal|lead|manager|director|head of|sr\.?)\b", re.I
)
FIELD = re.compile(
    r"\b(machine learning|\bml\b|\bai\b|artificial intelligence|data scien|deep learning|"
    r"nlp|computer vision|llm|generative|mlops|data engineer|applied scien)\b",
    re.I,
)
US_HINT = re.compile(
    r"\b(united states|usa|u\.s\.|remote[- ]?us|us[- ]?remote|"
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|"
    r"NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b"
)
NO_SPONSOR = re.compile(
    r"(without (the need for |requiring )?(visa )?sponsorship"
    r"|not (be able to |currently )?(provide|offer|sponsor)[^.]{0,40}(visa|sponsorship|work authorization)"
    r"|unable to sponsor"
    r"|no (visa )?sponsorship"
    r"|must be (a )?(us|u\.s\.) citizen"
    r"|citizenship (is )?required"
    r"|security clearance)",
    re.I,
)

def apply_rules(title: str, location: str, description: str) -> tuple[bool, str]:
    """Returns (passed, reason). reason is '' on pass, else the rule that fired."""
    if not STAGE_INCLUDE.search(title):
        return False, "stage:no-early-career-signal"
    if STAGE_EXCLUDE.search(title):
        return False, "stage:senior-term-in-title"
    if not FIELD.search(f"{title} {description}"):
        return False, "field:no-ai-ml-ds-terms"
    loc_blob = f"{location} {description[:500]}"
    if not US_HINT.search(loc_blob):
        return False, "geo:no-us-signal"
    if NO_SPONSOR.search(description):
        return False, "sponsor:explicit-negation"
    return True, ""
```

## Build order

1. Migration adding the two tables to the tracker DB
2. Poller: fetch + normalize (strip HTML from descriptions) for all three ATS types
3. Wire in the rules filter; log rejections
4. Haiku scoring stage (batched; reuse the tracker's existing Anthropic client)
5. Seed script: parse SimplifyJobs repo → companies table; DOL LCA loader → lca_filings
6. htmx views: score-sorted new postings, save/dismiss actions, applied handoff
7. Cron entry (nightly), respecting per-host rate limits and conditional requests

## Not yet decided / ask the user

- Whether the tracker DB is actually SQLite (adjust schema accordingly)
- Exact resume text to embed in the Haiku scoring prompt
- Initial company list size and any must-include companies
