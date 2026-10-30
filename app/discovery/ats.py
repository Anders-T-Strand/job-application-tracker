"""Fetch and normalize job postings from public ATS board APIs (no scraping)."""

import html
import json
import re
from html.parser import HTMLParser
from io import StringIO

import httpx
from pydantic import BaseModel

BOARD_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
}
MAX_DESCRIPTION_CHARS = 20_000
# Boards past this (decompressed) size are skipped: parsing them can OOM the
# 256MB container (Anduril's greenhouse board did exactly that).
MAX_BOARD_BYTES = 8_000_000
REMOTE_RE = re.compile(r"\bremote\b", re.I)


class Posting(BaseModel):
    """One normalized posting, ATS-agnostic."""

    external_id: str | None = None
    title: str
    location: str = ""
    remote: bool = False
    url: str
    description: str = ""
    visa_sponsor: bool = False  # a feed's sponsorship estimate (Zapply); ATS boards don't say
    terms: str = ""  # a feed's hiring-cycle tags ("Spring 2027", "New Grad"); vouches early-career


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._out = StringIO()

    def handle_data(self, data: str) -> None:
        self._out.write(data)

    BLOCKS = ("p", "br", "li", "div", "tr", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.BLOCKS:
            self._out.write("\n")

    def handle_endtag(self, tag: str) -> None:
        # "<h2>Qualifications:</h2>3+ years..." must not run the heading into the text
        if tag in self.BLOCKS:
            self._out.write("\n")

    def text(self) -> str:
        return self._out.getvalue()


def strip_html(markup: str) -> str:
    """HTML -> plain text: tags dropped, block tags become newlines, whitespace collapsed."""
    parser = _TextExtractor()
    parser.feed(markup)
    text = html.unescape(parser.text())
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _normalize_greenhouse(payload: dict) -> list[Posting]:
    postings = []
    for job in payload.get("jobs", []):
        if not job.get("title") or not job.get("absolute_url"):
            continue
        # Greenhouse returns `content` as entity-escaped HTML
        content = html.unescape(job.get("content") or "")
        location = ((job.get("location") or {}).get("name") or "").strip()
        postings.append(
            Posting(
                external_id=str(job["id"]) if job.get("id") is not None else None,
                title=job["title"].strip(),
                location=location,
                remote=bool(REMOTE_RE.search(location)),
                url=job["absolute_url"],
                description=strip_html(content)[:MAX_DESCRIPTION_CHARS],
            )
        )
    return postings


def _normalize_lever(payload: list) -> list[Posting]:
    postings = []
    for job in payload:
        if not job.get("text") or not job.get("hostedUrl"):
            continue
        categories = job.get("categories") or {}
        location = (categories.get("location") or "").strip()
        description = job.get("descriptionPlain") or strip_html(job.get("description") or "")
        additional = job.get("additionalPlain") or ""
        if additional:  # Lever puts EEO/visa boilerplate in `additional`
            description = f"{description}\n{additional}"
        postings.append(
            Posting(
                external_id=job.get("id"),
                title=job["text"].strip(),
                location=location,
                remote=job.get("workplaceType") == "remote" or bool(REMOTE_RE.search(location)),
                url=job["hostedUrl"],
                description=description[:MAX_DESCRIPTION_CHARS],
            )
        )
    return postings


def _normalize_ashby(payload: dict) -> list[Posting]:
    postings = []
    for job in payload.get("jobs", []):
        if not job.get("title") or job.get("isListed") is False:
            continue
        url = job.get("jobUrl") or job.get("applyUrl")
        if not url:
            continue
        locations = [job.get("location") or ""] + [
            (secondary.get("location") or "") for secondary in job.get("secondaryLocations") or []
        ]
        location = "; ".join(part for part in locations if part).strip()
        description = job.get("descriptionPlain") or strip_html(job.get("descriptionHtml") or "")
        postings.append(
            Posting(
                external_id=job.get("id"),
                title=job["title"].strip(),
                location=location,
                remote=bool(job.get("isRemote")) or bool(REMOTE_RE.search(location)),
                url=url,
                description=description[:MAX_DESCRIPTION_CHARS],
            )
        )
    return postings


NORMALIZERS = {
    "greenhouse": _normalize_greenhouse,
    "lever": _normalize_lever,
    "ashby": _normalize_ashby,
}


async def fetch_board(
    http: httpx.AsyncClient, ats_type: str, token: str, etag: str | None
) -> tuple[list[Posting] | None, str | None]:
    """Fetch one company's board. Returns (postings, etag); postings is None on 304.

    Raises ValueError on oversized or malformed responses (caller skips the company).
    """
    headers = {"If-None-Match": etag} if etag else {}
    async with http.stream(
        "GET", BOARD_URLS[ats_type].format(token=token), headers=headers
    ) as response:
        if response.status_code == 304:
            return None, etag
        response.raise_for_status()
        size = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BOARD_BYTES:
                raise ValueError(f"board response exceeds {MAX_BOARD_BYTES} bytes")
            chunks.append(chunk)
    payload = json.loads(b"".join(chunks))
    return NORMALIZERS[ats_type](payload), response.headers.get("etag")
