"""Match classified emails to applications and apply status updates."""

import sqlite3
from datetime import UTC, datetime

from rapidfuzz import fuzz

from app.classifier.prefilter import sender_domain
from app.models import STATUS_FOR_TYPE, EmailClassification

MATCH_THRESHOLD = 86
AMBIGUITY_MARGIN = 5
LOW_CONFIDENCE = 0.6
DOMAIN_AGREEMENT_MIN = 50  # domain boost needs the company name to at least roughly agree


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _score(app_row: sqlite3.Row, company: str | None, domain: str) -> float:
    fuzzy = 0.0
    if company and app_row["company"]:
        fuzzy = fuzz.token_set_ratio(company.lower(), app_row["company"].lower())
    score = fuzzy
    # ATS domains (myworkday.com, greenhouse-mail.io, ...) are shared by many
    # employers: same domain corroborates a match but must never override a
    # conflicting company name (a Nike rejection is not a Micron rejection
    # just because both came via Workday).
    domain_agrees = company is None or fuzzy >= DOMAIN_AGREEMENT_MIN
    if domain and app_row["ats_domain"] and domain == app_row["ats_domain"] and domain_agrees:
        score = max(score, 90.0)
    return score


def find_match(
    conn: sqlite3.Connection, company: str | None, domain: str
) -> tuple[sqlite3.Row | None, bool]:
    """Best-matching application for (company, sender domain). Returns (match, ambiguous)."""
    scored = sorted(
        ((_score(row, company, domain), row) for row in conn.execute("SELECT * FROM applications")),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored or scored[0][0] < MATCH_THRESHOLD:
        return None, False
    if (
        len(scored) > 1
        and scored[1][0] >= MATCH_THRESHOLD
        and scored[0][0] - scored[1][0] < AMBIGUITY_MARGIN
    ):
        return scored[0][1], True
    return scored[0][1], False


def process_email(conn: sqlite3.Connection, email_id: int, cls: EmailClassification) -> None:
    """Store the classification and apply match/create/review rules. Commits."""
    email = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    conn.execute(
        "UPDATE emails SET classification = ?, confidence = ?, processed = 1 WHERE id = ?",
        (cls.model_dump_json(), cls.confidence, email_id),
    )
    if not cls.is_job_related or cls.type == "other":
        conn.commit()
        return
    if cls.type == "recruiter_outreach" or cls.confidence < LOW_CONFIDENCE:
        # outreach never auto-creates an application; low confidence goes to a human
        _flag_review(conn, email_id)
        return

    domain = sender_domain(email["from_addr"])
    match, ambiguous = find_match(conn, cls.company, domain)
    if ambiguous:
        _flag_review(conn, email_id)
        return
    if match is not None:
        update_from_email(conn, match, cls, email_id)
        conn.execute("UPDATE emails SET application_id = ? WHERE id = ?", (match["id"], email_id))
        conn.commit()
        return
    if cls.type == "application_confirmation" and cls.company:
        app_id = create_application(conn, cls, domain, applied_at=email["received_at"])
        conn.execute("UPDATE emails SET application_id = ? WHERE id = ?", (app_id, email_id))
        conn.commit()
        return
    # a rejection/OA/interview/offer with no matching application, or no company name
    _flag_review(conn, email_id)


def _flag_review(conn: sqlite3.Connection, email_id: int) -> None:
    conn.execute("UPDATE emails SET needs_review = 1 WHERE id = ?", (email_id,))
    conn.commit()


def create_application(
    conn: sqlite3.Connection,
    cls: EmailClassification,
    domain: str,
    applied_at: str | None,
) -> int:
    now = utcnow()
    cursor = conn.execute(
        "INSERT INTO applications"
        " (company, role_title, source, ats_domain, applied_at, status, last_activity_at)"
        " VALUES (?, ?, 'email', ?, ?, 'applied', ?)",
        (cls.company, cls.role_title, domain or None, applied_at or now, now),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def update_from_email(
    conn: sqlite3.Connection, app_row: sqlite3.Row, cls: EmailClassification, email_id: int
) -> None:
    now = utcnow()
    updates: dict[str, str | None] = {
        "last_activity_at": now,
        "hidden_at": None,
    }  # activity un-hides
    if cls.role_title and not app_row["role_title"]:
        updates["role_title"] = cls.role_title
    new_status = STATUS_FOR_TYPE.get(cls.type)
    if new_status and new_status != app_row["status"]:
        conn.execute(
            "INSERT INTO status_history"
            " (application_id, old_status, new_status, changed_at, triggered_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (app_row["id"], app_row["status"], new_status, now, f"email:{email_id}"),
        )
        updates["status"] = new_status
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(
        f"UPDATE applications SET {set_clause} WHERE id = ?",  # noqa: S608 — keys are literals
        (*updates.values(), app_row["id"]),
    )


def record_manual_status(
    conn: sqlite3.Connection, app_id: int, old_status: str, new_status: str
) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO status_history"
        " (application_id, old_status, new_status, changed_at, triggered_by)"
        " VALUES (?, ?, ?, ?, 'manual')",
        (app_id, old_status, new_status, now),
    )
    conn.execute(
        "UPDATE applications SET status = ?, last_activity_at = ?, hidden_at = NULL WHERE id = ?",
        (new_status, now, app_id),
    )
