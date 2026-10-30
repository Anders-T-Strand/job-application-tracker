"""Stage-2 scoring: the LLM rates rules-passed postings 0-100 against the user's profile."""

import json
import logging
import re
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from app.discovery import terms
from app.llm import LLM, LLMError

log = logging.getLogger(__name__)

BATCH_SIZE = 8  # postings per LLM call
MAX_PER_RUN = 500  # per discovery cycle; the rest is picked up next cycle
MAX_DESC_CHARS = 2400
# Requirements usually come after a long intro; a plain prefix would cut them off.
REQUIREMENTS = re.compile(
    r"^[^\n]{0,40}\b(qualifications|requirements|what you(['’]ll)? need|you (have|bring)"
    r"|who you are)\b[^\n]{0,30}$",
    re.I | re.M,
)
# ...and sponsorship / work-authorization notes usually come last, past both.
AUTHORIZATION = re.compile(
    r"\b(visas?|sponsorship|(?-i:OPT|CPT)|work authori[sz]ation|authori[sz]ed to work)\b", re.I
)
SENTENCE_END = re.compile(r"(?<=[a-z]{2}[.!?])\s+|\n")  # not after "U.S."
MAX_NOTE_CHARS = 400

# Used when no resume file exists at settings.resume_path (data/resume.txt).
DEFAULT_PROFILE = """\
Early-career candidate looking for internships and new-grad roles in AI/ML and data
science — machine learning engineering, data science, applied science, MLOps, or
ML-heavy data engineering.
"""

# Timing, location and visa needs a résumé doesn't state, from PROFILE_PATH
# (data/profile.md, private like the résumé; see profile.example.md). Always sent with it.
DEFAULT_CONSTRAINTS = "- None given: judge timing and location from the résumé alone.\n"

SYSTEM_PROMPT = """\
You score job postings for relevance to one candidate's profile.

Respond with ONLY a JSON array — no prose, no code fences — one object per posting:
[{"id": <posting id>, "score": <0-100>, "rationale": "<one line>"}]

Scoring guidance:
- 80-100: early-career or intern AI/ML/data-science role that clearly fits, timing and
  location included
- 50-79: plausible fit with caveats (adjacent field, unclear level or location)
- 0-49: poor fit (wrong field, senior, non-US, sponsorship clearly excluded)
"Summer Analyst" and "Summer Associate" titles are internships, not full-time roles.
Many postings have no description: then judge only what the title, company and location
show, don't assume the candidate meets unstated graduation-date or degree requirements,
and stay at 80 or below unless the fit is unmistakable.
Name the decisive factor in the rationale.
"""


class PostingScore(BaseModel):
    id: int
    score: int = Field(ge=0, le=100)
    rationale: str = ""


SCORES_SCHEMA = {"type": "array", "items": PostingScore.model_json_schema()}


def load_constraints(profile_path: Path) -> str:
    if profile_path.exists():
        return profile_path.read_text()[:3000]
    log.info("no profile at %s; scoring without timing/location/visa constraints", profile_path)
    return DEFAULT_CONSTRAINTS


def load_profile(resume_path: Path) -> str:
    if resume_path.exists():
        return resume_path.read_text()[:6000]
    log.info("no resume at %s; scoring against the default profile", resume_path)
    return DEFAULT_PROFILE


def parse_scores(raw: str, expected_ids: set[int]) -> list[PostingScore]:
    """Extract the JSON array from model output; drop invalid or unrequested entries."""
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        log.warning("unparseable scorer output: %.200s", raw)
        return []
    try:
        items = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        log.warning("unparseable scorer output: %.200s", raw)
        return []
    scores = []
    for item in items if isinstance(items, list) else []:
        try:
            score = PostingScore.model_validate(item)
        except ValidationError:
            continue
        if score.id in expected_ids:
            scores.append(score)
    return scores


def authorization_notes(description: str, skip: str = "") -> str:
    """The description's sponsorship / work-authorization sentences not already in `skip`."""
    notes = []
    for sentence in SENTENCE_END.split(description):
        sentence = sentence.strip()
        if AUTHORIZATION.search(sentence) and sentence not in skip and sentence not in notes:
            notes.append(sentence)
    return " ".join(notes)[:MAX_NOTE_CHARS]


def excerpt(description: str, limit: int = MAX_DESC_CHARS) -> str:
    """The start of the description, its requirements section and its work-authorization
    notes, within `limit`."""
    if len(description) <= limit:
        return description
    notes = authorization_notes(description, skip=_head_and_requirements(description, limit))
    if not notes:
        return _head_and_requirements(description, limit)
    body = _head_and_requirements(description, limit - len(notes) - len("\n[...]\n"))
    return f"{body}\n[...]\n{notes}"


def _head_and_requirements(description: str, limit: int) -> str:
    head = limit // 3
    match = REQUIREMENTS.search(description, head)
    if not match:
        return description[:limit]
    return (
        f"{description[:head]}\n[...]\n{description[match.start() : match.start() + limit - head]}"
    )


def _batch_prompt(profile: str, rows: list[sqlite3.Row], constraints: str) -> str:
    parts = [f"Candidate profile:\n{profile}\n\nConstraints:\n{constraints}\nPostings:"]
    for row in rows:
        description = excerpt(row["description"] or "")
        visa = "Visa: employer likely sponsors (feed estimate)\n" if row["visa_sponsor"] else ""
        terms = f"Term: {row['terms']}\n" if row["terms"] else ""
        parts.append(
            f"--- posting id={row['id']} ---\n"
            f"Company: {row['company_name']}\n"
            f"Title: {row['title']}\n"
            f"Location: {row['location'] or 'unknown'}\n"
            f"{terms}{visa}"
            f"{description or '(no description; judge from title, company and location)'}"
        )
    return "\n".join(parts)


async def score_batch(
    llm: LLM, profile: str, rows: list[sqlite3.Row], constraints: str = DEFAULT_CONSTRAINTS
) -> list[PostingScore]:
    raw = await llm.complete(
        SYSTEM_PROMPT,
        _batch_prompt(profile, rows, constraints),
        max_tokens=100 * len(rows) + 100,
        schema=SCORES_SCHEMA,
    )
    return parse_scores(raw, {row["id"] for row in rows})


async def score_pending(
    conn: sqlite3.Connection,
    llm: LLM,
    profile: str,
    limit: int = MAX_PER_RUN,
    constraints: str = DEFAULT_CONSTRAINTS,
    graduation_year: int | None = None,
) -> int:
    """Score live, rules-passed, unscored postings in batches. Returns count scored. Commits.

    Postings in the Discover page's default terms (winter/spring '27, new grad) go first.
    """
    rows = conn.execute(
        "SELECT p.id, p.title, p.location, p.description, p.visa_sponsor, p.terms,"
        " c.name AS company_name"
        " FROM postings p JOIN companies c ON c.id = p.company_id"
        " WHERE p.rules_status = 'passed' AND p.llm_score IS NULL AND p.is_live = 1"
        " ORDER BY p.first_seen DESC"
    ).fetchall()
    rows.sort(  # stable, so newest-first holds within each group
        key=lambda row: (
            terms.bucket(row["title"], row["terms"], row["description"], graduation_year)
            not in terms.TARGET
        )
    )
    rows = rows[:limit]
    scored = 0
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset : offset + BATCH_SIZE]
        try:
            scores = await score_batch(llm, profile, batch, constraints)
        except LLMError as exc:
            log.warning("scoring batch failed: %s", exc)  # unscored rows retry next cycle
            break
        for score in scores:
            conn.execute(
                "UPDATE postings SET llm_score = ?, llm_rationale = ? WHERE id = ?",
                (score.score, score.rationale[:300], score.id),
            )
        conn.commit()
        scored += len(scores)
    return scored
