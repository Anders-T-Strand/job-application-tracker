"""Thin async Microsoft Graph client (raw REST via httpx — no heavy Graph SDK)."""

import asyncio
import html
import re
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.outlook import auth

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DELTA_SELECT = "id,subject,from,receivedDateTime,bodyPreview,internetMessageId"
PREFER_TEXT = 'outlook.body-content-type="text"'


class DeltaExpired(Exception):
    """The stored deltaLink is no longer valid (HTTP 410) — a bounded re-sync is needed."""


class GraphClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _token(self, force_refresh: bool = False) -> str:
        s = self.settings
        return await asyncio.to_thread(
            auth.get_access_token, s.ms_client_id, s.ms_authority, s.secrets_dir, force_refresh
        )

    async def get_json(self, url: str, params: dict | None = None) -> dict:
        force_refresh = False
        last_response: httpx.Response | None = None
        for attempt in range(4):
            token = await self._token(force_refresh)
            response = await self.http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Prefer": PREFER_TEXT},
            )
            last_response = response
            if response.status_code == 401 and attempt < 3:
                force_refresh = True
                continue
            if response.status_code in (429, 503) and attempt < 3:
                retry_after = float(response.headers.get("Retry-After", "5"))
                await asyncio.sleep(min(retry_after, 60))
                continue
            if response.status_code == 410:
                raise DeltaExpired
            response.raise_for_status()
            return response.json()
        assert last_response is not None
        last_response.raise_for_status()
        return last_response.json()

    def delta_url(self, days: int) -> tuple[str, dict]:
        """URL + params to establish delta change tracking over a small recent window.

        Delta reliably reports new/changed mail but does NOT reliably enumerate
        deep history, so the window here is only for overlap — the historical
        backfill uses backfill_url() instead.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        params = {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$select": DELTA_SELECT,
            "$top": "50",
        }
        return f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta", params

    def backfill_url(self) -> tuple[str, dict]:
        """URL + params for a plain paged list of the backfill window (reliable enumeration)."""
        since = datetime.now(UTC) - timedelta(days=self.settings.backfill_days)
        params = {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime desc",
            "$select": DELTA_SELECT,
            "$top": "100",
        }
        return f"{GRAPH_BASE}/me/mailFolders/inbox/messages", params

    async def get_body_text(self, graph_id: str) -> str:
        data = await self.get_json(
            f"{GRAPH_BASE}/me/messages/{graph_id}", params={"$select": "body"}
        )
        body = data.get("body") or {}
        content = body.get("content", "")
        # Prefer header usually yields text, but fall back to stripping HTML
        if body.get("contentType") == "html":
            content = strip_html(content)
        return content


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(content: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", content)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()
