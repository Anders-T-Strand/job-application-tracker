import json
from datetime import UTC, datetime

import httpx

from app import db
from app.discovery import feeds, poll, store
from app.discovery.ats import Posting
from tests.helpers import add_company

ZAPPLY_INTERN, ZAPPLY_NEWGRAD, SIMPLIFY_INTERN, SIMPLIFY_NEWGRAD = feeds.FEEDS
NOW = datetime(2026, 10, 14, tzinfo=UTC)
HEADER = "| Company | Role | Location | Posted | Visa | **Apply** |\n|---|---|---|---|---|---|"


def _row(company: str, role: str, location: str, visa: str, slug: str) -> str:
    """One README table row in Zapply's format."""
    apply = '[<img src="images/apply.png" width="80" alt="Apply">]'
    url = f"https://zapply.jobs/l/d/{slug}?s=gh-internships-2027"
    return f"| **{company}** | {role} | {location} | 1d | {visa} | {apply}({url}) |"


KLA = _row(
    "KLA",
    "Computer Vision/Machine Learning Intern",
    "Milpitas, CA",
    "✅ Sponsor",
    "workday-kla-search-2641921",
)
README = "\n".join(
    [
        "# Internships 2027",
        "<summary><h3>📊 <strong>Data Science & AI</strong></h3></summary>",
        HEADER,
        KLA,
        _row(
            "Nissan",
            "Data Science Intern - Summer 2027",
            "Smyrna, Tennessee - United...",
            "",
            "workday-alliance-nissanjobs-R00215331",
        ),
        _row("1Password", "Data Intern", "Remote (United States  Canada)", "", "ashby-1p-412c"),
        "<summary><h3>📂 <strong>Other Internships</strong></h3></summary>",
        HEADER,
        KLA,  # the same job under a second category
    ]
)


def _simplify(id_: str, title: str, **overrides) -> dict:
    item = {
        "id": id_,
        "company_name": "Capital One",
        "title": title,
        "locations": ["Plano, TX"],
        "terms": ["Winter 2027", "Spring 2027"],
        "category": "AI/ML/Data",
        "sponsorship": "Other",
        "active": True,
        "is_visible": True,
        "url": f"https://capitalone.wd12.myworkdayjobs.com/job/{id_}",
        "date_posted": NOW.timestamp() - 86400 * 10,
    }
    return {**item, **overrides}


# --- Zapply -------------------------------------------------------------------


def test_parse_zapply_reads_rows_flags_sponsors_and_dedupes():
    listings = feeds.parse_zapply(README)
    assert [(company, p.title) for company, p in listings] == [
        ("KLA", "Computer Vision/Machine Learning Intern"),  # listed twice, kept once
        ("Nissan", "Data Science Intern - Summer 2027"),
        ("1Password", "Data Intern"),
    ]
    kla, nissan, onepass = (p for _, p in listings)
    assert kla.visa_sponsor and not nissan.visa_sponsor  # blank = unknown, stored as 0
    assert kla.external_id == "workday-kla-search-2641921"
    assert kla.url == "https://zapply.jobs/l/d/workday-kla-search-2641921?s=gh-internships-2027"
    assert kla.location == "Milpitas, CA" and not kla.remote
    assert onepass.remote
    assert {p.terms for _, p in listings} == {"Internship"}


def test_new_grad_list_tags_new_grad_roles_but_not_its_internships():
    markdown = "\n".join(
        [
            HEADER,
            _row("Acme", "Data Scientist I", "Austin, TX", "", "wd-1"),
            _row("Acme", "2027 Graduate Intern", "Austin, TX", "", "wd-2"),
        ]
    )
    assert [p.terms for _, p in feeds.parse_zapply(markdown, new_grad=True)] == [
        "New Grad",
        "Internship",
    ]


def test_ingest_skips_polled_companies_and_expires_old_zapply_listings(conn):
    add_company(conn, name="1Password", ats_type="ashby", board_token="1password")
    listings = feeds.parse_zapply(README)

    assert feeds.ingest(conn, ZAPPLY_INTERN, listings, "2026-10-14T00:00:00Z") == 2
    rows = conn.execute(
        "SELECT c.name, c.ats_type, p.visa_sponsor, p.is_live, p.feed FROM postings p"
        " JOIN companies c ON c.id = p.company_id ORDER BY c.name"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("KLA", "feed", 1, 1, "zapply-intern"),
        ("Nissan", "feed", 0, 1, "zapply-intern"),
    ]  # 1Password skipped: its own board is polled

    assert feeds.ingest(conn, ZAPPLY_INTERN, listings, "2026-10-15T00:00:00Z") == 0
    # dropping off the README doesn't close a job; age does
    feeds.ingest(conn, ZAPPLY_INTERN, [], "2026-12-01T00:00:00Z")
    assert conn.execute("SELECT SUM(is_live) FROM postings").fetchone()[0] == 0


# --- Simplify -----------------------------------------------------------------


def test_parse_simplify_keeps_active_recent_listings_with_terms():
    items = [
        _simplify("a", "ML Engineer Intern"),
        _simplify("b", "Old Intern", date_posted=NOW.timestamp() - 86400 * 200),  # stale
        _simplify("c", "Closed Intern", active=False),
        _simplify("d", "Hidden Intern", is_visible=False),
        _simplify("e", "Citizens Only Intern", sponsorship="U.S. Citizenship is Required"),
        _simplify("f", "No Term Intern", terms=["N/A"]),
    ]
    listings, active = feeds.parse_simplify(items, new_grad=False, now=NOW)
    assert active == {"a", "b", "e", "f"}  # liveness counts old-but-active listings too
    by_title = {p.title: p for _, p in listings}
    assert set(by_title) == {"ML Engineer Intern", "Citizens Only Intern", "No Term Intern"}
    ml = by_title["ML Engineer Intern"]
    assert ml.terms == "Winter 2027, Spring 2027"
    assert ml.description == "Category: AI/ML/Data."
    assert ml.location == "Plano, TX" and ml.external_id == "a"
    assert "No visa sponsorship" in by_title["Citizens Only Intern"].description
    assert by_title["No Term Intern"].terms == "Internship"


def test_simplify_new_grad_and_liveness_follows_active_flag(conn):
    items = [_simplify("n1", "Data Scientist I", terms=None), _simplify("n2", "ML Engineer")]
    listings, active = feeds.parse_simplify(items, new_grad=True, now=NOW)
    assert {p.terms for _, p in listings} == {"New Grad"}

    feeds.ingest(conn, SIMPLIFY_NEWGRAD, listings, "2026-10-14T00:00:00Z", active)
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE is_live = 1").fetchone()[0] == 2
    feeds.ingest(conn, SIMPLIFY_NEWGRAD, listings[:1], "2026-10-15T00:00:00Z", {"n1"})
    live = conn.execute("SELECT title FROM postings WHERE is_live = 1").fetchall()
    assert [r[0] for r in live] == ["Data Scientist I"]


def test_same_job_in_two_feeds_is_stored_once_and_keeps_the_sponsor_mark(conn):
    zapply = [
        (
            "Capital One",
            Posting(
                title="ML Intern",
                location="Plano, TX",
                url="z",
                visa_sponsor=True,
                terms="Internship",
            ),
        )
    ]
    simplify = [
        (
            "Capital One",
            Posting(title="ml intern", location="Plano, Texas", url="s", terms="Spring 2027"),
        )
    ]
    assert feeds.ingest(conn, ZAPPLY_INTERN, zapply, "2026-10-14T00:00:00Z") == 1
    assert feeds.ingest(conn, SIMPLIFY_INTERN, simplify, "2026-10-14T01:00:00Z", {"x"}) == 0
    row = conn.execute("SELECT visa_sponsor, terms, url, feed FROM postings").fetchone()
    assert tuple(row) == (1, "Internship", "z", "zapply-intern")


# --- polling ------------------------------------------------------------------


async def test_poll_feed_uses_etag_and_ignores_unparseable_responses(conn):
    calls: list[str | None] = []
    responses = iter(
        [
            httpx.Response(200, text=README, headers={"etag": '"v1"'}),
            httpx.Response(304),
            httpx.Response(200, text="# README with no tables", headers={"etag": '"v2"'}),
            httpx.Response(503),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("if-none-match"))
        return next(responses)

    key = f"feed_etag:{ZAPPLY_INTERN.name}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await feeds.poll_feed(conn, http, ZAPPLY_INTERN)
        assert db.get_state(conn, key) == '"v1"'
        await feeds.poll_feed(conn, http, ZAPPLY_INTERN)  # 304: nothing to do
        await feeds.poll_feed(conn, http, ZAPPLY_INTERN)  # layout changed: etag not advanced
        assert db.get_state(conn, key) == '"v1"'
        await feeds.poll_feed(conn, http, ZAPPLY_INTERN)  # server error: logged, not raised
    assert calls == [None, '"v1"', '"v1"', '"v1"']
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 3


async def test_poll_runs_each_feed_on_its_own_schedule(conn):
    fetched: list[str] = []
    simplify_json = json.dumps([_simplify("s1", "ML Intern")])

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        body = simplify_json if request.url.path.endswith(".json") else README
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await feeds.poll(conn, http)
        assert len(fetched) == 4  # first run: everything is due
        await feeds.poll(conn, http)
        assert len(fetched) == 4  # nothing due yet
        for feed in feeds.FEEDS:  # pretend the last poll was 3 hours ago
            db.set_state(conn, f"feed_polled:{feed.name}", "2026-01-01T00:00:00+00:00")
        db.set_state(conn, f"feed_polled:{SIMPLIFY_INTERN.name}", datetime.now(UTC).isoformat())
        await feeds.poll(conn, http)
        assert len(fetched) == 7  # Simplify intern was polled just now, so it's skipped


async def test_feed_only_cycle_skips_board_polling(conn, settings, monkeypatch):
    add_company(conn)  # an ATS board that must not be fetched on a feed-only tick
    polled_boards: list[str] = []
    feed_polls: list[bool] = []

    async def fake_poll_company(conn, http, company):
        polled_boards.append(company["name"])

    async def fake_feeds(conn, http):
        feed_polls.append(True)

    monkeypatch.setattr(poll, "poll_company", fake_poll_company)
    monkeypatch.setattr(feeds, "poll", fake_feeds)
    monkeypatch.setattr(poll, "make_llm", lambda settings: None)
    monkeypatch.setattr(poll, "REQUEST_GAP_SECONDS", 0)
    feeds_on = settings.model_copy(update={"feeds_enabled": True})

    await poll.discover_once(feeds_on, boards=False)
    assert polled_boards == [] and feed_polls == [True]
    await poll.discover_once(feeds_on)
    assert polled_boards == ["Acme"] and len(feed_polls) == 2


def test_feed_terms_reach_the_rules(conn):
    """A new-grad title without an early-career word passes because the feed vouches."""
    company_id = add_company(conn, name="Acme Feed", ats_type="feed", board_token="")
    store.upsert_postings(
        conn,
        company_id,
        [Posting(title="Data Scientist I", location="Austin, TX", url="u", terms="New Grad")],
        "2026-10-14T00:00:00Z",
        feed="simplify-newgrad",
    )
    assert store.run_rules(conn) == (1, 0)


def test_posting_model_defaults_to_no_feed_signals():
    posting = Posting(title="t", url="u")
    assert posting.visa_sponsor is False and posting.terms == ""


def test_new_grad_list_bank_internships_are_tagged_internships():
    markdown = "\n".join(
        [HEADER, _row("JPMorgan Chase", "2027 ML CoE - Summer Associate", "NY", "", "wd-9")]
    )
    assert [p.terms for _, p in feeds.parse_zapply(markdown, new_grad=True)] == ["Internship"]
