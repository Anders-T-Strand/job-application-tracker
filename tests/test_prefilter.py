import pytest

from app.classifier.prefilter import passes_prefilter, sender_domain

SAMPLES = [
    # (from, subject, snippet, expected)
    ("no-reply@greenhouse.io", "Thank you for applying to Acme", "We received your...", True),
    ("acme@myworkday.com", "Your Workday account", "", True),
    ("careers@bigco.com", "Update on your application", "Thank you for applying", True),
    ("talent@startup.io", "Unfortunately...", "we have decided to move forward with", True),
    ("no-reply@hackerrank.com", "Acme Coding Assessment", "You have been invited", True),
    ("jane@agency.com", "Exciting opportunity", "I'm a recruiter working with a fintech", True),
    ("noreply@webcruiter.no", "Søknaden din er mottatt", "", True),
    ("hr@norskfirma.no", "Angående din søknad", "Vi har dessverre valgt å gå videre", True),
    ("deals@webshop.com", "50% off everything this weekend", "Shop now before it ends", False),
    ("no-reply@parcels.com", "Your package has shipped", "Track your delivery here", False),
    ("newsletter@technews.com", "This week in tech", "The latest gadgets reviewed", False),
    ("jobalerts-noreply@linkedin.com", "Applied AI Engineer at Zello", "Zello Applied AI", False),
    ("smartjobalert@postjobfree.com", "New jobs: ML Engineer", "Apply now - recruiting", False),
    ("opportunities@careeralerts-us.pwc.com", "Roles for you", "Your application journey", False),
    ("jobs-noreply@linkedin.com", "Alex, your application was sent to Acme", "", True),
]


@pytest.mark.parametrize("from_addr,subject,snippet,expected", SAMPLES)
def test_prefilter_samples(from_addr, subject, snippet, expected):
    assert passes_prefilter(from_addr, subject, snippet) is expected


def test_sender_domain():
    assert sender_domain("No-Reply@Sub.Greenhouse.IO") == "sub.greenhouse.io"
    assert sender_domain("not-an-address") == ""


def test_subdomain_of_ats_domain_matches():
    assert passes_prefilter("acme@notify.icims.com", "hello", "") is True
    # but a lookalike domain must not
    assert passes_prefilter("x@notgreenhouse.io", "hello", "") is False
