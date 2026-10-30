"""Which hiring cycle a posting belongs to, relative to the candidate's graduation.

With GRADUATION_YEAR set (a spring graduation, e.g. 2027):
- final_term: internships/co-ops in the last school term — Winter (year-1/year) or Spring
  (year). Feeds label that winter either way ("Winter 2026" or "Winter 2027").
- new_grad: full-time new-grad / entry-level roles
- later: Summer (year) or later internships, which usually require returning to school
- unspecified: an internship that doesn't say
Without it, only new_grad vs unspecified is told apart.

Evidence order: a feed's explicit terms and the title first, then the top of the
description (where ATS postings usually name the season).
"""

import functools
import re

# "Summer Analyst/Associate" is how banks title internships; student jobs are for
# current students. Neither is a new-grad role, whatever list it came from.
INTERN = re.compile(
    r"\b(intern(ship)?s?|co[- ]?op|summer (associate|analyst)s?|apprentice(ship)?s?"
    r"|student (employee|assistant|worker|researcher)s?|for students)\b",
    re.I,
)
SUMMER = re.compile(r"\bsummer\b", re.I)
YEAR = re.compile(r"\b20(\d\d)\b")
NEW_GRAD = re.compile(
    r"\b(new[- ]grad(uate)?|recent grad(uate)?|university grad(uate)?|graduate (program|role)"
    r"|entry[- ]level|early[- ]career|20\d\d grad(uate)?s?)\b",
    re.I,
)
DESC_CHARS = 800
TARGET = ("final_term", "new_grad")  # what the Discover page shows by default


def _season(names: str, year: int) -> str:
    """Regex for e.g. "Spring 2027", "spring '27", "Spring of 2027", "2027 Spring"."""
    yy = f"{year % 100:02d}"
    return rf"(?:(?:{names})\s*(?:of\s*)?['’]?(?:20)?{yy}|(?:20)?{yy}\s+(?:{names}))"


@functools.cache
def _cycle(year: int) -> tuple[re.Pattern, re.Pattern]:
    final = (
        rf"\b(?:{_season('winter', year - 1)}|{_season('winter', year)}"
        rf"|{_season('spring', year)}|jan(?:uary)?\s*(?:20)?{year % 100:02d})\b"
    )
    later = (
        rf"\b(?:{_season('summer|fall|autumn', year)}"
        rf"|{_season('winter|spring|summer|fall', year + 1)})\b"
    )
    return re.compile(final, re.I), re.compile(later, re.I)


@functools.cache
def school_terms(year: int) -> re.Pattern:
    """The terms still spent in school before a spring graduation: fall, winter, spring."""
    final, _ = _cycle(year)
    return re.compile(rf"\b{_season('fall|autumn', year - 1)}\b|{final.pattern}", re.I)


def labels(year: int | None) -> dict[str, str]:
    short = f"'{year % 100:02d}" if year else ""
    return {
        "final_term": f"Winter/Spring {short}",
        "new_grad": "New grad",
        "later": f"Summer {short}+",
        "unspecified": "",
    }


def bucket(
    title: str, terms: str | None = None, description: str | None = None, year: int | None = None
) -> str:
    terms = terms or ""
    internship = bool(INTERN.search(title)) or bool(terms and terms != "New Grad")
    if not internship and ("New Grad" in terms or NEW_GRAD.search(title)):
        return "new_grad"
    if year:
        final, later = _cycle(year)
        for text in (f"{title} {terms}", (description or "")[:DESC_CHARS]):
            if final.search(text):
                return "final_term"
            if later.search(text):
                return "later"
        # "2027 ML Center of Excellence - Summer Associate": a summer internship with the
        # graduation year (or later) anywhere in the title
        if (
            internship
            and SUMMER.search(title)
            and any(2000 + int(yy) >= year for yy in YEAR.findall(title))
        ):
            return "later"
    return "unspecified"
