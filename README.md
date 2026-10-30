# Job Application Tracker

Personal service that tracks my job applications automatically. It watches my Outlook mailbox (Microsoft Graph, read-only) for application confirmations and responses, classifies them with a local LLM (Qwen via Ollama; Claude Haiku optional), and keeps a private dashboard up to date over Tailscale. Single user, single container, runs on my own PC next to Ollama.

## Architecture

```
Outlook mailbox (personal Microsoft account)
      │  Microsoft Graph delta query — Mail.Read only, polled every 90s
      ▼
┌────────────────────── FastAPI container ───────────────────────┐
│  poller ─► prefilter ─► LLM classifier ───► fuzzy matcher ──┐   │
│  (asyncio task)         (Ollama / Qwen)      (rapidfuzz)    ▼   │
│                                                     SQLite (WAL)│
│  Jinja2 + htmx dashboard  ◄─────────────────────────────────┘   │
│  (board / table / stats / review queue)                         │
└──────────┬─────────────────────┬────────────┬───────────────────┘
           │ 127.0.0.1:8080      │            └──► healthchecks.io ping
           ▼                     └──► Ollama on the host (host.docker.internal:11434)
   tailscale serve (HTTPS, tailnet-only)
```

- **Poller** — background asyncio task. Historical backfill runs as a paged list query over the backfill window (resumable across cycles — Graph's delta endpoint doesn't reliably enumerate deep history); ongoing changes are tracked with a delta query and stored `deltaLink` (re-established on 410). Dedupe on `internetMessageId`.
- **Prefilter** — known ATS sender domains + recruiting keywords (English and Norwegian), so only likely-recruiting mail costs an LLM call.
- **Classifier** — a local model through Ollama (`qwen3.8:27b` by default) or `claude-haiku-4-5` with `LLM_PROVIDER=anthropic`. Strict JSON output (schema-constrained on Ollama) validated with pydantic; unusable output lands in the review queue instead of looping. If the backend is unreachable, mail stays queued and the batch retries next cycle.
- **Matcher** — rapidfuzz on company name + ATS sender domain. Confident matches update status and history; new confirmations create applications; anything ambiguous or low-confidence goes to the dashboard review queue.
- **Lifecycle** — after each poll cycle, once no mail is waiting to be classified (`app/lifecycle.py`): an open application (applied/oa/screen/interview) with no activity for **60 days** goes to `ghosted`; ghosted for **90 days** → `rejected`; rejected for **60 days** → hidden from the board and table (kept in the database, so stats keep their history; **Table → show** lists them, and a new email or manual status change brings one back). Offers are never touched. Time in a status counts from the last move into it or later activity, whichever is newer; automatic moves are logged in the history as `auto`, don't change "last activity", and don't count as responses on the stats page. A move restarts the clock, so a long-silent application is ghosted first and only goes further after the next waiting period.

## Local development

Requires Python 3.12 ([uv](https://docs.astral.sh/uv/) handles that automatically) and
Ollama with the model pulled (see [LLM backend](#llm-backend)):

```sh
uv venv --python 3.12
source .venv/bin/activate     # Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"

cp .env.example .env          # fill in MS_CLIENT_ID
python scripts/authorize.py  # one-time device-code login (see Azure setup below)

uvicorn app.main:app --reload  # dashboard on http://127.0.0.1:8000
```

Run checks:

```sh
ruff check . && ruff format --check .
pytest
```

Set `POLL_ENABLED=false` in `.env` to work on the dashboard without Graph credentials.
Tests never call a real model (the fixture pins the Anthropic backend with a fake key).

## Personal configuration

Everything specific to the candidate lives outside the code, in gitignored files:

| What | Where | Used by |
|---|---|---|
| Résumé text | `data/resume.txt` | discovery scoring, résumé tailoring |
| Timing / location / visa constraints, free text | `data/profile.md` (start from [`profile.example.md`](profile.example.md)) | discovery scoring — sent with the résumé on every batch |
| Spring graduation year | `GRADUATION_YEAR` in `.env` | Discover's term buckets and default view |
| Places reachable while still in school | `LOCAL_AREA` in `.env` (comma-separated) | with `GRADUATION_YEAR`, the school-term locality rule |

All of it is optional: without them, discovery scores against a generic early-career AI/ML
profile and shows every term.

## LLM backend

Four jobs use an LLM: classifying recruiting email, pre-filling the quick-log form,
scoring discovered postings, and the résumé-tailoring page. By default they run on a local model through
[Ollama](https://ollama.com) — free, private, and on an RTX-class GPU about 0.5s per email:

```sh
ollama pull qwen3.8:27b       # ~17GB; fits a 24–32GB GPU with room for the KV cache
```

| Setting | Default | |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `anthropic` to use Claude Haiku instead (needs `ANTHROPIC_API_KEY`) |
| `OLLAMA_URL` | `http://localhost:11434` | compose overrides it to `http://host.docker.internal:11434` |
| `OLLAMA_MODEL` | `qwen3.8:27b` | any local model; Qwen3 "thinking" is switched off per request |
| `OLLAMA_NUM_CTX` | `16384` | context window; the scorer's 8-posting batches need ~6k tokens |

Notes:

- On a 9-email sample (confirmation, rejection, OA, interview, recruiter, offer, job alert,
  Norwegian rejection, non-job), `qwen3.8:27b` and `qwen3.6:35b-a3b` both classified all
  nine correctly; the dense 27b was better at extracting the company from the sender
  domain and uses less VRAM, so it's the default.
- Ollama unloads an idle model after 5 minutes (freeing the GPU), so the first call after a
  quiet spell takes ~15s to load it. Background work doesn't care; a quick-log just loads slower.
- If Ollama isn't running, nothing is lost: mail stays unclassified and discovery postings
  stay unscored until the next cycle that can reach it.

## Azure setup (one-time, human steps)

The app reads mail via Microsoft Graph with the **`Mail.Read` delegated scope only** — it can never modify or send mail. There is no client secret; auth is a device-code flow against a public client registration.

1. Go to [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name: `job-application-tracker`. Supported account types: **Personal Microsoft accounts only**. No redirect URI needed.
3. After creation, copy the **Application (client) ID** into `.env` as `MS_CLIENT_ID`.
4. Under **Authentication** → *Advanced settings*, set **Allow public client flows** to **Yes** and save.
5. Under **API permissions**, confirm delegated **Microsoft Graph → Mail.Read** (add it if missing; the default `User.Read` can stay). No admin consent needed — you consent personally during login.
6. Run `python scripts/authorize.py`, open the printed URL (microsoft.com/devicelogin), enter the code, and sign in with the Outlook account. The MSAL token cache lands in `secrets/msal_token_cache.json` (`chmod 600`, gitignored).

Token longevity: personal-account refresh tokens roll on use, and the 90-second poll loop keeps them fresh indefinitely. Re-run the authorize script only if the app has been offline for ~90 days (or the token was revoked).

## Deployment (own machine: Docker Desktop + Ollama)

Runs on a desktop with Docker Desktop, Ollama and Tailscale (Windows here; macOS/Linux work
the same way). The container reaches Ollama on the host via `host.docker.internal`.

```sh
git pull
cp .env.example .env          # fill in values
# secrets/: run scripts/authorize.py once, or copy secrets/ from the old host
docker compose up -d --build
```

The compose file publishes the dashboard on **`127.0.0.1:8080` only** — nothing is reachable from the public internet. Then expose it inside the tailnet:

```sh
tailscale serve --bg 8080     # sudo on Linux
```

The dashboard is now at `https://<host>.<tailnet>.ts.net`. If something else already
serves the tailnet root on that machine (check `tailscale serve status`), use another HTTPS
port instead of replacing it: `tailscale serve --bg --https=8443 8080` →
`https://<host>.<tailnet>.ts.net:8443`.

Always-on-ish: with Docker Desktop and Ollama set to start at login, the tracker comes up
with the PC (`restart: unless-stopped`). It only polls while the PC is awake, but nothing is
missed — the Graph delta query catches up on everything that arrived in the meantime, and
discovery runs on each start. Re-run `scripts/authorize.py` only if the PC is off for ~90 days.

Health monitoring (optional): create a check at [healthchecks.io](https://healthchecks.io) and put its ping URL in `.env` as `HEALTHCHECK_URL`. The poller pings it after every successful cycle. On a PC that sleeps, set the grace period to cover overnight gaps or leave it unset.

Memory: the container is capped at 1GB (`mem_limit`); typical RSS is far below — check with `docker stats`.

### Moving from the Oracle VM

1. On the VM: `docker compose stop job-tracker` and remove the tracker's cron lines
   (`crontab -e`; the Discord bot is unaffected). Stopping first keeps one poller per mailbox.
2. Copy state to the PC over Tailscale (or restore `data/tracker.db` from the Azure backup):
   ```sh
   scp <vm>:job-application-tracker/data/tracker.db data/
   scp <vm>:job-application-tracker/secrets/msal_token_cache.json secrets/
   scp <vm>:job-application-tracker/.env .env
   ```
   In `.env`, `ANTHROPIC_API_KEY` is now optional (kept only for `LLM_PROVIDER=anthropic`).
3. `docker compose up -d --build`, then `tailscale serve --bg 8080` on the PC.
4. Point the iPhone shortcut at the PC's tailnet name and re-drag the bookmarklet from **+ Add**.

### Running on a small VM instead

The old setup still works on a 1GB box without a GPU: in `.env` set `LLM_PROVIDER=anthropic`,
`ANTHROPIC_API_KEY=...` and `DISCOVERY_ENABLED=false`; in compose drop `mem_limit` to `256m`;
run discovery from host cron (see [Job discovery](#job-discovery)). After `tailscale serve`
works, close all inbound ports in the cloud provider's firewall.

## Data model

SQLite in WAL mode, stored in the `./data` volume so it survives rebuilds:

- `applications` — company, role, source (email/manual), ATS domain, status (`applied|oa|screen|interview|offer|rejected|ghosted`), timestamps, notes, `hidden_at` (old rejections hidden by the lifecycle)
- `emails` — one row per stored message; `internet_message_id` unique; carries the raw classification JSON and review flags
- `status_history` — every status transition with its trigger (`email:<id>`, `manual`, or `auto` for lifecycle moves)
- `sync_state` — the Graph `deltaLink` and other sync markers
- `companies` — curated ATS boards for discovery (plus `ats_type='feed'` employers seen only in feeds), with DOL LCA filing counts
- `postings` — discovered job postings: filter results, LLM score, feed name, term tags and `visa_sponsor` estimate, and lifecycle status

## Security notes

- Graph scope is `Mail.Read` only; the app cannot write or send mail.
- Secrets: MSAL token cache in `secrets/` (mode 600), API keys in `.env` — both gitignored and volume-mounted, never baked into the image.
- The app is only reachable via localhost on the host and HTTPS inside the tailnet.

## Logging applications that never send a confirmation email

Some ATS flows send nothing. Two one-tap ways to log those — both open the add form
pre-filled by the LLM from the posting's URL and title, and nothing is saved until you press Save:

- **Mac**: open **+ Add** in the dashboard and drag the *"Log job application"* bookmarklet
  to your bookmarks bar. Click it on any job posting.
- **iPhone** (Shortcuts app → new shortcut):
  1. Shortcut receives **URLs** from the **Share Sheet**
  2. **URL Encode** — input: Shortcut Input
  3. **Text** — `https://<host>.<tailnet>.ts.net/log?url=` followed by the *URL Encoded Text* variable
  4. **Open URLs** — the Text

  Then on any job page: Share → your shortcut → confirm the pre-filled form → Save.

Each quick-log is one small LLM call — free on Ollama (~$0.0006 on Haiku).

## Job discovery

The **Discover** tab surfaces early-career AI/ML/DS postings from a curated list of company
boards (design and decisions in `DISCOVERY_SPEC.md`). Public Greenhouse/Lever/Ashby board
APIs are polled daily by a background task — no job-board scraping. Postings pass a cheap
regex filter (stage/field/US/sponsorship; rejections keep a `rules_reason` for tuning), then
the LLM scores survivors 0–100 against `data/resume.txt` (drop your resume text there;
a generic early-career-AI/ML profile is used until you do). Save/dismiss postings in the UI;
**Applied ↗** creates a tracked application (`source='discovery'`), which later confirmation
emails match onto instead of duplicating.

Job-list feeds cover the employers those boards can't reach (mostly Workday, Oracle and
SmartRecruiters) — `app/discovery/feeds.py`, `FEEDS_ENABLED=false` turns them off:

| Feed | What it reads | Polled | Live while |
|---|---|---|---|
| [Zapply Internships-2027](https://github.com/zapplyjobs/Internships-2027) | README tables (the repo is display-only) | every 2 h | 45 days after first seen |
| [Zapply New-Grad-Jobs-2027](https://github.com/zapplyjobs/New-Grad-Jobs-2027) | same | every 2 h | 45 days after first seen |
| [SimplifyJobs Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) | `listings.json` — includes the off-season Winter/Spring terms | every 12 h | the listing's `active` flag |
| [SimplifyJobs New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) | `listings.json` | every 12 h | the listing's `active` flag |

Zapply's README only shows the newest ~100 listings per category (turning over within a day),
hence the frequent polls and the fixed live window. Its **✅ Sponsor** mark is an automated
estimate (blank means unknown, not "no"); Simplify's "no sponsorship / citizenship required"
tags trip the rules' no-sponsor filter. Feed postings have no job description: each records
the feed's term tags (`Winter 2027, Spring 2027`, `Internship`, `New Grad`), which stand in
for an early-career word in the title, and Simplify's category goes into the description for
the field filter. Companies whose own board is polled are skipped, and the same job from two
feeds is stored once (matched on company + title). Zapply's terms allow this with credit and
a link back, shown on the Discover page; don't republish the listings to other aggregators.

**Descriptions for feed postings** (`app/discovery/details.py`): feeds only give a title, so
once a feed posting passes the title-only rules, its description is fetched from the
employer's job system — the Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Oracle HCM and
Workable public APIs, else the schema.org `JobPosting` JSON-LD most career sites embed for
search engines (Zapply links are followed through their redirect; one that lands back on
zapply.jobs means the job closed). The posting then goes back through the rules — now able to
see "no sponsorship", clearance and graduation-date requirements — and is re-scored on the
real text. Up to 200 per cycle, one request at a time, default-view postings first; Summer+
internships are skipped. On a sample of 80, 74 got a description and 14 of those were then
rejected (mostly explicit no-sponsorship).

**Graduation-aware terms:** with `GRADUATION_YEAR` set (see
[Personal configuration](#personal-configuration)), each posting is bucketed by hiring cycle
(`app/discovery/terms.py`) — final-term (Winter/Spring) internships, New grad, Summer+
internships, or unspecified — from the feed's tags, the title, then the top of the
description. **Discover** then shows final-term internships + New grad by default (**terms:
all** shows the rest), and the scorer works through those first. With `LOCAL_AREA` set too,
postings that name a remaining school term (fall, winter or spring before graduation) must
be in that area or remote. Bank-style "Summer Analyst/Associate" titles, apprenticeships and
student jobs count as internships even when a new-grad list carries them, and a description
that only accepts graduation dates after the candidate's ("graduating between December 2027
and May 2028", "Class of 2028") is rejected — "graduating by …" upper bounds are not.
The **new** view also hides postings scored below 50 behind a "show" link.

Setup:

```sh
# seed the companies table from the SimplifyJobs internship/new-grad repos
python scripts/seed_companies.py            # add --dry-run / --limit N to preview
# optional H-1B signal: download an LCA disclosure file (dol.gov → performance data),
# convert to CSV if needed, then:
python scripts/load_lca.py LCA_Disclosure_Data_FY2025.csv --year 2025
```

By default the poll runs inside the app (`DISCOVERY_ENABLED=true`): boards once at startup,
then every `DISCOVERY_INTERVAL_HOURS=24`, with conditional requests + 1 req/s per host; due
feeds every 2 hours in between. To run it by hand: `docker compose exec job-tracker python -m app.discovery`.

On a 1GB VM, set `DISCOVERY_ENABLED=false` and run the poll from host cron in its own
short-lived container instead, so a hungry cycle can never OOM the web app:

```sh
(crontab -l 2>/dev/null | grep -v app.discovery; \
 echo "43 2 * * * cd $HOME/job-application-tracker && docker compose run --rm job-tracker python -m app.discovery >> discovery.log 2>&1") | crontab -
```

## Backups

`scripts/backup.sh` takes a consistent SQLite snapshot (SQLite backup API, safe on a live
WAL database) and uploads it to Azure Blob Storage with plain `curl` — 7 rolling weekday
blobs plus 12 rolling monthly blobs, so retention needs no pruning logic. Runs daily from
Task Scheduler (Windows, via Git Bash) or cron. Cost: free for 12 months (5GB allowance), then a few cents per month.

One-time Azure setup:

1. Portal → **Storage accounts** → Create (Standard performance, **LRS** redundancy).
2. In the account: **Containers** → create `backups` (private access).
3. Open the container → **Shared access tokens** → permissions **Create + Write** only,
   expiry a few years out → Generate → copy the **Blob SAS URL**.
4. In the repo root, save it from Git Bash (and optionally a healthchecks.io ping URL as
   `.backup_ping_url` for backup monitoring):

```sh
umask 077
echo 'PASTE_BLOB_SAS_URL' > .backup_sas_url
```

5. Schedule it. Windows (PowerShell, from the repo root; `-StartWhenAvailable` runs a
   missed 03:17 backup as soon as the PC is back on):

```powershell
$action = New-ScheduledTaskAction -Execute "C:\Program Files\Git\bin\bash.exe" `
  -Argument '-c "scripts/backup.sh >> backup.log 2>&1"' -WorkingDirectory (Get-Location)
$trigger = New-ScheduledTaskTrigger -Daily -At 3:17am
Register-ScheduledTask -TaskName "job-tracker-backup" -Action $action -Trigger $trigger `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

   Linux:

```sh
(crontab -l 2>/dev/null | grep -v backup.sh; \
 echo "17 3 * * * $HOME/job-application-tracker/scripts/backup.sh >> $HOME/job-application-tracker/backup.log 2>&1") | crontab -
```

**Restore**: download a blob from the portal, stop the container, replace
`data/tracker.db` (delete any `-wal`/`-shm` files), start the container.

## Roadmap

- Per-application reminders / notes timeline.
