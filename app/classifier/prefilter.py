"""Cheap heuristic filter so only likely-recruiting mail hits the LLM."""

ATS_DOMAINS = (
    "myworkday.com",
    "myworkdayjobs.com",
    "greenhouse.io",
    "greenhouse-mail.io",
    "lever.co",
    "icims.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "jobvite.com",
    "taleo.net",
    "successfactors.com",
    "oraclecloud.com",
    "workablemail.com",
    "workable.com",
    "bamboohr.com",
    "teamtailor.com",
    "recruitee.com",
    "breezy.hr",
    "applytojob.com",  # JazzHR
    "eightfold.ai",
    "avature.net",
    "phenompeople.com",
    "hirevue.com",
    # assessment platforms
    "hackerrank.com",
    "codility.com",
    "codesignal.com",
    "testgorilla.com",
    # common Nordic ATS
    "jobylon.com",
    "webcruiter.no",
    "webcruiter.com",
    "recman.no",
    "easycruit.com",
    "varbi.com",
    "jobbnorge.no",
)

# Job-alert digests (jobalerts-noreply@linkedin.com, smartjobalert@postjobfree.com, ...)
# never report on an application, yet titles like "Applied AI Engineer" trip the
# "applied" keyword. LinkedIn's jobs-noreply@ (real "application sent" mail) is unaffected.
JOB_ALERT_MARKERS = ("jobalert", "careeralert")
# ...but some employers send real application mail from an alert domain ("Next Steps for Your
# Application" from hiring@jobalerts.<employer>.com), so a subject like these still gets through.
APPLICATION_SUBJECTS = (
    "your application",
    "thank you for applying",
    "thanks for applying",
    "application received",
    "received your application",
)

KEYWORDS = (
    # English
    "application",
    "applied",
    "applying",
    "candidacy",
    "candidate",
    "interview",
    "assessment",
    "coding challenge",
    "coding test",
    "recruiter",
    "recruiting",
    "recruitment",
    "talent acquisition",
    "vacancy",
    "unfortunately",
    "job offer",
    "offer letter",
    "thank you for applying",
    "we received your application",
    "your application",
    # Norwegian
    "søknad",
    "intervju",
    "stilling",
    "dessverre",
    "kandidat",
    "rekrutter",
    "ansettelse",
    "jobbtilbud",
)


def sender_domain(from_addr: str) -> str:
    return from_addr.rsplit("@", 1)[-1].lower().strip() if "@" in from_addr else ""


def is_ats_domain(domain: str) -> bool:
    """An applicant-tracking system's domain, which sends mail for many employers."""
    return any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS)


def passes_prefilter(from_addr: str, subject: str, snippet: str) -> bool:
    if any(marker in from_addr.lower() for marker in JOB_ALERT_MARKERS):
        return any(phrase in subject.lower() for phrase in APPLICATION_SUBJECTS)
    domain = sender_domain(from_addr)
    if domain and is_ats_domain(domain):
        return True
    haystack = f"{subject}\n{snippet}".lower()
    return any(keyword in haystack for keyword in KEYWORDS)
