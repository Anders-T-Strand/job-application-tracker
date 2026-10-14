import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.models import JobPosting
from tests.helpers import add_application, add_email


@pytest.fixture
def web(settings):
    application = create_app(settings)
    with TestClient(application) as client:  # lifespan runs migrations; poller disabled
        yield client, settings


def test_pages_render_when_empty(web):
    client, _ = web
    for path in ("/", "/table", "/stats", "/review", "/applications/new"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_manual_add_edit_and_history(web):
    client, settings = web
    response = client.post(
        "/applications",
        data={
            "company": "Acme",
            "role_title": "Backend Engineer",
            "status": "applied",
            "applied_date": "2026-07-01",
            "ats_domain": "greenhouse.io",
            "notes": "referred by a friend",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    board = client.get("/")
    assert "Acme" in board.text

    conn = db.connect(settings.db_path)
    app_id = conn.execute("SELECT id FROM applications").fetchone()["id"]
    conn.close()

    # status change through the edit form records history with a manual trigger
    response = client.post(
        f"/applications/{app_id}",
        data={
            "company": "Acme",
            "role_title": "Backend Engineer",
            "status": "interview",
            "applied_date": "2026-07-01",
            "ats_domain": "greenhouse.io",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = client.get(f"/applications/{app_id}")
    assert "applied → interview" in detail.text
    assert "manual" in detail.text


def test_quick_log_prefills_from_extraction(web, monkeypatch):
    async def fake_extract(llm, url, title):
        return JobPosting(company="Acme", role_title="ML Engineer")

    monkeypatch.setattr("app.web.routes.extract_job_posting", fake_extract)
    client, _ = web
    response = client.get(
        "/log",
        params={"url": "https://jobs.acme.com/ml-eng", "title": "ML Engineer at Acme"},
    )
    assert response.status_code == 200
    assert 'value="Acme"' in response.text
    assert 'value="ML Engineer"' in response.text
    assert "https://jobs.acme.com/ml-eng" in response.text  # posting URL lands in notes


def test_quick_log_without_api_key_still_renders(settings):
    no_key = settings.model_copy(update={"anthropic_api_key": ""})
    with TestClient(create_app(no_key)) as client:
        response = client.get("/log", params={"url": "https://jobs.acme.com/x", "title": "T"})
        assert response.status_code == 200
        assert "https://jobs.acme.com/x" in response.text


def test_review_dismiss_flow(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    email_id = add_email(conn, subject="Mystery recruiting email")
    conn.execute("UPDATE emails SET needs_review = 1, processed = 1 WHERE id = ?", (email_id,))
    conn.commit()
    conn.close()

    review = client.get("/review")
    assert "Mystery recruiting email" in review.text

    response = client.post(f"/review/{email_id}/dismiss", follow_redirects=False)
    assert response.status_code == 303
    assert "Mystery recruiting email" not in client.get("/review").text


def test_review_create_builds_application_from_email(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    email_id = add_email(conn, subject="We regret to inform you")
    conn.execute(
        "UPDATE emails SET needs_review = 1, processed = 1, classification = ? WHERE id = ?",
        (
            '{"is_job_related": true, "type": "rejection", "company": null,'
            ' "role_title": null, "confidence": 0.5}',
            email_id,
        ),
    )
    conn.commit()
    conn.close()

    response = client.post(
        f"/review/{email_id}/create",
        data={"company": "Mystery Co", "role_title": "Data Engineer"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    conn = db.connect(settings.db_path)
    app_row = conn.execute("SELECT * FROM applications").fetchone()
    email_row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    conn.close()
    assert app_row["company"] == "Mystery Co"
    assert app_row["status"] == "rejected"  # stored classification applied on create
    assert email_row["application_id"] == app_row["id"]
    assert email_row["needs_review"] == 0


def test_hidden_applications_leave_board_and_table_but_count_in_stats(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    add_application(conn, company="Visible Co", status="rejected")
    hidden = add_application(conn, company="Hidden Co", status="rejected")
    conn.execute(
        "UPDATE applications SET hidden_at = '2026-10-01T00:00:00Z' WHERE id = ?", (hidden,)
    )
    conn.commit()
    conn.close()

    assert "Hidden Co" not in client.get("/").text
    table = client.get("/table").text
    assert "Visible Co" in table and "Hidden Co" not in table
    assert "1 rejected 60+ days ago is hidden" in table
    assert "Hidden Co" in client.get("/table", params={"hidden": 1}).text
    assert "Hidden from the board since 2026-10-01" in client.get(f"/applications/{hidden}").text
    assert '<div class="num">2</div>' in client.get("/stats").text  # both still counted


def test_stats_ignore_automatic_lifecycle_moves(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    ghosted = add_application(conn, company="Silent", status="ghosted")
    conn.execute(
        "INSERT INTO status_history"
        " (application_id, old_status, new_status, changed_at, triggered_by)"
        " VALUES (?, 'applied', 'ghosted', '2026-09-01T00:00:00Z', 'auto')",
        (ghosted,),
    )
    conn.commit()
    conn.close()
    assert '<div class="num">0%</div>' in client.get("/stats").text  # ghosting isn't a response
