import json

import httpx
import pytest

from app.discovery import details, feeds, poll, store
from app.discovery.ats import Posting
from tests.helpers import add_company

WORKDAY_API = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Austin-TX/ML-Intern_R1"
LD_PAGE = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [{"@type": "Organization"},
 {"@type": "JobPosting", "description": "<p>Applied AI intern, &amp; more</p>"}]}
</script></head></html>"""

RESPONSES = {
    WORKDAY_API: {"jobPostingInfo": {"jobDescription": "<p>Spring 2027 co-op.</p><p>Python</p>"}},
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs/42": {
        "content": "&lt;p&gt;Build &amp;amp; ship models&lt;/p&gt;"
    },
    "https://api.lever.co/v0/postings/acme/abc-123": {
        "descriptionPlain": "Lever intro.",
        "lists": [{"text": "Requirements", "content": "<li>PyTorch</li>"}],
        "additionalPlain": "Visa sponsorship available.",
    },
    "https://api.ashbyhq.com/posting-api/job-board/acme": {
        "jobs": [{"id": "j1", "descriptionHtml": "<p>Ashby one</p>"}, {"id": "j2"}]
    },
    "https://api.smartrecruiters.com/v1/companies/LLNL/postings/3743": {
        "jobAd": {"sections": {"jobDescription": {"text": "<p>GIS developer</p>"}}}
    },
    "https://hcwp.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
    "recruitingCEJobRequisitionDetails": {
        "items": [
            {"ExternalDescriptionStr": "<p>Oracle role</p>", "ExternalQualificationsStr": "SQL"}
        ]
    },
    "https://apply.workable.com/api/v2/accounts/raydar/jobs/31364F00E3": {
        "description": "<p>Workable role</p>",
        "requirements": "<p>Python</p>",
    },
}


def _transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        calls.append(str(request.url))
        if url in RESPONSES:
            return httpx.Response(200, json=RESPONSES[url])
        if url == "https://careers.example.com/jobs/7":
            return httpx.Response(200, text=LD_PAGE)
        if url == "https://careers.example.com/no-json-ld":
            return httpx.Response(200, text="<html><body>Apply now</body></html>")
        if url == "https://zapply.jobs/l/d/workday-acme-R1":
            return httpx.Response(
                302, headers={"location": WORKDAY_API.replace("wday/cxs/acme/", "")}
            )
        if url == "https://zapply.jobs/l/d/workday-gone-R9":
            return httpx.Response(302, headers={"location": "https://zapply.jobs/jobs/"})
        if url == "https://zapply.jobs/jobs/":
            return httpx.Response(200, text="<html>Browse jobs</html>")
        if url.endswith("/flaky"):
            return httpx.Response(503)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/ML-Intern_R1/apply",
            "Spring 2027 co-op.\nPython",
        ),
        ("https://job-boards.greenhouse.io/acme/jobs/42", "Build & ship models"),
        (
            "https://jobs.lever.co/acme/abc-123/apply",
            "Lever intro.\nRequirements\nPyTorch\nVisa sponsorship available.",
        ),
        ("https://jobs.ashbyhq.com/acme/j1/application?embed=true", "Ashby one"),
        ("https://jobs.smartrecruiters.com/LLNL/3743-gis-developer", "GIS developer"),
        (
            "https://hcwp.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/2015589",
            "Oracle role\nSQL",
        ),
        ("https://apply.workable.com/raydar/j/31364F00E3/apply", "Workable role\nPython"),
        ("https://careers.example.com/jobs/7", "Applied AI intern, & more"),  # JSON-LD fallback
        ("https://zapply.jobs/l/d/workday-acme-R1", "Spring 2027 co-op.\nPython"),  # redirect
    ],
)
async def test_fetch_description_per_source(url, expected):
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_transport(calls), follow_redirects=True) as http:
        assert await details.fetch_description(http, url, {}) == expected


async def test_closed_postings_and_board_cache():
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_transport(calls), follow_redirects=True) as http:
        with pytest.raises(details.Closed):  # Zapply sends expired jobs back to its job list
            await details.fetch_description(http, "https://zapply.jobs/l/d/workday-gone-R9", {})
        with pytest.raises(details.Closed):  # API 404
            await details.fetch_description(http, "https://jobs.lever.co/acme/missing", {})
        cache: dict = {}
        with pytest.raises(details.Closed):  # not on the Ashby board any more
            await details.fetch_description(http, "https://jobs.ashbyhq.com/acme/j9", cache)
        assert (
            await details.fetch_description(http, "https://jobs.ashbyhq.com/acme/j2", cache) == ""
        )
    assert sum("job-board/acme" in call for call in calls) == 1  # one board fetch per run


def _feed_posting(conn, title: str, url: str, description: str = "", terms: str = "Internship"):
    company_id = add_company(conn, name=f"Co {title}", ats_type="feed", board_token="")
    store.upsert_postings(
        conn,
        company_id,
        [
            Posting(
                title=title, location="Austin, TX", url=url, description=description, terms=terms
            )
        ],
        "2026-10-30T00:00:00Z",
        feed="simplify-intern",
    )
    conn.execute(
        "UPDATE postings SET rules_status = 'passed', llm_score = 70 WHERE title = ?", (title,)
    )
    conn.commit()


def _row(conn, title: str):
    return conn.execute("SELECT * FROM postings WHERE title = ?", (title,)).fetchone()


async def test_enrich_updates_postings_by_outcome(conn, monkeypatch):
    monkeypatch.setattr(details, "REQUEST_GAP_SECONDS", 0)
    _feed_posting(
        conn,
        "ML Co-op - Spring 2027",
        "https://acme.wd5.myworkdayjobs.com/External/job/Austin-TX/ML-Intern_R1",
        description="Category: AI/ML/Data.",
    )
    _feed_posting(conn, "Data Intern", "https://zapply.jobs/l/d/workday-gone-R9")
    _feed_posting(conn, "AI Intern", "https://careers.example.com/flaky")
    _feed_posting(conn, "Quant Intern", "https://careers.example.com/no-json-ld")
    _feed_posting(conn, "ML Intern - Summer 2027", "https://jobs.lever.co/acme/abc-123")

    calls: list[str] = []
    async with httpx.AsyncClient(transport=_transport(calls), follow_redirects=True) as http:
        assert await details.enrich(conn, http, graduation_year=2027) == 1

    described = _row(conn, "ML Co-op - Spring 2027")
    assert described["detail_status"] == "ok"
    assert described["description"] == "Category: AI/ML/Data.\n\nSpring 2027 co-op.\nPython"
    assert described["rules_status"] == "pending" and described["llm_score"] is None
    assert tuple(_row(conn, "Data Intern")[k] for k in ("detail_status", "is_live")) == (
        "closed",
        0,
    )
    assert _row(conn, "AI Intern")["detail_status"] == "failed"
    assert _row(conn, "Quant Intern")["detail_status"] == "empty"
    # a Summer 2027 internship isn't worth a request for a May 2027 graduate
    assert _row(conn, "ML Intern - Summer 2027")["detail_status"] is None
    assert not any("lever" in call for call in calls)

    async with httpx.AsyncClient(transport=_transport(calls), follow_redirects=True) as http:
        calls.clear()
        assert await details.enrich(conn, http, graduation_year=2027) == 0
    assert calls == []  # failures wait RETRY_FAILED_DAYS; everything else is settled


async def test_enrich_puts_the_default_view_first_and_respects_the_cap(conn, monkeypatch):
    monkeypatch.setattr(details, "REQUEST_GAP_SECONDS", 0)
    _feed_posting(conn, "Data Intern", "https://careers.example.com/jobs/7")  # unspecified
    _feed_posting(
        conn, "ML Engineer", "https://job-boards.greenhouse.io/acme/jobs/42", terms="New Grad"
    )
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_transport(calls), follow_redirects=True) as http:
        assert await details.enrich(conn, http, graduation_year=2027, limit=1) == 1
    assert _row(conn, "ML Engineer")["detail_status"] == "ok"  # new grad before unspecified
    assert _row(conn, "Data Intern")["detail_status"] is None


async def test_discovery_cycle_refilters_on_the_fetched_text(conn, settings, monkeypatch):
    company_id = add_company(conn, name="Acme Feed", ats_type="feed", board_token="")
    store.upsert_postings(
        conn,
        company_id,
        [Posting(title="ML Intern", location="Austin, TX", url="https://x/1", terms="Internship")],
        "2026-10-30T00:00:00Z",
        feed="simplify-intern",
    )
    conn.commit()

    async def no_feeds(conn, http):
        return None

    async def fake_fetch(http, url, cache):
        return "Build ML models in Python. We are unable to sponsor visas for this role."

    monkeypatch.setattr(feeds, "poll", no_feeds)
    monkeypatch.setattr(details, "fetch_description", fake_fetch)
    monkeypatch.setattr(details, "REQUEST_GAP_SECONDS", 0)
    monkeypatch.setattr(poll, "make_llm", lambda settings: None)

    await poll.discover_once(settings.model_copy(update={"feeds_enabled": True}), boards=False)
    row = conn.execute("SELECT rules_status, rules_reason, detail_status FROM postings").fetchone()
    assert tuple(row) == ("rejected", "sponsor:explicit-negation", "ok")


def test_json_ld_list_form_is_understood():
    block = json.dumps([{"@type": "JobPosting", "description": "x"}])
    assert details.JSON_LD.search(f'<script type="application/ld+json">{block}</script>')
