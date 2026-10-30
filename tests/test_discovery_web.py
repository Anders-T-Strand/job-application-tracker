import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from tests.helpers import add_company, add_posting


@pytest.fixture
def web(settings):
    application = create_app(settings)
    with TestClient(application) as client:  # lifespan runs migrations; pollers disabled
        yield client, settings


def _seed(settings, **posting_kwargs) -> int:
    conn = db.connect(settings.db_path)
    company_id = add_company(conn, lca_filings=120)
    posting_id = add_posting(conn, company_id, **posting_kwargs)
    conn.close()
    return posting_id


def test_discover_renders_empty(web):
    client, _ = web
    response = client.get("/discover")
    assert response.status_code == 200
    assert "Nothing new" in response.text


def test_stylesheet_url_is_versioned_by_content(web):
    client, _ = web
    page = client.get("/discover").text
    href = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{8})"', page)
    assert href, "a changed stylesheet needs a new URL, or browsers keep the cached one"
    assert client.get(href.group(1)).status_code == 200


def test_discover_lists_scored_postings(web):
    client, settings = web
    _seed(settings, llm_score=88)
    response = client.get("/discover", params={"term": "all"})  # no season in the title
    assert "Machine Learning Intern" in response.text
    assert "88" in response.text
    assert "120 H-1B" in response.text  # LCA soft signal surfaced


def test_new_view_hides_rejected_and_dead_but_saved_view_keeps_saved(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    company_id = add_company(conn)
    add_posting(conn, company_id, title="Rejected Intern", rules_status="rejected")
    add_posting(conn, company_id, title="Dead Intern", is_live=0)
    add_posting(conn, company_id, title="Saved Gone Intern", status="saved", is_live=0)
    conn.close()

    new_view = client.get("/discover").text
    assert "Rejected Intern" not in new_view
    assert "Dead Intern" not in new_view

    saved_view = client.get("/discover", params={"view": "saved"}).text
    assert "Saved Gone Intern" in saved_view
    assert "no longer listed" in saved_view


def test_save_dismiss_restore_cycle(web):
    client, settings = web
    posting_id = _seed(settings)

    assert client.post(f"/discover/{posting_id}/save", follow_redirects=False).status_code == 303
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT status FROM postings").fetchone()["status"] == "saved"
    conn.close()

    client.post(f"/discover/{posting_id}/dismiss", follow_redirects=False)
    client.post(f"/discover/{posting_id}/restore", follow_redirects=False)
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT status FROM postings").fetchone()["status"] == "new"
    conn.close()


def test_applied_handoff_creates_linked_application(web):
    client, settings = web
    posting_id = _seed(settings)

    response = client.post(f"/discover/{posting_id}/applied", follow_redirects=False)
    assert response.status_code == 303

    conn = db.connect(settings.db_path)
    app_row = conn.execute("SELECT * FROM applications").fetchone()
    posting = conn.execute("SELECT * FROM postings").fetchone()
    conn.close()
    assert app_row["company"] == "Acme"
    assert app_row["role_title"] == "Machine Learning Intern"
    assert app_row["source"] == "discovery"
    assert posting["url"] in app_row["notes"]
    assert posting["status"] == "applied"
    assert posting["application_id"] == app_row["id"]

    # idempotent: a second click must not create a duplicate application
    client.post(f"/discover/{posting_id}/applied", follow_redirects=False)
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) AS c FROM applications").fetchone()["c"] == 1
    conn.close()

    # the applied posting shows up on the applied tab with a link to the application
    applied_view = client.get("/discover", params={"view": "applied"}).text
    assert "tracked application" in applied_view


def test_sponsor_filter_badge_and_feed_credits(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    kla = add_company(conn, name="KLA", ats_type="feed", board_token="")
    sponsor = add_posting(conn, kla, title="Vision ML Intern - Spring 2027", llm_score=80)
    conn.execute(
        "UPDATE postings SET visa_sponsor = 1, feed = 'zapply-intern' WHERE id = ?", (sponsor,)
    )
    add_posting(conn, add_company(conn), title="Unknown Visa Intern - Spring 2027", llm_score=70)
    conn.commit()
    conn.close()

    page = client.get("/discover").text
    assert "Vision ML Intern" in page and "Unknown Visa Intern" in page
    assert ">visa-friendly</span>" in page and "via Zapply" in page
    assert "github.com/zapplyjobs" in page and "github.com/SimplifyJobs" in page  # credits
    assert "1 tracked company board" in page  # feed companies aren't boards

    filtered = client.get("/discover", params={"visa": 1}).text
    assert "Vision ML Intern" in filtered and "Unknown Visa Intern" not in filtered

    response = client.post(
        f"/discover/{sponsor}/save",
        params=[("visa", 1), ("term", "all"), ("kind", "intern"), ("kind", "co-op")],
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert "visa=1" in location and "term=all" in location  # filters survive actions
    assert "kind=intern&kind=co-op" in location  # ...including every selected value


@pytest.fixture
def web_graduating(settings):
    graduating = settings.model_copy(update={"graduation_year": 2027})
    with TestClient(create_app(graduating)) as client:
        yield client, graduating


def test_default_view_shows_final_term_and_new_grad_only(web_graduating):
    client, settings = web_graduating
    conn = db.connect(settings.db_path)
    company_id = add_company(conn)
    add_posting(conn, company_id, title="ML Co-op - Spring 2027", llm_score=70)
    add_posting(conn, company_id, title="Machine Learning Engineer, New Grad", llm_score=70)
    add_posting(conn, company_id, title="ML Intern - Summer 2027", llm_score=90)
    add_posting(conn, company_id, title="ML Intern", llm_score=90)
    conn.close()

    page = client.get("/discover").text
    assert "ML Co-op - Spring 2027" in page and '"term">Winter/Spring &#39;27<' in page
    assert "Machine Learning Engineer, New Grad" in page and '"term">New grad<' in page
    assert "ML Intern - Summer 2027" not in page and ">ML Intern<" not in page
    assert '<span class="badge">2</span>' in page  # the nav badge counts the default view
    assert 'value="final_term" checked' in page and 'value="later" >' in page

    everything = client.get("/discover", params={"term": "all"}).text
    assert "ML Intern - Summer 2027" in everything and '"term">Summer &#39;27+<' in everything
    assert ">ML Intern<" in everything


def test_filters_combine_any_within_a_row_and_all_across_rows(web_graduating):
    client, settings = web_graduating
    conn = db.connect(settings.db_path)
    company_id = add_company(conn)
    add_posting(conn, company_id, title="ML Co-op - Spring 2027", llm_score=70)
    add_posting(conn, company_id, title="ML Intern - Summer 2027", llm_score=70)
    add_posting(conn, company_id, title="ML Summer Associate 2027", llm_score=70)
    add_posting(conn, company_id, title="Associate Data Scientist, New Grad", llm_score=70)
    add_posting(
        conn,
        company_id,
        title="Data Scientist, New Grad",
        description="Build ML models. Visa sponsorship is available.",
        llm_score=70,
    )
    titles = [row["title"] for row in conn.execute("SELECT title FROM postings")]
    conn.close()

    def shown(**params):
        page = client.get("/discover", params=params).text
        return {title for title in titles if f">{title}<" in page}

    assert shown(term=["all", "later"]) == {"ML Intern - Summer 2027", "ML Summer Associate 2027"}
    assert shown(term=["all"], kind=["co-op", "associate"]) == {
        "ML Co-op - Spring 2027",
        "ML Summer Associate 2027",
        "Associate Data Scientist, New Grad",
    }
    assert shown(term=["all", "new_grad"], kind=["associate"]) == {
        "Associate Data Scientist, New Grad"
    }
    # the posting's own sponsorship statement counts as visa-friendly, whatever the term
    assert shown(term=["all", "new_grad"], visa=1) == {"Data Scientist, New Grad"}
    assert shown(term=["all", "later"], visa=1) == set()
    assert shown(term=["all"], search="summer") == {
        "ML Intern - Summer 2027",
        "ML Summer Associate 2027",
    }
    assert (
        "matches these filters"
        in client.get("/discover", params={"term": ["all", "later"], "visa": 1}).text
    )


def test_low_scores_are_hidden_from_the_new_view_until_asked(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    company_id = add_company(conn)
    add_posting(conn, company_id, title="Good Fit Intern", llm_score=72)
    add_posting(conn, company_id, title="Poor Fit Intern", llm_score=20)
    add_posting(conn, company_id, title="Unscored Intern")
    conn.close()

    page = client.get("/discover").text
    assert "Good Fit Intern" in page and "Unscored Intern" in page
    assert "Poor Fit Intern" not in page
    assert "1 posting scored below 50 is hidden" in page
    assert '<span class="badge">2</span>' in page  # nav badge matches what's shown

    shown = client.get("/discover", params={"low": 1}).text
    assert "Poor Fit Intern" in shown and "Hide postings scored below 50" in shown
