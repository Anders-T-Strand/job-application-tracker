import json

from app.discovery.scorer import (
    DEFAULT_CONSTRAINTS,
    DEFAULT_PROFILE,
    load_constraints,
    load_profile,
    parse_scores,
    score_pending,
)
from tests.helpers import add_company, add_posting


def test_parse_scores_extracts_and_filters():
    raw = (
        'Here you go:\n[{"id": 1, "score": 85, "rationale": "great fit"},'
        ' {"id": 2, "score": 300, "rationale": "out of range"},'
        ' {"id": 99, "score": 50, "rationale": "not requested"}]'
    )
    scores = parse_scores(raw, expected_ids={1, 2})
    assert [(s.id, s.score) for s in scores] == [(1, 85)]


def test_parse_scores_garbage_returns_empty():
    assert parse_scores("I cannot score these postings.", {1}) == []
    assert parse_scores("[not json]", {1}) == []


def test_load_profile_falls_back_to_default(tmp_path):
    assert load_profile(tmp_path / "missing.txt") == DEFAULT_PROFILE
    resume = tmp_path / "resume.txt"
    resume.write_text("MS CS student, PyTorch, NLP research.")
    assert "PyTorch" in load_profile(resume)


class FakeLLM:
    """Implements app.llm.LLM: scores every requested posting 77."""

    def __init__(self):
        self.prompts: list[str] = []

    async def complete(self, system, user, max_tokens, schema=None):
        self.prompts.append(user)
        ids = [
            int(line.split("id=")[1].split(" ")[0])
            for line in user.splitlines()
            if line.startswith("--- posting id=")
        ]
        return json.dumps([{"id": i, "score": 77, "rationale": "fits"} for i in ids])


async def test_score_pending_batches_and_persists(conn):
    company_id = add_company(conn)
    for i in range(10):
        add_posting(conn, company_id, title=f"ML Intern {i}")
    add_posting(conn, company_id, title="Rejected", rules_status="rejected")
    add_posting(conn, company_id, title="Already scored", llm_score=90)
    add_posting(conn, company_id, title="Dead", is_live=0)

    llm = FakeLLM()
    scored = await score_pending(conn, llm, "profile text")
    assert scored == 10
    assert len(llm.prompts) == 2  # 10 postings / batch of 8
    assert "profile text" in llm.prompts[0]
    rows = conn.execute(
        "SELECT llm_score, llm_rationale FROM postings WHERE title LIKE 'ML Intern %'"
    ).fetchall()
    assert all(row["llm_score"] == 77 and row["llm_rationale"] == "fits" for row in rows)
    # rejected / dead / already-scored rows untouched
    assert (
        conn.execute("SELECT llm_score FROM postings WHERE title = 'Rejected'").fetchone()[
            "llm_score"
        ]
        is None
    )
    assert await score_pending(conn, llm, "profile text") == 0


async def test_prompt_carries_feed_sponsorship_and_missing_description(conn):
    company_id = add_company(conn)
    posting_id = add_posting(conn, company_id, description="")
    conn.execute("UPDATE postings SET visa_sponsor = 1 WHERE id = ?", (posting_id,))
    conn.commit()
    llm = FakeLLM()
    await score_pending(conn, llm, "profile text", constraints="- Graduating spring 2030.\n")
    assert "Visa: employer likely sponsors" in llm.prompts[0]
    assert "Graduating spring 2030" in llm.prompts[0]  # constraints go in alongside the résumé
    assert "(no description; judge from title" in llm.prompts[0]


async def test_default_view_terms_are_scored_first(conn):
    company_id = add_company(conn)
    later = add_posting(conn, company_id, title="ML Intern - Summer 2027")
    target = add_posting(conn, company_id, title="ML Co-op - Spring 2027")
    conn.execute("UPDATE postings SET first_seen = '2026-09-01T00:00:00Z' WHERE id = ?", (target,))
    conn.commit()
    llm = FakeLLM()
    assert await score_pending(conn, llm, "profile text", limit=1, graduation_year=2027) == 1
    assert f"posting id={target} " in llm.prompts[0] and f"id={later} " not in llm.prompts[0]


def test_constraints_come_from_the_private_profile_file(tmp_path):
    assert load_constraints(tmp_path / "missing.md") == DEFAULT_CONSTRAINTS
    profile = tmp_path / "profile.md"
    profile.write_text("- Needs remote work.")
    assert load_constraints(profile) == "- Needs remote work."
