import sqlite3


def add_email(
    conn: sqlite3.Connection,
    *,
    internet_message_id: str = "<msg-1@example.com>",
    graph_id: str = "g1",
    received_at: str = "2026-07-01T10:00:00Z",
    from_addr: str = "no-reply@greenhouse.io",
    subject: str = "Thank you for applying",
    snippet: str = "",
) -> int:
    cursor = conn.execute(
        "INSERT INTO emails"
        " (internet_message_id, graph_id, received_at, from_addr, subject, snippet)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (internet_message_id, graph_id, received_at, from_addr, subject, snippet),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_company(
    conn: sqlite3.Connection,
    *,
    name: str = "Acme",
    ats_type: str = "greenhouse",
    board_token: str = "acme",
    lca_filings: int = 0,
) -> int:
    cursor = conn.execute(
        "INSERT INTO companies (name, ats_type, board_token, lca_filings) VALUES (?, ?, ?, ?)",
        (name, ats_type, board_token, lca_filings),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_posting(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    title: str = "Machine Learning Intern",
    location: str = "New York, NY",
    url: str = "https://boards.greenhouse.io/acme/jobs/1",
    description: str = "Work on ML models.",
    rules_status: str = "passed",
    llm_score: int | None = None,
    status: str = "new",
    is_live: int = 1,
) -> int:
    cursor = conn.execute(
        "INSERT INTO postings"
        " (company_id, title, location, url, description, content_hash, first_seen, last_seen,"
        "  is_live, rules_status, llm_score, status)"
        " VALUES (?, ?, ?, ?, ?, ?, '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', ?, ?, ?, ?)",
        (
            company_id,
            title,
            location,
            url,
            description,
            f"hash-{company_id}-{title}-{location}",
            is_live,
            rules_status,
            llm_score,
            status,
        ),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_application(
    conn: sqlite3.Connection,
    *,
    company: str = "Acme",
    role_title: str | None = None,
    ats_domain: str | None = None,
    status: str = "applied",
    last_activity_at: str = "2026-06-20T00:00:00Z",
) -> int:
    cursor = conn.execute(
        "INSERT INTO applications"
        " (company, role_title, source, ats_domain, applied_at, status, last_activity_at)"
        " VALUES (?, ?, 'email', ?, '2026-06-20T00:00:00Z', ?, ?)",
        (company, role_title, ats_domain, status, last_activity_at),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid
