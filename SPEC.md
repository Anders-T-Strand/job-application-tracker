# Job Application Tracker — Build Spec

Personal, always-on service that tracks my job applications automatically. It watches my Outlook mailbox (personal Microsoft account) for application confirmations and responses (rejections, OA invites, interview requests), classifies them with an LLM, and keeps a private dashboard up to date. Single user: me.

## Deployment context (important constraints)

- Runs 24/7 on my existing Oracle Cloud VM.Standard.E2.1.Micro (x86_64, **1GB RAM total**), which already runs a Discord bot in Docker.
- This project joins the same Docker Compose setup as a second service. Keep it lightweight: single container, `python:3.12-slim`, no Prometheus/Grafana, no heavy frontend build.
- Dashboard must NOT be publicly exposed. It binds to localhost / the Tailscale interface only and is served over Tailscale (`tailscale serve` for HTTPS). After Tailscale works, all inbound ports in the Oracle security list get closed.
- Dev happens locally on macOS; deploy = `git pull && docker compose up -d --build` on the VM. Docker is only required on the VM — local dev runs the app directly with `uvicorn --reload` (OrbStack or Docker Desktop optional, only for testing the container locally before deploying).

## Architecture (v1 — email-first)

No browser extension in v1. Nearly every ATS (Workday, Greenhouse, Lever, iCIMS, Ashby) sends a confirmation email, so an Outlook watcher handles both detection of new applications and status updates.

Components, all in one FastAPI app:

1. **Outlook poller** — background asyncio task, polls every 90s. Two Graph mechanisms, because delta does not reliably enumerate deep history: the **historical backfill** is a plain paged list query over the backfill window (`$filter=receivedDateTime ge ...`, resumable across cycles via a stored nextLink), and **ongoing changes** are tracked with a delta query over a small recent window (store the `deltaLink`; on 410 Gone, re-establish it). Both are narrowed to likely-recruiting mail by the prefilter. Dedupe on `internetMessageId` (stable across folder moves, unlike the Graph message id).
2. **Classifier** — cheap heuristic prefilter first (sender domain contains known ATS domains, or subject/body keywords like "application", "interview", "assessment", "unfortunately"). Only messages passing the prefilter go to the Anthropic API (`claude-haiku-4-5`, max_tokens ~300). Strict JSON output:
   `{is_job_related, type, company, role_title, confidence}` where `type` ∈ `application_confirmation | rejection | oa_invite | interview_invite | recruiter_outreach | offer | other`.
   Input to the model: from address, subject, first ~1500 chars of plain-text body (fetched lazily via Graph with `Prefer: outlook.body-content-type="text"`). Parse defensively (strip code fences, validate with pydantic).
3. **Matcher** — fuzzy-match classified emails to existing applications by company name + sender domain (rapidfuzz). Match → update status + append to history. No match on an `application_confirmation` → create a new application. Ambiguous → flag for manual review in the dashboard.
4. **Dashboard** — server-rendered Jinja2 + htmx (lightest option for the RAM budget). Views: pipeline board grouped by status; sortable table; stats (response rate, median days to first response, apps per week); follow-up flags (applied >14 days with no response). htmx polling every 30s keeps it live while open. Manual add/edit for applications that never send a confirmation email.
5. **Health** — ping a healthchecks.io URL after each successful poll cycle so silent failures alert me.

## Data model (SQLite, WAL mode)

- `applications`: id, company, role_title, source (`email`/`manual`), ats_domain, applied_at, status (`applied|oa|screen|interview|offer|rejected|ghosted`), last_activity_at, notes
- `emails`: id, internet_message_id (UNIQUE), graph_id, application_id FK nullable, received_at, from_addr, subject, snippet, classification, confidence, processed, needs_review
- `status_history`: id, application_id, old_status, new_status, changed_at, triggered_by (`email:<id>` or `manual`)
- `sync_state`: key/value (stores `delta_link`, backfill markers)

DB file lives in a mounted volume so it survives container rebuilds.

## Security requirements (non-negotiable)

- Microsoft Graph OAuth scope: **`Mail.Read` only.** Never request ReadWrite/Send scopes.
- Auth is a one-time **device code flow** against a personal Azure app registration (public client, "Personal Microsoft accounts" account type, "Allow public client flows" enabled). No client secret exists or is needed.
- MSAL token cache lives in a volume-mounted `secrets/` dir, `chmod 600`, gitignored. `ANTHROPIC_API_KEY` and the healthchecks URL come from `.env`, gitignored. Provide `.env.example`.
- Token longevity: unlike Google's "Testing"-mode 7-day expiry, personal Microsoft account refresh tokens roll on use — the 90s poll loop keeps them fresh indefinitely. Re-run the authorize script only if the app has been down for ~90 days.
- App binds to `127.0.0.1` on the host (compose publishes `127.0.0.1:8080` only) — never `0.0.0.0` on a public interface. Access is via Tailscale only.

## Build phases (work through these in order)

1. **Scaffold**: repo layout, FastAPI app skeleton, SQLite schema + tiny migration runner, config loading, pytest setup, ruff.
2. **Outlook sync**: MSAL device-code flow (one-time local auth creates the token cache), 90-day backfill via initial delta query, incremental delta sync loop, message storage. Use a mock classifier at this stage.
3. **Classifier + matcher**: Haiku integration, prefilter, JSON parsing, fuzzy matching, status-update rules. Unit tests with ~10 sample emails (confirmation, rejection, OA, recruiter outreach, non-job mail).
4. **Dashboard**: Jinja2 + htmx views listed above, manual add/edit, review queue for ambiguous matches.
5. **Deploy**: Dockerfile, compose service definition (with `restart: unless-stopped`, volumes for db + secrets), healthchecks.io ping, README section documenting Tailscale serve setup and Oracle port closure. Confirm total container RSS stays modest (~150MB target).
6. **v2 (later, separate)**: browser extension to log applications from ATS pages that never email confirmations.

## Conventions

- Python 3.12, full type hints, ruff for lint/format, pytest for tests.
- Keep dependencies minimal: fastapi, uvicorn, jinja2, msal, anthropic, rapidfuzz, httpx, pydantic.
- Simple > clever. This is a single-user tool on a 1GB box.
- Portfolio matters: clear README with architecture diagram, honest metrics, setup instructions.

## Notes for the human steps (Claude: remind me when we reach them)

- I need to create the Azure app registration (Entra ID → App registrations, "Personal Microsoft accounts only", enable public client flows, add delegated `Mail.Read`) and put the Application (client) ID in `.env` as `MS_CLIENT_ID` — walk me through it at Phase 2.
- I need to create the healthchecks.io check and Tailscale auth at Phase 5.
