"""Stage-1 rules filter: cheap regexes that reject ~90% of postings before the LLM.

Expect to tune STAGE_INCLUDE over time — some companies title internships as
"Student Researcher" or "Fellowship". Every rejection records which rule fired
(rules_reason) so misses are auditable.
"""

import re
from dataclasses import dataclass

from app.discovery.terms import school_terms

STAGE_INCLUDE = re.compile(
    r"\b(intern(ship)?|co[- ]?op|new grad|university|early career|entry[- ]level|graduate)\b",
    re.I,
)
STAGE_EXCLUDE = re.compile(
    r"\b(senior|staff|principal|lead|manager|director|head of|sr\.?)\b", re.I
)
FIELD = re.compile(  # stems end in \w* so "data scien" also matches "Data Scientist"
    r"\b(machine learning|\bml\b|\bai\b|artificial intelligence|data scien\w*|deep learning|"
    r"nlp|computer vision|llms?|generative|mlops|data engineer\w*|applied scien\w*)\b",
    re.I,
)
# The US signal is split in two: phrases match case-insensitively ("United
# States", "USA"), while two-letter state codes stay case-sensitive so words
# like "in", "or", "me", "hi" in prose don't false-positive.
US_PHRASE = re.compile(
    r"\b(united states|usa|u\.s\.|remote[- ]?us|us[- ]?remote)\b",
    re.I,
)
US_STATE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|"
    r"NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b"
)
NO_SPONSOR = re.compile(
    r"(without (the need for |requiring )?(visa )?sponsorship"
    r"|not (be able to |currently )?(provide|offer|sponsor)"
    r"[^.]{0,40}(visa|sponsorship|work authorization)"
    r"|unable to sponsor"
    r"|no (visa )?sponsorship"
    r"|must be (a )?(us|u\.s\.) citizen"
    r"|citizenship (is )?required"
    r"|security clearance)",
    re.I,
)


@dataclass(frozen=True)
class Locality:
    """A candidate still in school can only be on-site near campus until graduating."""

    school_term: re.Pattern  # the terms before graduation (terms.school_terms)
    area: re.Pattern  # places within reach of campus


def locality(graduation_year: int | None, local_area: str) -> Locality | None:
    """From GRADUATION_YEAR and LOCAL_AREA ("City, City, ..."); None if either is unset."""
    places = [place.strip() for place in local_area.split(",") if place.strip()]
    if not graduation_year or not places:
        return None
    area = re.compile(r"\b(" + "|".join(map(re.escape, places)) + r")\b", re.I)
    return Locality(school_terms(graduation_year), area)


def apply_rules(
    title: str,
    location: str,
    description: str,
    remote: bool = False,
    terms: str = "",
    local: Locality | None = None,
) -> tuple[bool, str]:
    """Returns (passed, reason). reason is '' on pass, else the rule that fired.

    `terms` comes from a feed that lists only internships and new-grad roles, so it
    stands in for an early-career word in the title. With `local` set, roles that
    explicitly name a remaining school term (in the title, the feed's terms, or the
    top of the description) must be local or remote; postings without an explicit term
    pass, and the scorer weighs timing and location instead.
    """
    if not (terms or STAGE_INCLUDE.search(title)):
        return False, "stage:no-early-career-signal"
    if STAGE_EXCLUDE.search(title):
        return False, "stage:senior-term-in-title"
    if not FIELD.search(f"{title} {description}"):
        return False, "field:no-ai-ml-ds-terms"
    loc_blob = f"{location} {description[:500]}"
    if not (US_PHRASE.search(loc_blob) or US_STATE.search(loc_blob)):
        return False, "geo:no-us-signal"
    if (
        local
        and local.school_term.search(f"{title} {terms} {description[:500]}")
        and not remote
        and not local.area.search(location)
    ):
        return False, "geo:school-term-not-local-or-remote"
    if NO_SPONSOR.search(description):
        return False, "sponsor:explicit-negation"
    return True, ""
