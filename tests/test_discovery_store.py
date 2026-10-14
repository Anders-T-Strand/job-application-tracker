from app.discovery.ats import Posting
from app.discovery.store import content_hash, mark_missing, run_rules, upsert_postings
from tests.helpers import add_company

INTERN = Posting(
    external_id="1",
    title="Machine Learning Intern",
    location="New York, NY",
    url="https://boards.greenhouse.io/acme/jobs/1",
    description="Build ML models on our machine learning team.",
)
SENIOR = Posting(
    external_id="2",
    title="Senior ML Intern",
    location="New York, NY",
    url="https://boards.greenhouse.io/acme/jobs/2",
    description="Lead our machine learning platform.",
)


def test_upsert_dedupes_on_title_and_location(conn):
    company_id = add_company(conn)
    assert upsert_postings(conn, company_id, [INTERN], "2026-07-01T00:00:00Z") == 1
    moved = INTERN.model_copy(update={"url": "https://boards.greenhouse.io/acme/jobs/9"})
    assert upsert_postings(conn, company_id, [moved], "2026-07-02T00:00:00Z") == 0

    rows = conn.execute("SELECT * FROM postings").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == "2026-07-01T00:00:00Z"  # preserved on re-see
    assert rows[0]["last_seen"] == "2026-07-02T00:00:00Z"
    assert rows[0]["url"].endswith("/jobs/9")  # mutable fields refreshed
    assert rows[0]["content_hash"] == content_hash(company_id, INTERN.title, INTERN.location)


def test_same_title_at_two_companies_is_not_a_dupe(conn):
    first, second = add_company(conn, name="Acme"), add_company(conn, name="Globex")
    upsert_postings(conn, first, [INTERN], "2026-07-01T00:00:00Z")
    upsert_postings(conn, second, [INTERN], "2026-07-01T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) AS c FROM postings").fetchone()["c"] == 2


def test_mark_missing_flips_is_live_and_reappearance_revives(conn):
    company_id = add_company(conn)
    upsert_postings(conn, company_id, [INTERN, SENIOR], "2026-07-01T00:00:00Z")

    # next poll: only the intern role remains
    upsert_postings(conn, company_id, [INTERN], "2026-07-02T00:00:00Z")
    assert mark_missing(conn, company_id, "2026-07-02T00:00:00Z") == 1
    live = {
        row["title"]: row["is_live"] for row in conn.execute("SELECT title, is_live FROM postings")
    }
    assert live == {"Machine Learning Intern": 1, "Senior ML Intern": 0}

    # the senior role comes back
    upsert_postings(conn, company_id, [INTERN, SENIOR], "2026-07-03T00:00:00Z")
    assert mark_missing(conn, company_id, "2026-07-03T00:00:00Z") == 0
    assert (
        conn.execute("SELECT is_live FROM postings WHERE title = 'Senior ML Intern'").fetchone()[
            "is_live"
        ]
        == 1
    )


def test_run_rules_records_status_and_reason(conn):
    company_id = add_company(conn)
    upsert_postings(conn, company_id, [INTERN, SENIOR], "2026-07-01T00:00:00Z")
    assert run_rules(conn) == (1, 1)
    rows = {row["title"]: row for row in conn.execute("SELECT * FROM postings")}
    assert rows["Machine Learning Intern"]["rules_status"] == "passed"
    assert rows["Machine Learning Intern"]["rules_reason"] is None
    assert rows["Senior ML Intern"]["rules_status"] == "rejected"
    assert rows["Senior ML Intern"]["rules_reason"] == "stage:senior-term-in-title"
    assert run_rules(conn) == (0, 0)  # nothing left pending
