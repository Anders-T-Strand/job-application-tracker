"""Discovery routes: score-sorted postings with save/dismiss/applied actions."""

import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, matcher
from app.discovery import feeds, terms
from app.web.routes import _review_count

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

VIEWS = ("new", "saved", "dismissed", "applied")
SORTS = {
    "score": "p.llm_score IS NULL, p.llm_score DESC, p.first_seen DESC",
    "lca": "c.lca_filings DESC, p.llm_score IS NULL, p.llm_score DESC",
    "recent": "p.first_seen DESC",
}
TERMS = ("target", "all")  # target: terms.TARGET (final-term internships, new grad)
FILTER_KEYS = ("view", "sort", "sponsor", "term", "low")  # carried through action redirects
MIN_SCORE = 50  # the new view hides postings scored below this unless asked (low=1)


def get_conn(request: Request) -> sqlite3.Connection:
    return db.connect(request.app.state.settings.db_path)


def _new_count(conn: sqlite3.Connection, graduation_year: int | None = None) -> int:
    """New postings in the default view (target terms when configured), for the nav badge."""
    rows = conn.execute(
        "SELECT title, terms, substr(description, 1, ?) AS description FROM postings"
        " WHERE status = 'new' AND is_live = 1 AND rules_status = 'passed'"
        " AND (llm_score IS NULL OR llm_score >= ?)",
        (terms.DESC_CHARS, MIN_SCORE),
    ).fetchall()
    if not graduation_year:
        return len(rows)
    return sum(
        terms.bucket(row["title"], row["terms"], row["description"], graduation_year)
        in terms.TARGET
        for row in rows
    )


@router.get("/discover")
def discover(
    request: Request,
    view: str = "new",
    sort: str = "score",
    sponsor: bool = False,
    term: str = "",
    low: bool = False,
):
    year = request.app.state.settings.graduation_year
    view = view if view in VIEWS else "new"
    # the term filter needs a graduation year to know the cycle; without one, show all
    term = term if term in TERMS and year else ("target" if year else "all")
    order_by = SORTS.get(sort) or SORTS["score"]
    where = "p.status = ?"
    if view == "new":  # new postings only matter while live and rules-passed
        where += " AND p.is_live = 1 AND p.rules_status = 'passed'"
    if sponsor:
        where += " AND p.visa_sponsor = 1"
    conn = get_conn(request)
    try:
        postings, hidden_low = [], 0
        for row in conn.execute(
            "SELECT p.*, c.name AS company_name, c.lca_filings, c.lca_year"
            f" FROM postings p JOIN companies c ON c.id = p.company_id WHERE {where}"  # noqa: S608
            f" ORDER BY {order_by}",
            (view,),
        ):
            item = dict(row)
            item["term"] = terms.bucket(row["title"], row["terms"], row["description"], year)
            # the term and score filters are for triaging new postings; saved/applied show all
            if view == "new" and term != "all" and item["term"] not in terms.TARGET:
                continue
            score = row["llm_score"]
            if view == "new" and not low and score is not None and score < MIN_SCORE:
                hidden_low += 1
                continue
            postings.append(item)
        context = {
            "postings": postings,
            "view": view,
            "views": VIEWS,
            "sort": sort if sort in SORTS else "score",
            "sponsor": sponsor,
            "term": term,
            "low": low,
            "hidden_low": hidden_low,
            "min_score": MIN_SCORE,
            "term_filter": bool(year),
            "term_labels": terms.labels(year),
            "sources": feeds.SOURCES,
            "company_count": conn.execute(
                "SELECT COUNT(*) AS c FROM companies WHERE active = 1 AND ats_type != 'feed'"
            ).fetchone()["c"],
            "discover_count": _new_count(conn, year),
            "review_count": _review_count(conn),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "discover.html", context)


def _back(request: Request) -> RedirectResponse:
    """Back to the Discover page with the filters the action was posted from."""
    query = {k: v for k, v in request.query_params.items() if k in FILTER_KEYS}
    return RedirectResponse(f"/discover?{urlencode(query)}", status_code=303)


def _set_status(request: Request, posting_id: int, from_statuses: tuple[str, ...], to: str) -> None:
    conn = get_conn(request)
    try:
        placeholders = ",".join("?" * len(from_statuses))
        conn.execute(
            f"UPDATE postings SET status = ? WHERE id = ? AND status IN ({placeholders})",  # noqa: S608
            (to, posting_id, *from_statuses),
        )
        conn.commit()
    finally:
        conn.close()


@router.post("/discover/{posting_id}/save")
def discover_save(request: Request, posting_id: int):
    _set_status(request, posting_id, ("new", "dismissed"), "saved")
    return _back(request)


@router.post("/discover/{posting_id}/dismiss")
def discover_dismiss(request: Request, posting_id: int):
    _set_status(request, posting_id, ("new", "saved"), "dismissed")
    return _back(request)


@router.post("/discover/{posting_id}/restore")
def discover_restore(request: Request, posting_id: int):
    _set_status(request, posting_id, ("saved", "dismissed"), "new")
    return _back(request)


@router.post("/discover/{posting_id}/applied")
def discover_applied(request: Request, posting_id: int):
    """Lifecycle handoff: create an application for the posting and link it back."""
    conn = get_conn(request)
    try:
        posting = conn.execute(
            "SELECT p.*, c.name AS company_name FROM postings p"
            " JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
            (posting_id,),
        ).fetchone()
        if posting and posting["status"] != "applied":
            now = matcher.utcnow()
            cursor = conn.execute(
                "INSERT INTO applications"
                " (company, role_title, source, applied_at, status, last_activity_at, notes)"
                " VALUES (?, ?, 'discovery', ?, 'applied', ?, ?)",
                (posting["company_name"], posting["title"], now, now, posting["url"]),
            )
            conn.execute(
                "UPDATE postings SET status = 'applied', application_id = ? WHERE id = ?",
                (cursor.lastrowid, posting_id),
            )
            conn.commit()
    finally:
        conn.close()
    return _back(request)
