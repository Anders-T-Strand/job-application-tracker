"""Seed the companies table from the SimplifyJobs internship / new-grad repos.

Run: python scripts/seed_companies.py [--limit N] [--dry-run] [--add "Name:ats:token"]

Parses each repo's listings.json for direct Greenhouse/Lever/Ashby URLs and
extracts board tokens. Companies whose listings only link to other ATSes (or to
simplify.jobs redirects) are skipped — this seeder only covers boards the
discovery poller can poll.
"""

import argparse
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.config import load_settings  # noqa: E402

LISTINGS_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
]

TOKEN_PATTERNS = {
    "greenhouse": re.compile(
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_(?:board|app)\?for=)?([A-Za-z0-9_-]+)"
    ),
    "lever": re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
}
NOT_TOKENS = {"embed", "jobs"}  # path fragments the greenhouse pattern can catch


def extract_board(url: str) -> tuple[str, str] | None:
    """(ats_type, board_token) from a posting URL, or None if not a supported ATS."""
    for ats_type, pattern in TOKEN_PATTERNS.items():
        match = pattern.search(url)
        if match and match.group(1) not in NOT_TOKENS:
            return ats_type, match.group(1)
    return None


def collect(listings: list[dict]) -> dict[str, tuple[str, str]]:
    """name -> (ats_type, token) for listings with a pollable ATS URL."""
    boards: dict[str, tuple[str, str]] = {}
    for listing in listings:
        name = (listing.get("company_name") or "").strip()
        if not name or listing.get("active") is False:
            continue
        board = extract_board(listing.get("url") or "")
        if board and name not in boards:
            boards[name] = board
    return boards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="cap number of companies inserted")
    parser.add_argument("--dry-run", action="store_true", help="print what would be inserted")
    parser.add_argument(
        "--add",
        action="append",
        default=[],
        metavar="NAME:ATS:TOKEN",
        help="manually add a company, e.g. 'OpenAI:ashby:openai' (repeatable)",
    )
    args = parser.parse_args()

    boards: dict[str, tuple[str, str]] = {}
    for spec in args.add:
        try:
            name, ats_type, token = spec.split(":", 2)
        except ValueError:
            sys.exit(f"--add needs NAME:ATS:TOKEN, got {spec!r}")
        if ats_type not in TOKEN_PATTERNS:
            sys.exit(f"unknown ats type {ats_type!r} (use greenhouse|lever|ashby)")
        boards[name] = (ats_type, token)

    with httpx.Client(timeout=60, follow_redirects=True) as http:
        for url in LISTINGS_URLS:
            response = http.get(url)
            response.raise_for_status()
            found = collect(response.json())
            for name, board in found.items():
                boards.setdefault(name, board)
            print(f"{url.split('/')[4]}: {len(found)} companies with pollable boards")

    items = sorted(boards.items())
    if args.limit:
        items = items[: args.limit]
    if args.dry_run:
        for name, (ats_type, token) in items:
            print(f"{name}: {ats_type}/{token}")
        print(f"(dry run) {len(items)} companies")
        return

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    inserted = 0
    for name, (ats_type, token) in items:
        cursor = conn.execute(
            "INSERT INTO companies (name, ats_type, board_token) VALUES (?, ?, ?)"
            " ON CONFLICT (name) DO NOTHING",
            (name, ats_type, token),
        )
        inserted += cursor.rowcount
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS c FROM companies").fetchone()["c"]
    conn.close()
    print(f"inserted {inserted} new companies ({total} total) into {settings.db_path}")


if __name__ == "__main__":
    main()
