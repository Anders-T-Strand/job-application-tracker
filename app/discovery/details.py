"""Fetch job descriptions for feed postings, so rules and scoring see real requirements.

Feeds list only a title. For feed postings that pass the title-only rules, this fetches
the description from the employer's job system and stores it as plain text; the posting
then goes back through the rules (sponsorship, graduation window, ...) and is re-scored.

Sources, by URL: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Oracle HCM,
Workable and amazon.jobs public APIs; anything else falls back to the schema.org JobPosting JSON-LD
many career sites embed for search engines. Zapply links are redirects: a live job
resolves to the employer URL, a closed one back to zapply.jobs (then marked not live).

One request at a time with a short gap, capped per discovery cycle; default-view
postings (final-term internships, new grad) go first and Summer+ internships are
skipped. detail_status records the outcome: ok, closed, empty (no description found or
unsupported site), failed (network/HTTP error; retried after RETRY_FAILED_DAYS).
"""

import asyncio
import html
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from app.discovery import terms
from app.discovery.ats import strip_html

log = logging.getLogger(__name__)

MAX_PER_RUN = 200
REQUEST_GAP_SECONDS = 0.5
RETRY_FAILED_DAYS = 3
MAX_CHARS = 20_000
MAX_PAGE_BYTES = 3_000_000  # for the JSON-LD fallback
HEADERS = {"User-Agent": "job-application-tracker (personal use)"}

LOCALE = re.compile(r"[a-z]{2}-[a-z]{2}", re.I)
JSON_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


class Closed(Exception):
    """The posting is gone (feed redirect to its home page, or a 404 from the source)."""


def _parts(url: str) -> tuple[str, list[str]]:
    parsed = urlparse(url)
    return parsed.netloc.lower(), [part for part in parsed.path.split("/") if part]


async def _json(http: httpx.AsyncClient, url: str, **kwargs) -> dict:
    response = await http.get(url, headers={**HEADERS, "Accept": "application/json"}, **kwargs)
    if response.status_code == 404:
        raise Closed(url)
    response.raise_for_status()
    return response.json()


async def _workday(http: httpx.AsyncClient, url: str) -> str:
    host, parts = _parts(url)
    if parts and LOCALE.fullmatch(parts[0]):
        parts = parts[1:]
    site, rest = parts[0], parts[1:]
    if rest and rest[-1].lower() in ("apply", "applymanually"):
        rest = rest[:-1]
    tenant = host.split(".")[0]
    data = await _json(http, f"https://{host}/wday/cxs/{tenant}/{site}/{'/'.join(rest)}")
    return data["jobPostingInfo"]["jobDescription"]


async def _greenhouse(http: httpx.AsyncClient, url: str) -> str:
    _, parts = _parts(url)  # /{board}/jobs/{id}
    data = await _json(
        http, f"https://boards-api.greenhouse.io/v1/boards/{parts[0]}/jobs/{parts[2]}"
    )
    return html.unescape(data["content"])


async def _lever(http: httpx.AsyncClient, url: str) -> str:
    _, parts = _parts(url)  # /{org}/{id}[/apply]
    data = await _json(http, f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}")
    lists = "\n".join(f"{item['text']}\n{item['content']}" for item in data.get("lists") or [])
    return "\n\n".join(
        part for part in (data.get("descriptionPlain"), lists, data.get("additionalPlain")) if part
    )


async def _ashby(http: httpx.AsyncClient, url: str, cache: dict) -> str:
    _, parts = _parts(url)  # /{org}/{id}[/application]
    org, job_id = parts[0], parts[1]
    if org not in cache:  # the API serves a whole board; fetch each board once per run
        cache[org] = await _json(http, f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    for job in cache[org].get("jobs", []):
        if job.get("id") == job_id:
            return job.get("descriptionHtml") or job.get("descriptionPlain") or ""
    raise Closed(url)


async def _smartrecruiters(http: httpx.AsyncClient, url: str) -> str:
    _, parts = _parts(url)  # /{company}/{id}[-slug]
    posting_id = parts[1].split("-")[0]
    data = await _json(
        http, f"https://api.smartrecruiters.com/v1/companies/{parts[0]}/postings/{posting_id}"
    )
    sections = (data.get("jobAd") or {}).get("sections") or {}
    return "\n\n".join(section.get("text", "") for section in sections.values())


async def _oracle(http: httpx.AsyncClient, url: str) -> str:
    host, parts = _parts(url)  # /hcmUI/CandidateExperience/{lang}/sites/{site}/job/{id}
    site, job_id = parts[parts.index("sites") + 1], parts[parts.index("job") + 1]
    data = await _json(
        http,
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
        params={
            "expand": "all",
            "onlyData": "true",
            "finder": f'ById;Id="{job_id}",siteNumber={site}',
        },
    )
    items = data.get("items") or []
    if not items:
        raise Closed(url)
    item = items[0]
    return "\n\n".join(
        item.get(key) or ""
        for key in (
            "ExternalDescriptionStr",
            "ExternalQualificationsStr",
            "ExternalResponsibilitiesStr",
        )
    )


async def _workable(http: httpx.AsyncClient, url: str) -> str:
    _, parts = _parts(url)  # /{account}/j/{shortcode}[/apply]
    data = await _json(
        http, f"https://apply.workable.com/api/v2/accounts/{parts[0]}/jobs/{parts[2]}"
    )
    return "\n\n".join(data.get(key) or "" for key in ("description", "requirements", "benefits"))


async def _amazon(http: httpx.AsyncClient, url: str) -> str:
    _, parts = _parts(url)  # /{lang}/jobs/{id}/{slug}
    job_id = parts[parts.index("jobs") + 1]
    data = await _json(
        http, "https://www.amazon.jobs/en/search.json", params={"job_id_icims[]": job_id}
    )
    for job in data.get("jobs") or []:
        if str(job.get("id_icims")) == job_id:
            return (  # headings keep basic vs preferred apart for the experience rule
                f"{job.get('description') or ''}<p>Basic Qualifications</p>"
                f"{job.get('basic_qualifications') or ''}<p>Preferred Qualifications</p>"
                f"{job.get('preferred_qualifications') or ''}"
            )
    raise Closed(url)


async def _json_ld(http: httpx.AsyncClient, url: str) -> str:
    response = await http.get(url, headers=HEADERS, follow_redirects=True)
    if response.status_code == 404:
        raise Closed(url)
    response.raise_for_status()
    page = response.text[:MAX_PAGE_BYTES]
    for block in JSON_LD.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item.get("description") or ""
    return ""


async def _resolve_zapply(http: httpx.AsyncClient, url: str) -> str:
    response = await http.get(url, headers=HEADERS, follow_redirects=True)
    final = str(response.url)
    if urlparse(final).netloc.endswith("zapply.jobs"):  # expired: sent back to the job list
        raise Closed(url)
    return final


async def fetch_description(http: httpx.AsyncClient, url: str, cache: dict) -> str:
    """The posting's description as plain text ('' if none found). Raises Closed."""
    host, parts = _parts(url)
    if host.endswith("zapply.jobs"):
        url = await _resolve_zapply(http, url)
        host, parts = _parts(url)
    if host.endswith("myworkdayjobs.com") and "job" in [p.lower() for p in parts]:
        raw = await _workday(http, url)
    elif host.endswith("greenhouse.io") and len(parts) >= 3 and parts[1] == "jobs":
        raw = await _greenhouse(http, url)
    elif host == "jobs.lever.co" and len(parts) >= 2:
        raw = await _lever(http, url)
    elif host == "jobs.ashbyhq.com" and len(parts) >= 2:
        raw = await _ashby(http, url, cache)
    elif host == "jobs.smartrecruiters.com" and len(parts) >= 2:
        raw = await _smartrecruiters(http, url)
    elif host.endswith("oraclecloud.com") and "sites" in parts and "job" in parts:
        raw = await _oracle(http, url)
    elif host == "apply.workable.com" and len(parts) >= 3 and parts[1] == "j":
        raw = await _workable(http, url)
    elif host.endswith("amazon.jobs") and "jobs" in parts[:-1]:
        raw = await _amazon(http, url)
    else:  # custom career sites (incl. Greenhouse-on-own-domain ?gh_jid=...)
        raw = await _json_ld(http, url)
    return strip_html(raw)[:MAX_CHARS].strip()


def _candidates(conn: sqlite3.Connection, now: datetime, graduation_year: int | None) -> list:
    retry_before = (now - timedelta(days=RETRY_FAILED_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id, title, terms, description, url FROM postings"
        " WHERE feed IS NOT NULL AND is_live = 1 AND status = 'new' AND rules_status = 'passed'"
        " AND (detail_status IS NULL OR (detail_status = 'failed' AND detail_fetched_at < ?))"
        " ORDER BY first_seen DESC",
        (retry_before,),
    ).fetchall()
    order = {"final_term": 0, "new_grad": 0, "unspecified": 1}  # "later" isn't worth a request
    ranked = [
        (order[bucket], row)
        for row in rows
        if (bucket := terms.bucket(row["title"], row["terms"], row["description"], graduation_year))
        in order
    ]
    ranked.sort(key=lambda pair: pair[0])  # stable: newest first within each group
    return [row for _, row in ranked]


async def enrich(
    conn: sqlite3.Connection,
    http: httpx.AsyncClient,
    graduation_year: int | None = None,
    limit: int = MAX_PER_RUN,
) -> int:
    """Fetch descriptions for feed postings that passed the title-only rules. Commits.

    Returns how many got a description; those go back to rules_status 'pending' with
    their score cleared, so the caller should run the rules again before scoring.
    """
    now = datetime.now(UTC)
    stamp = now.isoformat()
    cache: dict = {}
    described = closed = 0
    for row in _candidates(conn, now, graduation_year)[:limit]:
        status = "ok"
        try:
            text = await fetch_description(http, row["url"], cache)
            if not text:
                status = "empty"
        except Closed:
            status = "closed"
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.info("description fetch failed for posting %s (%s): %r", row["id"], row["url"], exc)
            status = "failed"
        if status == "ok":
            category = row["description"] or ""  # Simplify's "Category: ..." line
            description = f"{category}\n\n{text}".strip()
            conn.execute(
                "UPDATE postings SET description = ?, detail_status = 'ok', detail_fetched_at = ?,"
                " rules_status = 'pending', rules_reason = NULL, llm_score = NULL,"
                " llm_rationale = NULL WHERE id = ?",
                (description, stamp, row["id"]),
            )
            described += 1
        elif status == "closed":
            conn.execute(
                "UPDATE postings SET is_live = 0, detail_status = 'closed', detail_fetched_at = ?"
                " WHERE id = ?",
                (stamp, row["id"]),
            )
            closed += 1
        else:
            conn.execute(
                "UPDATE postings SET detail_status = ?, detail_fetched_at = ? WHERE id = ?",
                (status, stamp, row["id"]),
            )
        conn.commit()
        await asyncio.sleep(REQUEST_GAP_SECONDS)
    if described or closed:
        log.info("descriptions: %d fetched, %d postings closed", described, closed)
    return described
