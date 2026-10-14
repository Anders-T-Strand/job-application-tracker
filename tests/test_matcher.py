from app import matcher
from app.models import EmailClassification
from tests.helpers import add_application, add_email


def cls(**overrides) -> EmailClassification:
    base = {
        "is_job_related": True,
        "type": "application_confirmation",
        "company": "Acme",
        "role_title": None,
        "confidence": 0.9,
    }
    return EmailClassification(**{**base, **overrides})


def get_email(conn, email_id):
    return conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()


def all_apps(conn):
    return conn.execute("SELECT * FROM applications").fetchall()


def test_confirmation_without_match_creates_application(conn):
    email_id = add_email(conn)
    matcher.process_email(conn, email_id, cls(role_title="Backend Engineer"))
    apps = all_apps(conn)
    assert len(apps) == 1
    assert apps[0]["company"] == "Acme"
    assert apps[0]["status"] == "applied"
    assert apps[0]["ats_domain"] == "greenhouse.io"
    assert apps[0]["applied_at"] == "2026-07-01T10:00:00Z"
    assert get_email(conn, email_id)["application_id"] == apps[0]["id"]


def test_duplicate_confirmation_does_not_duplicate_application(conn):
    add_application(conn, company="Acme")
    email_id = add_email(conn)
    matcher.process_email(conn, email_id, cls())
    assert len(all_apps(conn)) == 1


def test_rejection_fuzzy_matches_and_records_history(conn):
    app_id = add_application(conn, company="Acme Inc")
    email_id = add_email(conn, subject="Your application at Acme")
    matcher.process_email(conn, email_id, cls(type="rejection", company="Acme"))
    app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert app_row["status"] == "rejected"
    history = conn.execute("SELECT * FROM status_history").fetchall()
    assert len(history) == 1
    assert history[0]["old_status"] == "applied"
    assert history[0]["new_status"] == "rejected"
    assert history[0]["triggered_by"] == f"email:{email_id}"


def test_domain_only_match_when_company_unknown(conn):
    app_id = add_application(conn, company="Acme", ats_domain="greenhouse.io")
    email_id = add_email(conn, subject="Invitation to online assessment")
    matcher.process_email(conn, email_id, cls(type="oa_invite", company=None))
    app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert app_row["status"] == "oa"
    assert get_email(conn, email_id)["needs_review"] == 0


def test_shared_ats_domain_does_not_override_conflicting_company(conn):
    # Micron and Nike both hire via Workday; a Nike rejection arriving from
    # @myworkday.com must not attach to the Micron application.
    micron_id = add_application(conn, company="Micron", ats_domain="myworkday.com")
    email_id = add_email(
        conn, from_addr="nike@myworkday.com", subject="Update on your Nike application"
    )
    matcher.process_email(conn, email_id, cls(type="rejection", company="Nike"))
    micron = conn.execute("SELECT * FROM applications WHERE id = ?", (micron_id,)).fetchone()
    assert micron["status"] == "applied"  # untouched
    email = get_email(conn, email_id)
    assert email["application_id"] is None
    assert email["needs_review"] == 1  # no Nike application exists -> human decides


def test_rejection_without_match_goes_to_review(conn):
    email_id = add_email(conn)
    matcher.process_email(conn, email_id, cls(type="rejection", company="Unknown Co"))
    assert all_apps(conn) == []
    assert get_email(conn, email_id)["needs_review"] == 1


def test_ambiguous_match_goes_to_review(conn):
    add_application(conn, company="Acme")
    add_application(conn, company="Acme Corp")
    email_id = add_email(conn)
    matcher.process_email(conn, email_id, cls(type="rejection"))
    assert get_email(conn, email_id)["needs_review"] == 1
    for app_row in all_apps(conn):
        assert app_row["status"] == "applied"  # nothing auto-updated


def test_recruiter_outreach_never_creates_application(conn):
    email_id = add_email(conn, from_addr="jane@agency.com")
    matcher.process_email(conn, email_id, cls(type="recruiter_outreach", company="Fintech Co"))
    assert all_apps(conn) == []
    assert get_email(conn, email_id)["needs_review"] == 1


def test_low_confidence_goes_to_review(conn):
    add_application(conn, company="Acme")
    email_id = add_email(conn)
    matcher.process_email(conn, email_id, cls(type="rejection", confidence=0.4))
    assert get_email(conn, email_id)["needs_review"] == 1


def test_not_job_related_is_stored_and_ignored(conn):
    email_id = add_email(conn, from_addr="deals@webshop.com", subject="Sale!")
    matcher.process_email(conn, email_id, cls(is_job_related=False, type="other", company=None))
    email = get_email(conn, email_id)
    assert email["processed"] == 1
    assert email["needs_review"] == 0
    assert all_apps(conn) == []


def test_confirmation_fills_missing_role_title(conn):
    app_id = add_application(conn, company="Acme", role_title=None)
    email_id = add_email(conn)
    matcher.process_email(conn, email_id, cls(role_title="Platform Engineer"))
    app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert app_row["role_title"] == "Platform Engineer"
    assert app_row["status"] == "applied"  # confirmations never change status
