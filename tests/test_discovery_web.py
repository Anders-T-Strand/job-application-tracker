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
    assert "sponsors visas" in page and "via Zapply" in page
    assert "github.com/zapplyjobs" in page and "github.com/SimplifyJobs" in page  # credits
    assert "1 tracked company board" in page  # feed companies aren't boards

    filtered = client.get("/discover", params={"sponsor": 1}).text
    assert "Vision ML Intern" in filtered and "Unknown Visa Intern" not in filtered

    response = client.post(
        f"/discover/{sponsor}/save", params={"sponsor": 1, "term": "all"}, follow_redirects=False
    )
    location = response.headers["location"]
    assert "sponsor=1" in location and "term=all" in location  # filters survive actions


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
    assert "ML Co-op - Spring 2027" in page and "Winter/Spring &#39;27" in page
    assert "Machine Learning Engineer, New Grad" in page and "New grad" in page
    assert "ML Intern - Summer 2027" not in page and ">ML Intern<" not in page
    assert '<span class="badge">2</span>' in page  # the nav badge counts the default view

    everything = client.get("/discover", params={"term": "all"}).text
    assert "ML Intern - Summer 2027" in everything and "Summer &#39;27+" in everything
    assert ">ML Intern<" in everything
