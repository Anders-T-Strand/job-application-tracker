import httpx
import pytest

from app.discovery.ats import fetch_board, strip_html

GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 4011,
            "title": "Machine Learning Intern",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4011",
            "location": {"name": "New York, NY"},
            # Greenhouse escapes the HTML content
            "content": "&lt;p&gt;Build &amp;amp; ship ML models.&lt;/p&gt;",
        },
        {"id": 4012, "title": "", "absolute_url": "https://x", "content": ""},  # skipped
    ]
}

LEVER_PAYLOAD = [
    {
        "id": "lever-1",
        "text": "Data Science Intern",
        "hostedUrl": "https://jobs.lever.co/acme/lever-1",
        "categories": {"location": "San Francisco, CA"},
        "workplaceType": "remote",
        "descriptionPlain": "Analyze data.",
        "additionalPlain": "We cannot offer visa sponsorship.",
    }
]

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "id": "ashby-1",
            "title": "AI Research Intern",
            "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-1",
            "location": "Remote",
            "secondaryLocations": [{"location": "Austin, TX"}],
            "isRemote": True,
            "descriptionHtml": "<p>Do research.</p><p>Publish papers.</p>",
        },
        {"id": "ashby-2", "title": "Hidden Role", "jobUrl": "https://x", "isListed": False},
    ]
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_strip_html_blocks_and_entities():
    text = strip_html("<div><h2>Role</h2><ul><li>Build &amp; ship</li><li>Learn</li></ul></div>")
    assert "Role" in text and "Build & ship" in text
    assert "<" not in text
    assert "\n\n" not in text  # blank runs collapsed


async def test_greenhouse_normalization():
    async with _client(lambda req: httpx.Response(200, json=GREENHOUSE_PAYLOAD)) as http:
        postings, _ = await fetch_board(http, "greenhouse", "acme", None)
    assert len(postings) == 1  # titleless job dropped
    posting = postings[0]
    assert posting.external_id == "4011"
    assert posting.location == "New York, NY"
    assert posting.description == "Build & ship ML models."  # double-unescaped, tags stripped
    assert not posting.remote


async def test_lever_normalization_appends_additional():
    async with _client(lambda req: httpx.Response(200, json=LEVER_PAYLOAD)) as http:
        postings, _ = await fetch_board(http, "lever", "acme", None)
    posting = postings[0]
    assert posting.title == "Data Science Intern"
    assert posting.remote  # workplaceType, not location text
    assert "Analyze data." in posting.description
    assert "visa sponsorship" in posting.description  # additionalPlain carries the visa boilerplate


async def test_ashby_normalization_merges_locations_and_skips_unlisted():
    async with _client(lambda req: httpx.Response(200, json=ASHBY_PAYLOAD)) as http:
        postings, _ = await fetch_board(http, "ashby", "acme", None)
    assert len(postings) == 1
    posting = postings[0]
    assert posting.location == "Remote; Austin, TX"
    assert posting.remote
    assert posting.description == "Do research.\nPublish papers."


async def test_fetch_board_etag_roundtrip():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"jobs": []}, headers={"ETag": '"v1"'})

    async with _client(handler) as http:
        postings, etag = await fetch_board(http, "greenhouse", "acme", None)
        assert postings == [] and etag == '"v1"'
        postings, etag = await fetch_board(http, "greenhouse", "acme", '"v1"')
        assert postings is None and etag == '"v1"'  # 304: caller skips the company


async def test_fetch_board_http_error_raises():
    async with _client(lambda req: httpx.Response(500)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_board(http, "lever", "acme", None)


async def test_fetch_board_oversized_response_raises(monkeypatch):
    # a board bigger than the cap must raise instead of being parsed (OOM guard)
    monkeypatch.setattr("app.discovery.ats.MAX_BOARD_BYTES", 1024)
    big = httpx.Response(200, content=b'{"jobs": [' + b" " * 2048 + b"]}")
    async with _client(lambda req: big) as http:
        with pytest.raises(ValueError, match="exceeds"):
            await fetch_board(http, "greenhouse", "anduril", None)
