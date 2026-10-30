"""Résumé tailoring route: analyze a posting (scraped or pasted) against the résumé."""

import logging
import sqlite3

from fastapi import APIRouter, Form, Request

from app import db, tailor
from app.llm import LLMError, make_llm
from app.web.discovery import _new_count
from app.web.routes import _review_count
from app.web.templating import templates

log = logging.getLogger(__name__)

router = APIRouter()

RECENT_LIMIT = 50  # scraped postings offered in the "load" dropdown
EMPTY_PREFILL = {"title": "", "company": "", "description": ""}


def get_conn(request: Request) -> sqlite3.Connection:
    return db.connect(request.app.state.settings.db_path)


def _recent_postings(conn: sqlite3.Connection) -> list[dict]:
    """Live postings that carry a description, best-scored first, for the load dropdown."""
    rows = conn.execute(
        "SELECT p.id, p.title, c.name AS company_name FROM postings p"
        " JOIN companies c ON c.id = p.company_id"
        " WHERE p.is_live = 1 AND p.description IS NOT NULL AND p.description != ''"
        " ORDER BY p.llm_score IS NULL, p.llm_score DESC, p.first_seen DESC LIMIT ?",
        (RECENT_LIMIT,),
    ).fetchall()
    return [dict(row) for row in rows]


def _render(request: Request, conn: sqlite3.Connection, **extra: object):
    context = {
        "recent": _recent_postings(conn),
        "discover_count": _new_count(conn, request.app.state.settings.graduation_year),
        "review_count": _review_count(conn),
        "prefill": EMPTY_PREFILL,
        "analysis": None,
        "error": None,
    }
    context.update(extra)
    return templates.TemplateResponse(request, "tailor.html", context)


@router.get("/tailor")
def tailor_form(request: Request, posting_id: int | None = None):
    """Show the tailor form; optionally prefill from an already-scraped posting."""
    conn = get_conn(request)
    try:
        prefill = dict(EMPTY_PREFILL)
        if posting_id is not None:
            row = conn.execute(
                "SELECT p.title, p.description, c.name AS company_name FROM postings p"
                " JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
                (posting_id,),
            ).fetchone()
            if row:
                prefill = {
                    "title": row["title"],
                    "company": row["company_name"],
                    "description": row["description"] or "",
                }
        return _render(request, conn, prefill=prefill)
    finally:
        conn.close()


@router.post("/tailor")
async def tailor_analyze(
    request: Request,
    description: str = Form(""),
    title: str = Form(""),
    company: str = Form(""),
):
    settings = request.app.state.settings
    prefill = {"title": title, "company": company, "description": description}
    analysis = None
    error = None
    llm = make_llm(settings)
    if not description.strip():
        error = "Paste a job description, or load a scraped posting, first."
    elif llm is None:
        error = "No LLM backend is configured, so analysis is unavailable."
    else:
        try:
            resume = tailor.load_profile(settings.resume_path)
            analysis = await tailor.analyze(llm, resume, title, company, description)
            if analysis is None:
                error = "Couldn't parse the analysis — please try again."
        except LLMError:
            log.warning("tailor analysis request failed", exc_info=True)
            error = "The analysis request failed — please try again."
    conn = get_conn(request)
    try:
        return _render(request, conn, prefill=prefill, analysis=analysis, error=error)
    finally:
        conn.close()
