"""Posting persistence: dedupe/upsert, liveness tracking, and the rules stage."""

import hashlib
import sqlite3

from app.discovery.ats import Posting
from app.discovery.rules import Locality, apply_rules


def content_hash(company_id: int, title: str, location: str) -> str:
    return hashlib.sha256(f"{company_id}|{title}|{location}".encode()).hexdigest()


def upsert_postings(
    conn: sqlite3.Connection,
    company_id: int,
    postings: list[Posting],
    now: str,
    feed: str | None = None,
) -> int:
    """Insert unseen postings, refresh last_seen on known ones. Returns count of new rows.

    Feed postings (feed set) match on company + title instead: feeds write locations
    differently, and the same job often appears in more than one feed. Their sponsorship
    and term tags merge rather than overwrite.
    """
    new = 0
    for posting in postings:
        digest = content_hash(company_id, posting.title, posting.location)
        if feed:
            existing = conn.execute(
                "SELECT id FROM postings WHERE company_id = ? AND lower(title) = lower(?)",
                (company_id, posting.title),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id FROM postings WHERE company_id = ? AND content_hash = ?",
                (company_id, digest),
            ).fetchone()
        if existing and feed:
            conn.execute(
                "UPDATE postings SET last_seen = ?, is_live = 1,"
                " visa_sponsor = MAX(visa_sponsor, ?), terms = COALESCE(terms, ?) WHERE id = ?",
                (now, int(posting.visa_sponsor), posting.terms or None, existing["id"]),
            )
        elif existing:
            conn.execute(
                "UPDATE postings SET last_seen = ?, is_live = 1, url = ?, external_id = ?,"
                " description = ?, visa_sponsor = ? WHERE id = ?",
                (
                    now,
                    posting.url,
                    posting.external_id,
                    posting.description,
                    int(posting.visa_sponsor),
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO postings (company_id, external_id, title, location, remote, url,"
                " description, visa_sponsor, feed, terms, content_hash, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    company_id,
                    posting.external_id,
                    posting.title,
                    posting.location,
                    int(posting.remote),
                    posting.url,
                    posting.description,
                    int(posting.visa_sponsor),
                    feed,
                    posting.terms or None,
                    digest,
                    now,
                    now,
                ),
            )
            new += 1
    return new


def mark_missing(conn: sqlite3.Connection, company_id: int, poll_time: str) -> int:
    """Flip is_live=0 on postings absent from the poll that ran at poll_time."""
    cursor = conn.execute(
        "UPDATE postings SET is_live = 0 WHERE company_id = ? AND is_live = 1 AND last_seen < ?",
        (company_id, poll_time),
    )
    return cursor.rowcount


# Chunk size for the rules pass: fetching all pending rows at once pulls every
# description into memory (a first full poll left ~34k pending and OOM'd the
# 256MB container), so fetch ids first and hydrate descriptions in batches.
RULES_BATCH = 500


def run_rules(conn: sqlite3.Connection, local: Locality | None = None) -> tuple[int, int]:
    """Apply the stage-1 filter to pending postings. Returns (passed, rejected). Commits."""
    ids = [
        row["id"] for row in conn.execute("SELECT id FROM postings WHERE rules_status = 'pending'")
    ]
    passed = rejected = 0
    for start in range(0, len(ids), RULES_BATCH):
        chunk = ids[start : start + RULES_BATCH]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT id, title, location, description, remote, terms FROM postings"
            f" WHERE id IN ({placeholders})",  # noqa: S608
            chunk,
        ).fetchall()
        for row in rows:
            ok, reason = apply_rules(
                row["title"],
                row["location"] or "",
                row["description"] or "",
                bool(row["remote"]),
                row["terms"] or "",
                local,
            )
            conn.execute(
                "UPDATE postings SET rules_status = ?, rules_reason = ? WHERE id = ?",
                ("passed" if ok else "rejected", reason or None, row["id"]),
            )
            if ok:
                passed += 1
            else:
                rejected += 1
        conn.commit()
    return passed, rejected
