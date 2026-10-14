"""Background poll loop: sync inbox -> classify new mail -> match/update -> ping healthcheck."""

import asyncio
import logging
import sqlite3

import httpx

from app import db, lifecycle, matcher
from app.classifier.llm import classify_email
from app.config import Settings
from app.llm import LLM, LLMError, make_llm
from app.outlook.auth import AuthError
from app.outlook.client import GraphClient
from app.outlook.sync import sync_inbox

log = logging.getLogger(__name__)

CLASSIFY_BATCH = 25  # per cycle, bounds LLM load during the initial backfill


async def poll_forever(settings: Settings) -> None:
    llm = make_llm(settings)
    if llm is None:
        log.warning("no LLM backend configured; new mail is stored but left unclassified")
    client = GraphClient(settings)
    try:
        while True:
            try:
                await poll_once(settings, client, llm)
                await ping_healthcheck(settings)
            except AuthError as exc:
                log.error("Graph auth failed: %s", exc)
            except Exception:
                log.exception("poll cycle failed")
            await asyncio.sleep(settings.poll_interval_seconds)
    finally:
        await client.aclose()


async def poll_once(settings: Settings, client: GraphClient, llm: LLM | None) -> None:
    conn = db.connect(settings.db_path)
    try:
        new = await sync_inbox(conn, client)
        if new:
            log.info("stored %d new candidate emails", new)
        if llm is None:
            return
        pending = conn.execute(
            "SELECT id, graph_id, from_addr, subject, snippet FROM emails"
            " WHERE processed = 0 ORDER BY received_at LIMIT ?",
            (CLASSIFY_BATCH,),
        ).fetchall()
        for row in pending:
            try:
                await _classify_and_apply(conn, client, llm, row)
            except LLMError as exc:
                # backend down (e.g. Ollama not running) — stop rather than fail each email
                log.warning("classifier unavailable, retrying next cycle: %s", exc)
                break
        if not conn.execute("SELECT 1 FROM emails WHERE processed = 0 LIMIT 1").fetchone():
            # only once every update is applied: a still-pending email may be exactly the
            # activity that should keep an application open
            lifecycle.apply_rules(conn)
    finally:
        conn.close()


async def _classify_and_apply(
    conn: sqlite3.Connection,
    client: GraphClient,
    llm: LLM,
    row: sqlite3.Row,
) -> None:
    try:
        body = await client.get_body_text(row["graph_id"])
    except httpx.HTTPError as exc:
        log.warning("could not fetch body for email %s (%s); using snippet", row["id"], exc)
        body = row["snippet"]
    cls = await classify_email(llm, row["from_addr"], row["subject"], body)
    if cls is None:
        # unusable model output: park it in the review queue instead of retrying forever
        conn.execute("UPDATE emails SET processed = 1, needs_review = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        return
    matcher.process_email(conn, row["id"], cls)


async def ping_healthcheck(settings: Settings) -> None:
    if not settings.healthcheck_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.get(settings.healthcheck_url)
    except httpx.HTTPError as exc:
        log.warning("healthcheck ping failed: %s", exc)
