"""Dashboard routes (server-rendered Jinja2 + htmx partials)."""

import json
import logging
import sqlite3
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, matcher
from app.classifier.llm import extract_job_posting
from app.llm import make_llm
from app.models import OPEN_STATUSES, STATUS_FOR_TYPE, STATUSES, EmailClassification

log = logging.getLogger(__name__)

EMPTY_PREFILL = {"company": "", "role_title": "", "notes": ""}

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

FOLLOW_UP_DAYS = 14
SORTABLE = {"company", "role_title", "status", "applied_at", "last_activity_at"}


def get_conn(request: Request) -> sqlite3.Connection:
    return db.connect(request.app.state.settings.db_path)


def _cutoff() -> str:
    return (datetime.now(UTC) - timedelta(days=FOLLOW_UP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _app_dict(row: sqlite3.Row, cutoff: str) -> dict:
    item = dict(row)
    item["stale"] = row["status"] in OPEN_STATUSES and row["last_activity_at"] < cutoff
    return item


def _review_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM emails WHERE needs_review = 1").fetchone()["c"]


def _board_context(conn: sqlite3.Connection) -> dict:
    cutoff = _cutoff()
    columns: dict[str, list[dict]] = {status: [] for status in STATUSES}
    for row in conn.execute(
        "SELECT * FROM applications WHERE hidden_at IS NULL ORDER BY last_activity_at DESC"
    ):
        columns.get(row["status"], columns["applied"]).append(_app_dict(row, cutoff))
    return {"statuses": STATUSES, "columns": columns, "review_count": _review_count(conn)}


# --- board ---------------------------------------------------------------


@router.get("/")
def board(request: Request):
    conn = get_conn(request)
    try:
        context = _board_context(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "board.html", context)


@router.get("/partials/board")
def board_partial(request: Request):
    conn = get_conn(request)
    try:
        context = _board_context(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "_board.html", context)


# --- table ---------------------------------------------------------------


@router.get("/table")
def table(
    request: Request, sort: str = "last_activity_at", order: str = "desc", hidden: bool = False
):
    sort = sort if sort in SORTABLE else "last_activity_at"
    direction = "ASC" if order == "asc" else "DESC"
    conn = get_conn(request)
    try:
        cutoff = _cutoff()
        apps = [
            _app_dict(row, cutoff)
            for row in conn.execute(
                f"SELECT * FROM applications {'' if hidden else 'WHERE hidden_at IS NULL'}"
                f" ORDER BY {sort} {direction}"  # noqa: S608
            )
        ]
        hidden_count = conn.execute(
            "SELECT COUNT(*) AS c FROM applications WHERE hidden_at IS NOT NULL"
        ).fetchone()["c"]
        context = {
            "apps": apps,
            "sort": sort,
            "order": "asc" if direction == "ASC" else "desc",
            "hidden": hidden,
            "hidden_count": hidden_count,
            "review_count": _review_count(conn),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "table.html", context)


# --- stats ---------------------------------------------------------------


@router.get("/stats")
def stats(request: Request):
    conn = get_conn(request)
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM applications").fetchone()["c"]
        status_counts = {
            row["status"]: row["c"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS c FROM applications GROUP BY status"
            )
        }
        responded = conn.execute(
            "SELECT COUNT(DISTINCT application_id) AS c FROM status_history"
            " WHERE triggered_by != 'auto'"  # lifecycle moves aren't employer responses
        ).fetchone()["c"]
        first_responses = conn.execute(
            "SELECT a.applied_at AS applied_at, MIN(h.changed_at) AS first_response"
            " FROM applications a JOIN status_history h"
            " ON h.application_id = a.id AND h.triggered_by != 'auto'"
            " GROUP BY a.id"
        ).fetchall()
        days: list[float] = []
        for row in first_responses:
            try:
                delta = datetime.fromisoformat(row["first_response"]) - datetime.fromisoformat(
                    row["applied_at"]
                )
            except (ValueError, TypeError):
                continue
            days.append(max(delta.total_seconds() / 86400, 0.0))
        weeks = conn.execute(
            "SELECT strftime('%Y-W%W', applied_at) AS week, COUNT(*) AS c"
            " FROM applications GROUP BY week ORDER BY week DESC LIMIT 8"
        ).fetchall()
        context = {
            "total": total,
            "statuses": STATUSES,
            "status_counts": status_counts,
            "response_rate": round(responded / total * 100) if total else 0,
            "median_days": round(statistics.median(days), 1) if days else None,
            "weeks": [dict(row) for row in weeks],
            "review_count": _review_count(conn),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "stats.html", context)


# --- review queue ----------------------------------------------------------


@router.get("/review")
def review(request: Request):
    conn = get_conn(request)
    try:
        emails = []
        for row in conn.execute(
            "SELECT * FROM emails WHERE needs_review = 1 ORDER BY received_at DESC"
        ):
            item = dict(row)
            item["cls"] = json.loads(row["classification"]) if row["classification"] else None
            emails.append(item)
        apps = conn.execute(
            "SELECT id, company, role_title FROM applications ORDER BY company"
        ).fetchall()
        context = {"emails": emails, "apps": apps, "review_count": len(emails)}
    finally:
        conn.close()
    return templates.TemplateResponse(request, "review.html", context)


def _stored_classification(row: sqlite3.Row) -> EmailClassification | None:
    if not row["classification"]:
        return None
    try:
        return EmailClassification.model_validate_json(row["classification"])
    except ValueError:
        return None


@router.post("/review/{email_id}/assign")
def review_assign(request: Request, email_id: int, application_id: int = Form(...)):
    conn = get_conn(request)
    try:
        email = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
        app_row = conn.execute(
            "SELECT * FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        if email and app_row:
            cls = _stored_classification(email)
            if cls:
                matcher.update_from_email(conn, app_row, cls, email_id)
            conn.execute(
                "UPDATE emails SET application_id = ?, needs_review = 0 WHERE id = ?",
                (application_id, email_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/review", status_code=303)


@router.post("/review/{email_id}/create")
def review_create(
    request: Request, email_id: int, company: str = Form(...), role_title: str = Form("")
):
    conn = get_conn(request)
    try:
        email = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
        if email and company.strip():
            stored = _stored_classification(email)
            cls = EmailClassification(
                is_job_related=True,
                type=stored.type if stored else "application_confirmation",
                company=company.strip(),
                role_title=role_title.strip() or None,
                confidence=1.0,
            )
            from app.classifier.prefilter import sender_domain

            app_id = matcher.create_application(
                conn, cls, sender_domain(email["from_addr"]), applied_at=email["received_at"]
            )
            if STATUS_FOR_TYPE.get(cls.type):
                app_row = conn.execute(
                    "SELECT * FROM applications WHERE id = ?", (app_id,)
                ).fetchone()
                matcher.update_from_email(conn, app_row, cls, email_id)
            conn.execute(
                "UPDATE emails SET application_id = ?, needs_review = 0 WHERE id = ?",
                (app_id, email_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/review", status_code=303)


@router.post("/review/{email_id}/dismiss")
def review_dismiss(request: Request, email_id: int):
    conn = get_conn(request)
    try:
        conn.execute("UPDATE emails SET needs_review = 0 WHERE id = ?", (email_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/review", status_code=303)


# --- applications (manual add / edit) ---------------------------------------


@router.get("/applications/new")
def application_new(request: Request):
    conn = get_conn(request)
    try:
        context = {
            "app_row": None,
            "prefill": EMPTY_PREFILL,
            "statuses": STATUSES,
            "review_count": _review_count(conn),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "app_form.html", context)


@router.get("/log")
async def quick_log(request: Request, url: str = "", title: str = ""):
    """Quick-log a job posting (bookmarklet / iPhone shortcut target).

    The LLM extracts company + role from the posting's URL and title; the user
    confirms in the normal add form. Nothing is saved without confirmation.
    """
    settings = request.app.state.settings
    prefill = {"company": "", "role_title": "", "notes": url.strip()}
    llm = make_llm(settings)
    if llm is not None and (url or title):
        try:
            posting = await extract_job_posting(llm, url, title)
            if posting:
                prefill["company"] = posting.company or ""
                prefill["role_title"] = posting.role_title or ""
        except Exception:
            log.warning("job posting extraction failed; serving empty form", exc_info=True)
    conn = get_conn(request)
    try:
        context = {
            "app_row": None,
            "prefill": prefill,
            "statuses": STATUSES,
            "review_count": _review_count(conn),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "app_form.html", context)


@router.post("/applications")
def application_create(
    request: Request,
    company: str = Form(...),
    role_title: str = Form(""),
    status: str = Form("applied"),
    applied_date: str = Form(""),
    ats_domain: str = Form(""),
    notes: str = Form(""),
):
    status = status if status in STATUSES else "applied"
    applied_at = f"{applied_date}T00:00:00Z" if applied_date else matcher.utcnow()
    conn = get_conn(request)
    try:
        conn.execute(
            "INSERT INTO applications"
            " (company, role_title, source, ats_domain, applied_at, status,"
            "  last_activity_at, notes)"
            " VALUES (?, ?, 'manual', ?, ?, ?, ?, ?)",
            (
                company.strip(),
                role_title.strip() or None,
                ats_domain.strip().lower() or None,
                applied_at,
                status,
                matcher.utcnow(),
                notes.strip(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/", status_code=303)


@router.get("/applications/{app_id}")
def application_detail(request: Request, app_id: int):
    conn = get_conn(request)
    try:
        app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        if app_row is None:
            return RedirectResponse("/", status_code=303)
        history = conn.execute(
            "SELECT * FROM status_history WHERE application_id = ? ORDER BY changed_at DESC",
            (app_id,),
        ).fetchall()
        emails = conn.execute(
            "SELECT * FROM emails WHERE application_id = ? ORDER BY received_at DESC", (app_id,)
        ).fetchall()
        context = {
            "app_row": dict(app_row),
            "prefill": EMPTY_PREFILL,
            "statuses": STATUSES,
            "history": history,
            "emails": emails,
            "review_count": _review_count(conn),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "app_form.html", context)


@router.post("/applications/{app_id}")
def application_update(
    request: Request,
    app_id: int,
    company: str = Form(...),
    role_title: str = Form(""),
    status: str = Form("applied"),
    applied_date: str = Form(""),
    ats_domain: str = Form(""),
    notes: str = Form(""),
):
    status = status if status in STATUSES else "applied"
    conn = get_conn(request)
    try:
        app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        if app_row:
            conn.execute(
                "UPDATE applications SET company = ?, role_title = ?, ats_domain = ?,"
                " applied_at = ?, notes = ? WHERE id = ?",
                (
                    company.strip(),
                    role_title.strip() or None,
                    ats_domain.strip().lower() or None,
                    f"{applied_date}T00:00:00Z" if applied_date else app_row["applied_at"],
                    notes.strip(),
                    app_id,
                ),
            )
            if status != app_row["status"]:
                matcher.record_manual_status(conn, app_id, app_row["status"], status)
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@router.post("/applications/{app_id}/delete")
def application_delete(request: Request, app_id: int):
    conn = get_conn(request)
    try:
        conn.execute("UPDATE emails SET application_id = NULL WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM status_history WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/", status_code=303)
