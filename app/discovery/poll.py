"""Discovery orchestration: poll ATS boards + job-list feeds -> rules filter -> LLM scoring.

Runs inside the app (discover_forever: boards daily, feeds every few hours) or one-shot
via `python -m app.discovery`.
"""

import asyncio
import logging
import sqlite3
import time

import httpx

from app import db
from app.config import Settings
from app.discovery import ats, details, feeds, rules, scorer, store
from app.llm import make_llm
from app.matcher import utcnow

log = logging.getLogger(__name__)

REQUEST_GAP_SECONDS = 1.0  # courtesy gap between board fetches (each host sees at most 1 req/s)


async def poll_company(
    conn: sqlite3.Connection, http: httpx.AsyncClient, company: sqlite3.Row
) -> None:
    """Fetch one board, upsert its postings, retire the ones that disappeared. Commits."""
    now = utcnow()
    try:
        postings, etag = await ats.fetch_board(
            http, company["ats_type"], company["board_token"], company["etag"]
        )
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("board fetch failed for %s: %s", company["name"], exc)
        return
    if postings is None:  # 304 — unchanged, so current liveness still holds
        conn.execute("UPDATE companies SET last_polled = ? WHERE id = ?", (now, company["id"]))
        conn.commit()
        return
    new = store.upsert_postings(conn, company["id"], postings, now)
    gone = store.mark_missing(conn, company["id"], now)
    conn.execute(
        "UPDATE companies SET last_polled = ?, etag = ? WHERE id = ?",
        (now, etag, company["id"]),
    )
    conn.commit()
    if new or gone:
        log.info("%s: %d new postings, %d gone", company["name"], new, gone)


async def discover_once(settings: Settings, boards: bool = True) -> None:
    """One cycle: ATS boards (unless boards=False), due feeds, rules, feed descriptions
    for title-only passes (then the rules again, now on real text), and scoring."""
    conn = db.connect(settings.db_path)
    local = rules.locality(settings.graduation_year, settings.local_area)
    try:
        companies = []
        if boards:
            companies = conn.execute(
                "SELECT * FROM companies WHERE active = 1 AND ats_type != 'feed' ORDER BY name"
            ).fetchall()
            if not companies:
                log.info("no active companies — run scripts/seed_companies.py first")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            for company in companies:
                await poll_company(conn, http, company)
                await asyncio.sleep(REQUEST_GAP_SECONDS)
            if settings.feeds_enabled:
                await feeds.poll(conn, http)
            passed, rejected = store.run_rules(conn, local, settings.graduation_year)
            if settings.feeds_enabled and await details.enrich(
                conn, http, settings.graduation_year
            ):
                # the described postings passed once on their title; some won't on the text
                _, now_rejected = store.run_rules(conn, local, settings.graduation_year)
                passed, rejected = passed - now_rejected, rejected + now_rejected
        if passed or rejected:
            log.info("rules filter: %d passed, %d rejected", passed, rejected)
        llm = make_llm(settings)
        if llm is not None:
            scored = await scorer.score_pending(
                conn,
                llm,
                scorer.load_profile(settings.resume_path),
                constraints=scorer.load_constraints(settings.profile_path),
                graduation_year=settings.graduation_year,
            )
            if scored:
                log.info("scored %d postings", scored)
    finally:
        conn.close()


async def discover_forever(settings: Settings) -> None:
    board_hours = settings.discovery_interval_hours
    tick_hours = min(feeds.POLL_HOURS, board_hours) if settings.feeds_enabled else board_hours
    boards_due = 0.0  # monotonic time of the next full board poll
    while True:
        boards = time.monotonic() >= boards_due
        try:
            await discover_once(settings, boards=boards)
            if boards:
                boards_due = time.monotonic() + board_hours * 3600
        except Exception:
            log.exception("discovery cycle failed")
        await asyncio.sleep(tick_hours * 3600)
