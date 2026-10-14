import httpx

from app.discovery.poll import poll_company
from tests.helpers import add_company

PAYLOAD = {
    "jobs": [
        {
            "id": 1,
            "title": "Machine Learning Intern",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "New York, NY"},
            "content": "Machine learning internship.",
        }
    ]
}


def _company_row(conn, company_id):
    return conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()


async def test_poll_company_stores_postings_and_etag(conn):
    company_id = add_company(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=PAYLOAD, headers={"ETag": '"v1"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await poll_company(conn, http, _company_row(conn, company_id))
        company = _company_row(conn, company_id)
        assert company["etag"] == '"v1"'
        assert company["last_polled"] is not None

        posting = conn.execute("SELECT * FROM postings").fetchone()
        assert posting["title"] == "Machine Learning Intern"
        assert posting["is_live"] == 1

        # second poll gets a 304: postings untouched, last_polled still refreshed
        conn.execute("UPDATE companies SET last_polled = NULL WHERE id = ?", (company_id,))
        await poll_company(conn, http, _company_row(conn, company_id))
        assert _company_row(conn, company_id)["last_polled"] is not None
        assert conn.execute("SELECT is_live FROM postings").fetchone()["is_live"] == 1


async def test_poll_company_fetch_failure_leaves_state_alone(conn):
    company_id = add_company(conn)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    ) as http:
        await poll_company(conn, http, _company_row(conn, company_id))
    assert _company_row(conn, company_id)["last_polled"] is None
    assert conn.execute("SELECT COUNT(*) AS c FROM postings").fetchone()["c"] == 0
