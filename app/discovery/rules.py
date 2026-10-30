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
STAGE_EXCLUDE = re.compile(  # postdocs need a PhD, though feeds list them as new grad
    r"\b(senior|staff|principal|lead|manager|director|head of|sr\.?|postdoc\w*)\b", re.I
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
# Location fields often spell the state out or name only the city ("Dallas, Texas", "NYC",
# "Sunnyvale", "San Jose California US"). Checked in the title and location only: in
# description prose, city names and "US" ("ABOUT US") would be noise.
US_PLACE = re.compile(
    r"\b((?-i:US)|alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware"
    r"|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine"
    r"|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada"
    r"|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma"
    r"|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah"
    r"|vermont|virginia|washington|wisconsin|wyoming|district of columbia"
    r"|nyc|manhattan|brooklyn|san francisco|sf|bay area|silicon valley|san jose|sunnyvale"
    r"|santa clara|mountain view|palo alto|menlo park|redwood city|cupertino|san mateo"
    r"|emeryville|berkeley|oakland|los angeles|san diego|irvine|seattle|bellevue|redmond"
    r"|kirkland|boston|chicago|austin|dallas|dfw|houston|plano|denver|boulder|atlanta|miami"
    r"|pittsburgh|philadelphia|salt lake city|detroit|ann arbor|minneapolis|raleigh"
    r"|nashville|baltimore|orlando|st\.? louis|kansas city)\b",
    re.I,
)
# Work-authorization dealbreakers for a candidate who needs sponsorship, or the employer's
# sign-off for CPT/OPT: sponsorship refused, CPT/OPT excluded, or permanent authorization,
# citizenship or U.S.-person status (export control) required. OPT/CPT match in capitals
# only, so "opt-out" doesn't count.
NO_SPONSOR = re.compile(
    # "We do not offer any type of employment-based immigration sponsorship", "are not
    # able to sponsor visas", "not eligible for CPT, OPT, or visa sponsorship"
    r"(?:\bnot|n['’]t|\bcannot|\bunable to)(?:\s+(?:currently|presently|be|able|to|anticipate))*"
    r"\s+(?:sponsor\w*|(?:provid(?:e|ing)|offer(?:ing)?|support(?:ing)?|consider(?:ing)?"
    r"|pursu(?:e|ing)|includ(?:e|ing)|requir(?:e|ing)|eligible for|available for)\b[^.;]{0,80}?"
    r"(?:(?<!-)\bsponsor\w*|\bvisas?\b|\b(?-i:OPT|CPT)\b|\bH-?1B\b"
    r"|\bwork authori[sz]ation support))"
    # "Visa sponsorship is not available", "Sponsorship now or in the future is NOT available"
    r"|\bsponsorship\b[^.;]{0,60}?\b(?:is|are|will be)\s+(?:not\s+|un)(?:available|offered)"
    r"|\bsponsorship available\s*:?\s*no\b"
    r"|\bno\s+(?:(?:visa|immigration|employer|relocation|or|and)\s+)*sponsorship"
    # "without current or future sponsorship", "without need of visa sponsorship"
    r"|\bwithout\s+(?:the\s+)?(?:need\s+(?:of|for)\s+|requiring\s+)?"
    r"(?:(?:any|current|future|now|or|and/or|visa|employer|immigration|employment)\s+)*"
    r"(?:sponsorship|visa support)"
    # "Applicants who require sponsorship now or in the future are not eligible"
    r"|\b(?:requir|need)\w*\s+(?:\w+\s+){0,3}?sponsorship\b[^.;]{0,80}?"
    r"\b(?:not eligible|ineligible|not be considered)"
    r"|\bauthori[sz]\w* to work (?:permanently|indefinitely)"
    r"|\bpermanent (?:work |employment )?authori[sz]ation"
    r"|\bauthorized on an unrestricted basis"
    r"|\bmust be (?:a )?(?:us|u\.s\.|united states) citizen"
    r"|\brequires? (?:us |u\.s\. |united states )?citizenship"
    r"|\bcitizenship (?:is )?required"
    r"|\bcitizens? (?:and (?:active )?green card holders )?only\b"
    r"|\bpermanent residents?\b.{0,120}?\b(?:asylees?|refugees?|protected individuals?)\b"
    r"|\bsecurity clearance",
    re.I,
)
# ...unless the posting welcomes students on their visa anyway: "sponsorship is not
# available for our internships, but we welcome applicants on a student visa".
STUDENT_VISA_OK = re.compile(
    r"\bwelcome\w*\s+(?:\w+\s+){0,4}?(?:student visas?\b|F-?1\b|(?-i:OPT|CPT)\b)"
    r"|\b(?-i:OPT|CPT)(?:/(?-i:OPT|CPT))?\s+(?:candidates|applicants|students)\s+(?:are\s+)?welcome",
    re.I,
)


# "graduating between December 2027 and May 2028", "Class of 2028": the dates a
# posting's graduation requirement names. Upper bounds ("by", "before", "no later
# than") are skipped — those accept anyone graduating earlier.
GRAD_CONTEXT = re.compile(r"(graduat\w*|class of)[^.;]{0,120}", re.I)
UPPER_BOUND = re.compile(r"\b(by|before|prior to|no later than|or earlier)\b", re.I)
GRAD_DATE = re.compile(
    r"\b(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    r"|(spring|summer|fall|autumn|winter))?\s*(?:of\s+)?(20\d\d)\b",
    re.I,
)
MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTHS = {name: number for number, name in enumerate(MONTH_NAMES, 1)}
SEASON_MONTHS = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12}


def graduates_too_late(description: str, graduation_year: int) -> bool:
    """Whether every graduation date the posting requires is after a spring graduation."""
    dates = []
    for context in GRAD_CONTEXT.finditer(description):
        if UPPER_BOUND.search(context.group(0)):
            continue
        for month, season, year in GRAD_DATE.findall(context.group(0)):
            number = MONTHS.get(month[:3].lower()) or SEASON_MONTHS.get(season.lower()) or 1
            dates.append((int(year), number))  # no month: assume the earliest, to be safe
    return bool(dates) and min(dates) > (graduation_year, 6)


# Required experience beyond early career: "3+ years of building models ... experience",
# "minimum of 4 years' experience". Only requirement lines count: those under a required/
# basic/qualifications heading, or that say "must"/"required"/"at least" themselves —
# never preferred / nice-to-have ones, nor company blurbs ("more than 30 years of
# investing experience"), which also end a requirements section when they follow it.
# Ranges count from their low end ("1–3 years"). Lines offering degree alternatives
# ("PhD, or Master's and 4+ years", "in lieu of education, 4 years") are skipped, since
# one of the paths may need no experience.
MAX_REQUIRED_YEARS = 2
MAX_PLAUSIBLE_YEARS = 15  # beyond this it's company history, not a requirement
EXPERIENCE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:[-–—]|to)\s*\d{1,2}\s*)?(?:or more\s+)?years?['’]?\s+"
    r"(?:of\s+)?[^.;\n]{0,80}?\bexperience",
    re.I,
)
REQUIRED_HEAD = re.compile(
    r"\b(basic|minimum|required|requirements?|qualifications|what you(['’]ll)? need"
    r"|you (have|bring)|must[- ]haves?)\b",
    re.I,
)
PREFERRED_HEAD = re.compile(r"\b(preferred|nice[- ]to[- ]have|bonus|pluses|a plus|desired)\b", re.I)
OTHER_HEAD = re.compile(
    r"^(about\b|who we are|our (company|culture|commitment|benefits)|benefits|compensation"
    r"|perks|equal (employment )?opportunity)",
    re.I,
)
SAYS_REQUIRED = re.compile(r"\b(required|must|minimum|at least)\b", re.I)
DEGREE = re.compile(
    r"\b(degree|education|bachelor\w*|master['’]?s|ph\.?\s?d|bs|ms|ba|ma|mba)\b", re.I
)
ALTERNATIVE = re.compile(r"\b(or|in lieu of|instead of)\b", re.I)
PARAGRAPH = 300  # requirement bullets are short; a longer line is prose (intro, about us)


def requirement_lines(description: str):
    """The description's requirement lines (see EXPERIENCE above for what counts)."""
    section = None  # None until a heading: "required" | "preferred"
    for line in description.splitlines():
        heading = line.strip(" -•*·:\t")
        if len(heading) < 60 and not EXPERIENCE.search(heading):
            if PREFERRED_HEAD.search(heading):
                section = "preferred"
                continue
            if REQUIRED_HEAD.search(heading):
                section = "required"
                continue
            if OTHER_HEAD.search(heading):
                section = None
                continue
        if section == "preferred" or PREFERRED_HEAD.search(line):
            continue
        says_required = SAYS_REQUIRED.search(line)
        if not says_required and (section != "required" or len(line) > PARAGRAPH):
            continue
        yield line


def required_years(description: str) -> int:
    """The most years of experience a requirement asks for (0 if none)."""
    most = 0
    for line in requirement_lines(description):
        if ALTERNATIVE.search(line) and DEGREE.search(line):
            continue
        for match in EXPERIENCE.finditer(line):
            if (years := int(match.group(1))) <= MAX_PLAUSIBLE_YEARS:
                most = max(most, years)
    return most


# Degree-gated roles, checked against DEGREES (the candidate's: bs, ms, mba, phd). A PhD
# or MBA counts as required when the title names it, or a requirement line does, without
# a lower or other degree as an alternative ("MS/PhD", "Master's or PhD" pass).
PHD = re.compile(r"\bph\.?\s?d\b", re.I)
MBA = re.compile(r"\bmba\b", re.I)
OTHER_DEGREE = re.compile(  # not BA/MA: they'd match "Boston, MA"
    r"\b(bachelor\w*|master['’]?s|undergrad\w*|graduate degree|b\.?s\.?|m\.?s\.?|b\.?sc|m\.?sc"
    r"|m\.?eng)(?=\W|$)",
    re.I,
)


def degree_required(title: str, description: str, degrees: frozenset[str]) -> str | None:
    """'phd' or 'mba' if the posting requires a degree the candidate doesn't have."""
    lines = list(requirement_lines(description))
    # options are often listed one per line ("Bachelor's and 2+ years OR" / "PhD in CS"):
    # any requirement line accepting another degree makes the PhD/MBA one an alternative
    other_accepted = any(OTHER_DEGREE.search(line) for line in lines)
    for degree, pattern in (("phd", PHD), ("mba", MBA)):
        if degree in degrees:
            continue
        if pattern.search(title) and not OTHER_DEGREE.search(title):
            return degree
        if not other_accepted and any(pattern.search(line) for line in lines):
            return degree
    return None


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
    graduation_year: int | None = None,
    degrees: frozenset[str] = frozenset(),
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
    if required_years(description) > MAX_REQUIRED_YEARS:
        return False, "stage:years-of-experience"
    if not FIELD.search(f"{title} {description}"):
        return False, "field:no-ai-ml-ds-terms"
    loc_blob = f"{location} {description[:500]}"
    if not (
        US_PHRASE.search(f"{title} {loc_blob}")
        or US_STATE.search(loc_blob)
        or US_PLACE.search(f"{title} {location}")
    ):
        return False, "geo:no-us-signal"
    if (
        local
        and local.school_term.search(f"{title} {terms} {description[:500]}")
        and not remote
        and not local.area.search(location)
    ):
        return False, "geo:school-term-not-local-or-remote"
    if NO_SPONSOR.search(description) and not STUDENT_VISA_OK.search(description):
        return False, "sponsor:explicit-negation"
    if graduation_year and graduates_too_late(description, graduation_year):
        return False, "timing:graduation-after-candidate"
    if degrees and (degree := degree_required(title, description, degrees)):
        return False, f"degree:{degree}-required"
    return True, ""
