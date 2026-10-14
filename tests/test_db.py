from app import db


def test_migrations_are_idempotent(conn):
    assert db.migrate(conn) == []  # conftest already migrated
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"applications", "emails", "status_history", "sync_state"} <= tables


def test_sync_state_roundtrip(conn):
    assert db.get_state(conn, "delta_link") is None
    db.set_state(conn, "delta_link", "https://example/delta?token=abc")
    assert db.get_state(conn, "delta_link") == "https://example/delta?token=abc"
    db.set_state(conn, "delta_link", "https://example/delta?token=def")
    assert db.get_state(conn, "delta_link") == "https://example/delta?token=def"
    db.set_state(conn, "delta_link", None)
    assert db.get_state(conn, "delta_link") is None


def test_feed_migrations_keep_postings(tmp_path):
    """004 and 005 rebuild companies; with foreign keys on, that would cascade-delete postings."""
    conn = db.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
    for name in ("001_initial.sql", "002_discovery.sql", "003_hidden.sql", "004_zapply_feed.sql"):
        conn.executescript((db.MIGRATIONS_DIR / name).read_text())
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT INTO companies (name, ats_type, board_token) VALUES ('Acme', 'lever', 'a')"
    )
    conn.execute("INSERT INTO companies (name, ats_type, board_token) VALUES ('KLA', 'zapply', '')")
    for company_id in (1, 2):
        conn.execute(
            "INSERT INTO postings (company_id, title, url, content_hash, first_seen, last_seen,"
            " rules_status, llm_score) VALUES (?, 'ML Intern', 'https://x', 'h', '2026-10-01',"
            " '2026-10-01', 'passed', 80)",
            (company_id,),
        )
    conn.commit()

    assert db.migrate(conn) == ["005_feeds.sql"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    rows = conn.execute(
        "SELECT c.ats_type, p.feed, p.terms, p.rules_status, p.llm_score FROM postings p"
        " JOIN companies c ON c.id = p.company_id ORDER BY p.id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("lever", None, None, "pending", None),  # open postings are re-filtered and re-scored
        ("feed", "zapply-intern", "Internship", "pending", None),
    ]
    conn.close()
