"""Inbox synchronisation via Microsoft Graph.

Two mechanisms, because Graph's delta endpoint reliably reports *new* mail but
does not reliably enumerate deep history:

- **Backfill**: a plain paged list query over the backfill window, resumable
  across poll cycles via a stored nextLink. Runs until complete, once.
- **Increments**: a delta query established over a small recent window, whose
  deltaLink is advanced every cycle.

Overlap between the two is harmless — dedupe is on internet_message_id.
"""

import logging
import sqlite3

from app import db
from app.classifier.prefilter import passes_prefilter
from app.outlook.client import DeltaExpired, GraphClient

log = logging.getLogger(__name__)

DELTA_LINK_KEY = "delta_link"
BACKFILL_LINK_KEY = "backfill_link"
BACKFILL_DONE_KEY = "backfill_done"

DELTA_INIT_DAYS = 2  # small overlap window when (re)establishing change tracking
MAX_DELTA_PAGES = 400
BACKFILL_PAGES_PER_CYCLE = 60  # 60 pages x 100 messages, keeps a cycle under ~a minute


async def sync_inbox(conn: sqlite3.Connection, client: GraphClient) -> int:
    """Advance delta tracking, then continue the backfill if unfinished.

    Returns the number of newly stored emails.
    """
    inserted = await _sync_delta(conn, client)
    if db.get_state(conn, BACKFILL_DONE_KEY) != "1":
        inserted += await _backfill_step(conn, client)
    return inserted


async def _sync_delta(conn: sqlite3.Connection, client: GraphClient) -> int:
    delta_link = db.get_state(conn, DELTA_LINK_KEY)
    try:
        return await _walk_delta(conn, client, delta_link)
    except DeltaExpired:
        log.warning("delta link expired; re-establishing change tracking")
        db.set_state(conn, DELTA_LINK_KEY, None)
        return await _walk_delta(conn, client, None)


async def _walk_delta(conn: sqlite3.Connection, client: GraphClient, delta_link: str | None) -> int:
    if delta_link:
        url, params = delta_link, None
    else:
        url, params = client.delta_url(days=DELTA_INIT_DAYS)
    inserted = 0
    for _ in range(MAX_DELTA_PAGES):
        page = await client.get_json(url, params=params)
        params = None
        for message in page.get("value", []):
            inserted += _store_message(conn, message)
        conn.commit()
        next_link = page.get("@odata.nextLink")
        if next_link:
            url = next_link
            continue
        new_delta = page.get("@odata.deltaLink")
        if new_delta:
            db.set_state(conn, DELTA_LINK_KEY, new_delta)
        return inserted
    log.warning("hit MAX_DELTA_PAGES; continuing next cycle")
    return inserted


async def _backfill_step(conn: sqlite3.Connection, client: GraphClient) -> int:
    """Walk up to BACKFILL_PAGES_PER_CYCLE pages of the historical list query."""
    url = db.get_state(conn, BACKFILL_LINK_KEY)
    params = None
    if not url:
        url, params = client.backfill_url()
        log.info("starting historical backfill")
    inserted = 0
    for _ in range(BACKFILL_PAGES_PER_CYCLE):
        page = await client.get_json(url, params=params)
        params = None
        for message in page.get("value", []):
            inserted += _store_message(conn, message)
        conn.commit()
        next_link = page.get("@odata.nextLink")
        if not next_link:
            db.set_state(conn, BACKFILL_LINK_KEY, None)
            db.set_state(conn, BACKFILL_DONE_KEY, "1")
            log.info("historical backfill complete")
            return inserted
        db.set_state(conn, BACKFILL_LINK_KEY, next_link)
        url = next_link
    log.info("backfill checkpoint: %d new emails this cycle, more pages remain", inserted)
    return inserted


def _store_message(conn: sqlite3.Connection, message: dict) -> int:
    if "@removed" in message:
        return 0
    internet_message_id = message.get("internetMessageId")
    if not internet_message_id:
        return 0
    from_addr = ((message.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    subject = message.get("subject") or ""
    snippet = (message.get("bodyPreview") or "").strip()
    if not passes_prefilter(from_addr, subject, snippet):
        return 0
    cursor = conn.execute(
        "INSERT OR IGNORE INTO emails"
        " (internet_message_id, graph_id, received_at, from_addr, subject, snippet)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            internet_message_id,
            message.get("id", ""),
            message.get("receivedDateTime", ""),
            from_addr.lower(),
            subject,
            snippet,
        ),
    )
    return 1 if cursor.rowcount > 0 else 0
