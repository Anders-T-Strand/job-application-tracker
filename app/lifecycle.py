"""Status lifecycle rules, applied after each poll cycle once all mail is classified.

- an open application (applied/oa/screen/interview) with no activity for 60 days -> ghosted
- ghosted for 90 days -> rejected
- rejected for 60 days -> hidden from the board and table (kept for stats)

Time in a status runs from the last move into it, or from later activity (a matched email
or a manual change), whichever is newer. Automatic moves are recorded in status_history as
'auto' and leave last_activity_at alone, so "last activity" keeps meaning real activity.
A move restarts the clock, so a long-silent application is ghosted first and only goes
further after the next waiting period. New activity un-hides (see matcher).
"""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from app.models import OPEN_STATUSES

log = logging.getLogger(__name__)

GHOST_AFTER_DAYS = 60
REJECT_GHOSTED_AFTER_DAYS = 90
HIDE_REJECTED_AFTER_DAYS = 60


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _move(conn: sqlite3.Connection, row: sqlite3.Row, new_status: str, stamp: str) -> None:
    conn.execute(
        "INSERT INTO status_history"
        " (application_id, old_status, new_status, changed_at, triggered_by)"
        " VALUES (?, ?, ?, ?, 'auto')",
        (row["id"], row["status"], new_status, stamp),
    )
    conn.execute("UPDATE applications SET status = ? WHERE id = ?", (new_status, row["id"]))


def apply_rules(conn: sqlite3.Connection, now: datetime | None = None) -> tuple[int, int, int]:
    """Apply the lifecycle rules. Returns (ghosted, rejected, hidden). Commits."""
    now = now or datetime.now(UTC)
    ghost_before = _iso(now - timedelta(days=GHOST_AFTER_DAYS))
    reject_before = _iso(now - timedelta(days=REJECT_GHOSTED_AFTER_DAYS))
    hide_before = _iso(now - timedelta(days=HIDE_REJECTED_AFTER_DAYS))
    rows = conn.execute(
        "SELECT a.id, a.status,"
        " MAX(a.last_activity_at, COALESCE((SELECT MAX(h.changed_at) FROM status_history h"
        "  WHERE h.application_id = a.id AND h.new_status = a.status), '')) AS since"
        " FROM applications a WHERE a.hidden_at IS NULL"
    ).fetchall()
    ghosted = rejected = hidden = 0
    stamp = _iso(now)
    for row in rows:
        if row["status"] in OPEN_STATUSES and row["since"] < ghost_before:
            _move(conn, row, "ghosted", stamp)
            ghosted += 1
        elif row["status"] == "ghosted" and row["since"] < reject_before:
            _move(conn, row, "rejected", stamp)
            rejected += 1
        elif row["status"] == "rejected" and row["since"] < hide_before:
            conn.execute("UPDATE applications SET hidden_at = ? WHERE id = ?", (stamp, row["id"]))
            hidden += 1
    conn.commit()
    if ghosted or rejected or hidden:
        log.info("lifecycle: %d ghosted, %d rejected, %d hidden", ghosted, rejected, hidden)
    return ghosted, rejected, hidden
