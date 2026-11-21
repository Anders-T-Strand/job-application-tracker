"""Match classified emails to applications and apply status updates."""

import sqlite3
from datetime import UTC, datetime

from rapidfuzz import fuzz, utils

from app.classifier.prefilter import is_ats_domain, sender_domain
from app.models import OPEN_STATUSES, STATUS_FOR_TYPE, EmailClassification

MATCH_THRESHOLD = 86
AMBIGUITY_MARGIN = 5
LOW_CONFIDENCE = 0.6
DOMAIN_AGREEMENT_MIN = 50  # an employer-domain boost needs the names to roughly agree
ROLE_MATCH = 90  # role titles this similar are the same job ("Full-Stack ..." = "Full Stack ...")
# A confirmation within this many days of a role-less email (a security code) or of an
# "Applied" click on Discover belongs to that application, however its role is worded.
# Later, a new role is a new application.
SAME_APPLICATION_DAYS = 7


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _score(app_row: sqlite3.Row, company: str | None, domain: str) -> float:
    fuzzy = 0.0
    if company and app_row["company"]:
        fuzzy = fuzz.token_set_ratio(company.lower(), app_row["company"].lower())
    score = fuzzy
    # The same sender domain corroborates a match. ATS domains (myworkday.com,
    # greenhouse-mail.io, ...) send for thousands of employers, so they only count when the
    # email names no company: one company's confirmation is not another's just because both
    # came via Greenhouse, even when unrelated names share enough letters to score over 50.
    domain_agrees = company is None or (not is_ats_domain(domain) and fuzzy >= DOMAIN_AGREEMENT_MIN)
    if domain and app_row["ats_domain"] and domain == app_row["ats_domain"] and domain_agrees:
        score = max(score, 90.0)
    return score


def find_candidates(
    conn: sqlite3.Connection, company: str | None, domain: str
) -> list[sqlite3.Row]:
    """The applications the email's company matches best, newest first; empty if none is
    close enough. Several means several applications at the same company."""
    scored = [
        (_score(row, company, domain), row) for row in conn.execute("SELECT * FROM applications")
    ]
    best = max((score for score, _ in scored), default=0.0)
    if best < MATCH_THRESHOLD:
        return []
    candidates = [
        row for score, row in scored if score >= MATCH_THRESHOLD and best - score < AMBIGUITY_MARGIN
    ]
    return sorted(candidates, key=lambda row: row["applied_at"], reverse=True)


def same_role(role: str | None, other: str | None) -> bool:
    return bool(role and other) and (
        fuzz.token_set_ratio(role, other, processor=utils.default_process) >= ROLE_MATCH
    )


def pick_application(
    candidates: list[sqlite3.Row], cls: EmailClassification, received_at: str
) -> tuple[sqlite3.Row | None, bool]:
    """Which of the company's applications an email is about: (application, ambiguous).
    A confirmation for a role none of them is for returns (None, False): a new application."""
    if cls.type == "application_confirmation":
        if not cls.role_title:  # security codes, "your application was sent": the latest
            return candidates[0], False
        for app_row in candidates:
            if same_role(cls.role_title, app_row["role_title"]):
                return app_row, False
        for app_row in candidates:
            loose = not app_row["role_title"] or app_row["source"] == "discovery"
            if loose and _days_between(app_row["applied_at"], received_at) <= SAME_APPLICATION_DAYS:
                return app_row, False
        return None, False
    if len(candidates) == 1:
        return candidates[0], False
    # several applications at the company: the one for this role, else the only open one
    for narrowed in (
        [row for row in candidates if same_role(cls.role_title, row["role_title"])],
        [row for row in candidates if row["status"] in OPEN_STATUSES],
    ):
        if len(narrowed) == 1:
            return narrowed[0], False
    return candidates[0], True


def _days_between(start: str, end: str) -> float:
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return abs(delta.total_seconds()) / 86400


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
    match, ambiguous = None, False
    if candidates := find_candidates(conn, cls.company, domain):
        match, ambiguous = pick_application(candidates, cls, email["received_at"])
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
