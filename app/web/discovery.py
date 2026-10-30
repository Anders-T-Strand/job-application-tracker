"""Discovery routes: score-sorted postings with save/dismiss/applied actions."""

import dataclasses
import re
import sqlite3
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from app import db, matcher
from app.discovery import feeds, rules, terms
from app.web.routes import _review_count
from app.web.templating import templates

router = APIRouter()

VIEWS = ("new", "saved", "dismissed", "applied")
SORTS = {
    "score": "p.llm_score IS NULL, p.llm_score DESC, p.first_seen DESC",
    "lca": "c.lca_filings DESC, p.llm_score IS NULL, p.llm_score DESC",
    "recent": "p.first_seen DESC",
}
TERMS = ("final_term", "new_grad", "later", "unspecified")  # terms.bucket values
# The "type" filter, by title keyword; a title can be more than one ("Summer Associate").
KINDS = {
    "intern": re.compile(r"\b(intern(ship)?s?|summer (associate|analyst)s?|apprentice\w*)\b", re.I),
    "co-op": re.compile(r"\bco[- ]?ops?\b", re.I),
    "associate": re.compile(r"\bassociates?\b", re.I),
    "analyst": re.compile(r"\banalysts?\b", re.I),
}
OTHER_KIND = "other"  # none of KINDS
FILTER_KEYS = ("view", "sort", "term", "kind", "visa", "low", "search")  # kept across actions
MIN_SCORE = 50  # the new view hides postings scored below this unless asked (low=1)


@dataclasses.dataclass(frozen=True)
class Filters:
    """The page's filters. Within a group (terms, kinds) any selected value matches; the
    groups, the visa mark and the search all have to match."""

    view: str = "new"
    sort: str = "score"
    terms: tuple[str, ...] | None = None  # None: the view's default; () = any term
    kinds: tuple[str, ...] = ()
    visa: bool = False
    low: bool = False
    search: str = ""

    def query(self, **changes) -> str:
        """This page's query string, with some filters changed."""
        f = dataclasses.replace(self, **changes)
        items = [("view", f.view), ("sort", f.sort)]
        if f.terms is not None:  # "all" marks an explicit choice, even of no terms
            items += [("term", "all"), *(("term", t) for t in f.terms)]
        items += [("kind", k) for k in f.kinds]
        if f.visa:
            items.append(("visa", "1"))
        if f.low:
            items.append(("low", "1"))
        if f.search:
            items.append(("search", f.search))
        return urlencode(items)


def _kinds(title: str) -> set[str]:
    return {kind for kind, pattern in KINDS.items() if pattern.search(title)} or {OTHER_KIND}


def visa_note(visa_sponsor: int, description: str | None) -> str:
    """Why a posting looks visa-friendly, or '' if nothing says so."""
    if visa_sponsor:
        return "A feed's automated estimate that the employer sponsors visas"
    if rules.SPONSORS.search(description or ""):
        return "The posting says it sponsors visas"
    if rules.STUDENT_VISA_OK.search(description or ""):
        return "The posting welcomes student visas (CPT/OPT)"
    return ""


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
    term: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    visa: bool = False,
    low: bool = False,
    search: str = "",
):
    year = request.app.state.settings.graduation_year
    view = view if view in VIEWS else "new"
    # without a graduation year there's no cycle to place internships in
    term_options = TERMS if year else ("new_grad", "unspecified")
    f = Filters(
        view=view,
        sort=sort if sort in SORTS else "score",
        terms=None if term is None else tuple(t for t in term_options if t in term),
        kinds=tuple(k for k in (*KINDS, OTHER_KIND) if k in (kind or ())),
        visa=visa,
        low=low,
        search=search.strip(),
    )
    # by default, new postings are triaged for the final term and new grad; the rest show all
    shown_terms = f.terms
    if shown_terms is None:
        shown_terms = terms.TARGET if view == "new" and year else ()
    where = "p.status = ?"
    if view == "new":  # new postings only matter while live and rules-passed
        where += " AND p.is_live = 1 AND p.rules_status = 'passed'"
    conn = get_conn(request)
    try:
        postings, hidden_low = [], 0
        for row in conn.execute(
            "SELECT p.*, c.name AS company_name, c.lca_filings, c.lca_year"
            f" FROM postings p JOIN companies c ON c.id = p.company_id WHERE {where}"  # noqa: S608
            f" ORDER BY {SORTS[f.sort]}",
            (view,),
        ):
            item = dict(row)
            item["term"] = terms.bucket(row["title"], row["terms"], row["description"], year)
            if shown_terms and item["term"] not in shown_terms:
                continue
            if f.kinds and not _kinds(row["title"]) & set(f.kinds):
                continue
            item["visa_note"] = visa_note(row["visa_sponsor"], row["description"])
            if f.visa and not item["visa_note"]:
                continue
            haystack = f"{row['title']} {row['company_name']}".lower()
            if f.search and f.search.lower() not in haystack:
                continue
            # the score cutoff is for triaging new postings; saved/applied show all
            score = row["llm_score"]
            if view == "new" and not f.low and score is not None and score < MIN_SCORE:
                hidden_low += 1
                continue
            postings.append(item)
        context = {
            "postings": postings,
            "f": f,
            "views": VIEWS,
            "term_options": term_options,
            "shown_terms": shown_terms,
            "default_terms": f.terms is None and bool(shown_terms),
            "filtered": bool(f.terms or f.kinds or f.visa or f.search),
            "kind_options": (*KINDS, OTHER_KIND),
            "hidden_low": hidden_low,
            "min_score": MIN_SCORE,
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
    query = [(k, v) for k, v in request.query_params.multi_items() if k in FILTER_KEYS]
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
