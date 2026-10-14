"""Curated job-list feeds: employers the ATS poller can't reach (Workday, Oracle, ...).

- Zapply (github.com/zapplyjobs) Internships-2027 and New-Grad-Jobs-2027. The repos are
  display-only, so the README tables are the feed: Company | Role | Location | Posted |
  Visa | Apply. "✅ Sponsor" is Zapply's automated estimate that the employer sponsors
  visas (blank = unknown, not "no"). Only the newest ~100 listings per category are
  shown, turning over within a day, so it's polled every 2 hours and absence doesn't mean
  closed: those postings stay live for ZAPPLY_LIVE_DAYS after first seen. Reuse terms:
  credit Zapply and link back (the Discover page does); don't republish the listings.
- SimplifyJobs (github.com/SimplifyJobs) Summer2027-Internships — which also carries the
  off-season Winter/Spring terms — and New-Grad-Positions: a structured listings.json with
  explicit terms, category and an `active` flag that drives liveness. ~13MB each, but a
  full list changes slowly, so it's polled twice a day.

Feed postings have no job descriptions. `terms` records what the feed knows ("Winter 2027,
Spring 2027", "Internship", "New Grad"), which also vouches that a posting is
early-career; Simplify's category goes into the description so the field filter sees it.
"""

import json
import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app import db
from app.discovery import store
from app.discovery.ats import Posting
from app.discovery.terms import INTERN

log = logging.getLogger(__name__)

SOURCES = {  # credited and linked on the Discover page
    "Zapply": "https://github.com/zapplyjobs",
    "SimplifyJobs": "https://github.com/SimplifyJobs",
}
ZAPPLY_LIVE_DAYS = 45
SIMPLIFY_MAX_AGE_DAYS = 120  # "active" listings older than this are usually stale
NO_SPONSOR_TAGS = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}


@dataclass(frozen=True)
class Feed:
    name: str  # stored in postings.feed
    url: str
    kind: str  # "zapply" (README table) or "simplify" (listings.json)
    new_grad: bool
    every_hours: int


FEEDS = (
    Feed(
        "zapply-intern",
        "https://raw.githubusercontent.com/zapplyjobs/Internships-2027/main/README.md",
        "zapply",
        False,
        2,
    ),
    Feed(
        "zapply-newgrad",
        "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md",
        "zapply",
        True,
        2,
    ),
    Feed(
        "simplify-intern",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
        "simplify",
        False,
        12,
    ),
    Feed(
        "simplify-newgrad",
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
        "simplify",
        True,
        12,
    ),
)
POLL_HOURS = min(feed.every_hours for feed in FEEDS)

ZAPPLY_ROW = re.compile(
    r"^\|\s*\*\*(?P<company>[^|]+?)\*\*\s*"
    r"\|(?P<role>[^|]*)\|(?P<location>[^|]*)\|[^|]*\|(?P<visa>[^|]*)\|"
    r".*?\((?P<url>https://zapply\.jobs/l/d/(?P<slug>[^?)\s]+)[^)\s]*)\)",
    re.MULTILINE,
)


def _term(title: str, new_grad: bool) -> str:
    """A new-grad list also carries some internships; the title decides."""
    return "New Grad" if new_grad and not INTERN.search(title) else "Internship"


def parse_zapply(markdown: str, new_grad: bool = False) -> list[tuple[str, Posting]]:
    """(company, posting) for every table row; the same job listed twice is kept once."""
    seen: set[str] = set()
    listings = []
    for match in ZAPPLY_ROW.finditer(markdown):
        if match["slug"] in seen:
            continue
        seen.add(match["slug"])
        title, location = match["role"].strip(), match["location"].strip()
        listings.append(
            (
                match["company"].strip(),
                Posting(
                    external_id=match["slug"],
                    title=title,
                    location=location,
                    remote="remote" in location.lower(),
                    url=match["url"],
                    visa_sponsor="sponsor" in match["visa"].lower(),
                    terms=_term(title, new_grad),
                ),
            )
        )
    return listings


def parse_simplify(
    items: list[dict], new_grad: bool, now: datetime
) -> tuple[list[tuple[str, Posting]], set[str]]:
    """(company, posting) for active, recent listings, plus the ids of all active ones."""
    oldest = (now - timedelta(days=SIMPLIFY_MAX_AGE_DAYS)).timestamp()
    listings, active = [], set()
    for item in items:
        if not item.get("active") or not item.get("is_visible", True):
            continue
        active.add(str(item["id"]))
        if (item.get("date_posted") or 0) < oldest:
            continue
        title = (item.get("title") or "").strip()
        locations = item.get("locations") or []
        seasons = [term for term in item.get("terms") or [] if term and term != "N/A"]
        description = f"Category: {item['category']}." if item.get("category") else ""
        if item.get("sponsorship") in NO_SPONSOR_TAGS:
            description += " No visa sponsorship."  # trips the rules' no-sponsor filter
        listings.append(
            (
                (item.get("company_name") or "").strip(),
                Posting(
                    external_id=str(item["id"]),
                    title=title,
                    location="; ".join(locations)[:300],
                    remote=any("remote" in place.lower() for place in locations),
                    url=item.get("url") or "",
                    description=description.strip(),
                    visa_sponsor=item.get("sponsorship") == "Offers Sponsorship",
                    terms=_term(title, new_grad)
                    if new_grad
                    else ", ".join(seasons) or "Internship",
                ),
            )
        )
    return [(company, p) for company, p in listings if company and p.title and p.url], active


def ingest(
    conn: sqlite3.Connection,
    feed: Feed,
    listings: list[tuple[str, Posting]],
    now: str,
    active_ids: set[str] | None = None,
) -> int:
    """Store listings under feed-only companies, then update liveness. Commits.

    Companies whose own Greenhouse/Lever/Ashby board is polled are skipped: the board has
    full descriptions. Liveness follows the feed's `active` flag when it has one
    (active_ids), otherwise a fixed window after first seen.
    """
    polled = {
        row["name"].lower()
        for row in conn.execute("SELECT name FROM companies WHERE ats_type != 'feed'")
    }
    by_company: dict[str, list[Posting]] = defaultdict(list)
    for company, posting in listings:
        if company.lower() not in polled:
            by_company[company].append(posting)
    new = 0
    for company, postings in by_company.items():
        conn.execute(
            "INSERT INTO companies (name, ats_type, board_token) VALUES (?, 'feed', '')"
            " ON CONFLICT (name) DO NOTHING",
            (company,),
        )
        company_id = conn.execute("SELECT id FROM companies WHERE name = ?", (company,)).fetchone()[
            "id"
        ]
        new += store.upsert_postings(conn, company_id, postings, now, feed=feed.name)
    if active_ids is None:
        cutoff = (datetime.fromisoformat(now) - timedelta(days=ZAPPLY_LIVE_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "UPDATE postings SET is_live = 0 WHERE feed = ? AND is_live = 1 AND first_seen < ?",
            (feed.name, cutoff),
        )
    else:
        gone = [
            (row["id"],)
            for row in conn.execute(
                "SELECT id, external_id FROM postings WHERE feed = ? AND is_live = 1", (feed.name,)
            )
            if row["external_id"] not in active_ids
        ]
        conn.executemany("UPDATE postings SET is_live = 0 WHERE id = ?", gone)
    conn.commit()
    return new


def _due(conn: sqlite3.Connection, feed: Feed, now: datetime) -> bool:
    last = db.get_state(conn, f"feed_polled:{feed.name}")
    if last is None:
        return True
    # a few minutes' slack so a feed with every_hours=2 is due on every 2-hour tick
    next_due = datetime.fromisoformat(last) + timedelta(hours=feed.every_hours, minutes=-5)
    return now >= next_due


async def poll_feed(conn: sqlite3.Connection, http: httpx.AsyncClient, feed: Feed) -> None:
    """Fetch one feed (conditionally) and ingest it. Failures are logged, not raised."""
    now = datetime.now(UTC)
    etag_key = f"feed_etag:{feed.name}"
    etag = db.get_state(conn, etag_key)
    try:
        response = await http.get(feed.url, headers={"If-None-Match": etag} if etag else {})
        if response.status_code != 304:
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("%s feed fetch failed: %s", feed.name, exc)
        return
    db.set_state(conn, f"feed_polled:{feed.name}", now.isoformat())
    if response.status_code == 304:
        return
    active_ids = None
    try:
        if feed.kind == "simplify":
            listings, active_ids = parse_simplify(json.loads(response.text), feed.new_grad, now)
        else:
            listings = parse_zapply(response.text, feed.new_grad)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("%s feed unparseable: %s", feed.name, exc)
        return
    if not listings:  # the layout changed — don't silently ingest nothing forever
        log.warning("%s feed parsed to 0 listings; its format may have changed", feed.name)
        return
    new = ingest(conn, feed, listings, now.strftime("%Y-%m-%dT%H:%M:%SZ"), active_ids)
    db.set_state(conn, etag_key, response.headers.get("etag"))
    log.info("%s feed: %d listings, %d new postings", feed.name, len(listings), new)


async def poll(conn: sqlite3.Connection, http: httpx.AsyncClient) -> None:
    """Poll every feed that is due."""
    now = datetime.now(UTC)
    for feed in FEEDS:
        if _due(conn, feed, now):
            await poll_feed(conn, http, feed)
