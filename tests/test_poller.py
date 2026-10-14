from app import poller
from app.llm import LLMError
from tests.helpers import add_application, add_email


class FakeGraph:
    async def get_body_text(self, graph_id: str) -> str:
        return "We received your application."


class DownLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system, user, max_tokens, schema=None):
        self.calls += 1
        raise LLMError("connection refused")


async def _no_new_mail(conn, client):
    return 0


async def test_backend_outage_stops_the_batch_and_leaves_mail_pending(conn, settings, monkeypatch):
    for i in range(3):
        add_email(conn, internet_message_id=f"<m{i}@x>", graph_id=f"g{i}")
    monkeypatch.setattr(poller, "sync_inbox", _no_new_mail)

    llm = DownLLM()
    await poller.poll_once(settings, FakeGraph(), llm)

    assert llm.calls == 1  # gave up after the first failure instead of trying all three
    pending = conn.execute("SELECT COUNT(*) FROM emails WHERE processed = 0").fetchone()[0]
    assert pending == 3


async def test_no_backend_skips_classification(conn, settings, monkeypatch):
    add_email(conn)
    monkeypatch.setattr(poller, "sync_inbox", _no_new_mail)
    await poller.poll_once(settings, FakeGraph(), None)
    assert conn.execute("SELECT processed FROM emails").fetchone()[0] == 0


class EchoLLM:
    async def complete(self, system, user, max_tokens, schema=None):
        return '{"is_job_related": false, "type": "other", "confidence": 0.9}'


async def test_lifecycle_runs_only_once_all_mail_is_classified(conn, settings, monkeypatch):
    silent = add_application(conn, last_activity_at="2020-01-01T00:00:00Z")
    add_email(conn)
    monkeypatch.setattr(poller, "sync_inbox", _no_new_mail)

    await poller.poll_once(settings, FakeGraph(), DownLLM())  # email stays pending
    assert (
        conn.execute("SELECT status FROM applications WHERE id = ?", (silent,)).fetchone()[0]
        == "applied"
    )

    await poller.poll_once(settings, FakeGraph(), EchoLLM())  # mail drained -> rules apply
    assert (
        conn.execute("SELECT status FROM applications WHERE id = ?", (silent,)).fetchone()[0]
        == "ghosted"
    )
