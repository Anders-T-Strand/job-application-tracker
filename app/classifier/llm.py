"""LLM classification of candidate emails (strict JSON out)."""

import json
import logging

from pydantic import ValidationError

from app.llm import LLM
from app.models import EmailClassification, JobPosting

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 1500

SYSTEM_PROMPT = """\
You classify one email for a personal job-application tracker.

Respond with ONLY a JSON object — no prose, no code fences:
{"is_job_related": <bool>, "type": "<type>", "company": <string or null>, \
"role_title": <string or null>, "confidence": <0.0-1.0>}

"type" must be one of:
- application_confirmation: automated "we received your application" mail
- rejection: the application was declined
- oa_invite: invitation to an online assessment / coding test
- interview_invite: invitation or scheduling for an interview or screening call
- recruiter_outreach: a recruiter contacting me about a role I did not apply for
- offer: a job offer
- other: everything else (job alerts, newsletters, marketing, unrelated mail)

"company" is the employer, not the ATS platform (e.g. "Acme" not "Greenhouse"). \
Use null when unclear.
If the email is not about recruiting or a job application, use is_job_related=false \
and type "other".
Job titles often start with "Applied" (Applied AI Engineer, Applied Scientist); that word \
is part of the title, not a sign that I applied. Job-alert and recommendation emails listing \
open roles are "other", even when they name a single company and role.
"""


def _json_slice(raw: str) -> str | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    return raw[start : end + 1]


def parse_classification(raw: str) -> EmailClassification | None:
    """Extract and validate the JSON object from model output. None if unusable."""
    sliced = _json_slice(raw)
    if sliced is None:
        return None
    try:
        return EmailClassification.model_validate(json.loads(sliced))
    except (json.JSONDecodeError, ValidationError):
        log.warning("unparseable classifier output: %.200s", raw)
        return None


EXTRACT_SYSTEM_PROMPT = """\
You extract job-posting details from a page URL and title for a personal \
job-application tracker.

Respond with ONLY a JSON object — no prose, no code fences:
{"company": <string or null>, "role_title": <string or null>}

"company" is the employer (not a job board like LinkedIn or Indeed). \
Use null for anything you cannot determine confidently.
"""


async def extract_job_posting(llm: LLM, url: str, title: str) -> JobPosting | None:
    """Best-effort company/role extraction for the quick-log flow."""
    raw = await llm.complete(
        EXTRACT_SYSTEM_PROMPT,
        f"URL: {url[:500]}\nPage title: {title[:300]}",
        max_tokens=150,
        schema=JobPosting.model_json_schema(),
    )
    sliced = _json_slice(raw)
    if sliced is None:
        return None
    try:
        return JobPosting.model_validate(json.loads(sliced))
    except (json.JSONDecodeError, ValidationError):
        log.warning("unparseable extraction output: %.200s", raw)
        return None


async def classify_email(
    llm: LLM, from_addr: str, subject: str, body: str
) -> EmailClassification | None:
    raw = await llm.complete(
        SYSTEM_PROMPT,
        f"From: {from_addr}\nSubject: {subject}\n\n{body[:MAX_BODY_CHARS]}",
        max_tokens=300,
        schema=EmailClassification.model_json_schema(),
    )
    return parse_classification(raw)
