from datetime import UTC, datetime, timedelta

from app import matcher
from app.lifecycle import apply_rules
from app.models import EmailClassification
from tests.helpers import add_application, add_email

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entered(conn, app_id: int, old: str, new: str, days: int) -> None:
    conn.execute(
        "INSERT INTO status_history"
        " (application_id, old_status, new_status, changed_at, triggered_by)"
        " VALUES (?, ?, ?, ?, 'email:1')",
        (app_id, old, new, _ago(days)),
    )
    conn.commit()


def _status(conn, app_id: int) -> str:
    return conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()[0]


def _hidden(conn, app_id: int) -> str | None:
    return conn.execute("SELECT hidden_at FROM applications WHERE id = ?", (app_id,)).fetchone()[0]


def test_silent_open_applications_are_ghosted(conn):
    silent = add_application(conn, company="Silent", last_activity_at=_ago(61))
    interviewing = add_application(
        conn, company="Quiet", status="interview", last_activity_at=_ago(75)
    )
    recent = add_application(conn, company="Recent", last_activity_at=_ago(59))
    offer = add_application(conn, company="Offer", status="offer", last_activity_at=_ago(200))

    assert apply_rules(conn, NOW) == (2, 0, 0)
    assert _status(conn, silent) == "ghosted"
    assert _status(conn, interviewing) == "ghosted"
    assert _status(conn, recent) == "applied"
    assert _status(conn, offer) == "offer"  # an offer is never auto-ghosted

    history = conn.execute(
        "SELECT old_status, new_status, changed_at, triggered_by FROM status_history"
        " WHERE application_id = ?",
        (silent,),
    ).fetchone()
    assert tuple(history) == ("applied", "ghosted", _ago(0), "auto")
    # last activity still reflects the real last contact, not the automatic move
    assert conn.execute(
        "SELECT last_activity_at FROM applications WHERE id = ?", (silent,)
    ).fetchone()[0] == _ago(61)


def test_ghosted_for_90_days_becomes_rejected(conn):
    old = add_application(conn, company="Old", status="ghosted", last_activity_at=_ago(200))
    _entered(conn, old, "applied", "ghosted", days=91)
    young = add_application(conn, company="Young", status="ghosted", last_activity_at=_ago(200))
    _entered(conn, young, "applied", "ghosted", days=89)
    revived = add_application(conn, company="Revived", status="ghosted", last_activity_at=_ago(5))
    _entered(conn, revived, "applied", "ghosted", days=120)  # but there was activity since

    assert apply_rules(conn, NOW) == (0, 1, 0)
    assert _status(conn, old) == "rejected"
    assert _status(conn, young) == "ghosted"
    assert _status(conn, revived) == "ghosted"


def test_rejected_for_60_days_is_hidden_not_deleted(conn):
    stale = add_application(conn, company="Stale", status="rejected", last_activity_at=_ago(61))
    _entered(conn, stale, "applied", "rejected", days=61)
    fresh = add_application(conn, company="Fresh", status="rejected", last_activity_at=_ago(30))

    assert apply_rules(conn, NOW) == (0, 0, 1)
    assert _status(conn, stale) == "rejected"  # still there, history intact, for stats
    assert _hidden(conn, stale) == _ago(0)
    assert _hidden(conn, fresh) is None
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE application_id = ?", (stale,)
        ).fetchone()[0]
        == 1
    )
    assert apply_rules(conn, NOW + timedelta(days=365)) == (0, 0, 1)  # only "fresh" is new


def test_new_activity_unhides(conn):
    app_id = add_application(conn, company="Acme", status="rejected", last_activity_at=_ago(70))
    assert apply_rules(conn, NOW) == (0, 0, 1)

    email_id = add_email(conn)
    cls = EmailClassification(
        is_job_related=True, type="interview_invite", company="Acme", confidence=0.9
    )
    matcher.update_from_email(
        conn,
        conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone(),
        cls,
        email_id,
    )
    assert _hidden(conn, app_id) is None
    assert _status(conn, app_id) == "interview"

    conn.execute("UPDATE applications SET hidden_at = ? WHERE id = ?", (_ago(1), app_id))
    matcher.record_manual_status(conn, app_id, "interview", "applied")
    assert _hidden(conn, app_id) is None


def test_one_step_per_run(conn):
    """A long-silent application is ghosted first; it isn't rejected or hidden the same day."""
    ancient = add_application(conn, company="Ancient", last_activity_at=_ago(400))
    assert apply_rules(conn, NOW) == (1, 0, 0)
    assert apply_rules(conn, NOW) == (0, 0, 0)
    assert apply_rules(conn, NOW + timedelta(days=91)) == (0, 1, 0)
    assert apply_rules(conn, NOW + timedelta(days=91 + 61)) == (0, 0, 1)
    assert _status(conn, ancient) == "rejected" and _hidden(conn, ancient) is not None
