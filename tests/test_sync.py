import httpx
import pytest

from app import db
from app.outlook import sync
from app.outlook.client import GRAPH_BASE, GraphClient
from app.outlook.sync import (
    BACKFILL_DONE_KEY,
    BACKFILL_LINK_KEY,
    DELTA_LINK_KEY,
    sync_inbox,
)


def msg(mid: str, subject: str, addr: str, preview: str = "", removed: bool = False) -> dict:
    message = {
        "id": f"graph-{mid}",
        "internetMessageId": f"<{mid}@example.com>",
        "subject": subject,
        "receivedDateTime": "2026-07-01T10:00:00Z",
        "from": {"emailAddress": {"address": addr}},
        "bodyPreview": preview,
    }
    if removed:
        message["@removed"] = {"reason": "deleted"}
    return message


# --- delta pages (change tracking over a small recent window) ---------------
DELTA_INIT = {
    "value": [
        msg("d1", "Thank you for applying to Acme", "no-reply@greenhouse.io"),
        msg("d2", "50% off everything", "deals@webshop.com"),  # dropped by prefilter
    ],
    "@odata.deltaLink": f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta?token=final",
}
DELTA_INCREMENTAL = {
    "value": [
        msg("d1", "Thank you for applying to Acme", "no-reply@greenhouse.io"),  # dupe
        msg("d3", "gone", "x@greenhouse.io", removed=True),  # tombstone
        msg("d4", "Your application status", "no-reply@lever.co"),
    ],
    "@odata.deltaLink": f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta?token=final2",
}

# --- backfill pages (plain filtered list, reliable enumeration) -------------
LIST_PAGE_1 = {
    "value": [
        msg("b1", "Thank You for Applying with Voya", "voya@myworkday.com"),
        msg("b2", "New polo just dropped", "shop@brand.com"),  # dropped by prefilter
    ],
    "@odata.nextLink": f"{GRAPH_BASE}/me/mailFolders/inbox/messages?skip=100",
}
LIST_PAGE_2 = {
    "value": [
        msg("b3", "Interview invitation", "acme@us.greenhouse-mail.io"),
    ],
}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/delta" in url:
        if "token=expired" in url:
            return httpx.Response(410)
        if "token=final" in url:  # matches final and final2 — both serve increments
            return httpx.Response(200, json=DELTA_INCREMENTAL)
        return httpx.Response(200, json=DELTA_INIT)
    if "skip=100" in url:
        return httpx.Response(200, json=LIST_PAGE_2)
    return httpx.Response(200, json=LIST_PAGE_1)


@pytest.fixture
def client(settings, monkeypatch) -> GraphClient:
    monkeypatch.setattr("app.outlook.auth.get_access_token", lambda *args, **kwargs: "token")
    return GraphClient(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_first_sync_runs_delta_init_and_full_backfill(conn, client):
    inserted = await sync_inbox(conn, client)
    stored = {row["internet_message_id"] for row in conn.execute("SELECT * FROM emails")}
    assert stored == {"<d1@example.com>", "<b1@example.com>", "<b3@example.com>"}
    assert inserted == 3
    assert "token=final" in db.get_state(conn, DELTA_LINK_KEY)
    assert db.get_state(conn, BACKFILL_DONE_KEY) == "1"
    assert db.get_state(conn, BACKFILL_LINK_KEY) is None


async def test_second_sync_only_advances_delta_and_dedupes(conn, client):
    await sync_inbox(conn, client)
    inserted = await sync_inbox(conn, client)
    assert inserted == 1  # only d4; d1 deduped, tombstone skipped, backfill not rerun
    assert conn.execute("SELECT COUNT(*) AS c FROM emails").fetchone()["c"] == 4
    assert "token=final2" in db.get_state(conn, DELTA_LINK_KEY)


async def test_backfill_resumes_across_cycles(conn, client, monkeypatch):
    monkeypatch.setattr(sync, "BACKFILL_PAGES_PER_CYCLE", 1)
    await sync_inbox(conn, client)  # cycle 1: delta init + first backfill page only
    assert db.get_state(conn, BACKFILL_DONE_KEY) is None
    assert "skip=100" in db.get_state(conn, BACKFILL_LINK_KEY)
    stored = {row["internet_message_id"] for row in conn.execute("SELECT * FROM emails")}
    assert "<b3@example.com>" not in stored

    await sync_inbox(conn, client)  # cycle 2: resumes from the stored nextLink
    assert db.get_state(conn, BACKFILL_DONE_KEY) == "1"
    stored = {row["internet_message_id"] for row in conn.execute("SELECT * FROM emails")}
    assert "<b3@example.com>" in stored


async def test_expired_delta_link_reestablishes_tracking(conn, client):
    db.set_state(conn, BACKFILL_DONE_KEY, "1")  # isolate the delta path
    db.set_state(
        conn, DELTA_LINK_KEY, f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta?token=expired"
    )
    inserted = await sync_inbox(conn, client)
    assert inserted == 1  # recovered via a fresh small-window delta init
    assert "token=final" in db.get_state(conn, DELTA_LINK_KEY)


async def test_url_builders(client):
    delta_url, delta_params = client.delta_url(days=2)
    assert delta_url.endswith("/me/mailFolders/inbox/messages/delta")
    assert delta_params["$filter"].startswith("receivedDateTime ge ")

    backfill_url, backfill_params = client.backfill_url()
    assert backfill_url.endswith("/me/mailFolders/inbox/messages")
    assert backfill_params["$filter"].startswith("receivedDateTime ge ")
    assert backfill_params["$orderby"] == "receivedDateTime desc"
    assert backfill_params["$top"] == "100"
