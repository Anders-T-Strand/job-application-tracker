import json

from app.classifier.llm import classify_email, extract_job_posting, parse_classification

VALID = {
    "is_job_related": True,
    "type": "rejection",
    "company": "Acme",
    "role_title": "Backend Engineer",
    "confidence": 0.95,
}


def test_parse_clean_json():
    cls = parse_classification(json.dumps(VALID))
    assert cls is not None
    assert cls.type == "rejection"
    assert cls.company == "Acme"


def test_parse_fenced_json():
    cls = parse_classification(f"```json\n{json.dumps(VALID)}\n```")
    assert cls is not None
    assert cls.confidence == 0.95


def test_parse_json_wrapped_in_prose():
    cls = parse_classification(f"Here is the classification: {json.dumps(VALID)} Hope that helps!")
    assert cls is not None
    assert cls.is_job_related is True


def test_parse_garbage_returns_none():
    assert parse_classification("I cannot classify this email.") is None


def test_parse_invalid_type_returns_none():
    assert parse_classification(json.dumps({**VALID, "type": "spam"})) is None


def test_parse_out_of_range_confidence_returns_none():
    assert parse_classification(json.dumps({**VALID, "confidence": 1.5})) is None


class FakeLLM:
    """Implements app.llm.LLM with a canned reply."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.last_call: dict | None = None

    async def complete(self, system, user, max_tokens, schema=None):
        self.last_call = {"user": user, "max_tokens": max_tokens, "schema": schema}
        return self._text


async def test_extract_job_posting_parses_company_and_role():
    llm = FakeLLM('{"company": "Acme", "role_title": "ML Engineer"}')
    posting = await extract_job_posting(
        llm, "https://boards.greenhouse.io/acme/jobs/123", "ML Engineer - Acme"
    )
    assert posting is not None
    assert posting.company == "Acme"
    assert posting.role_title == "ML Engineer"


async def test_extract_job_posting_garbage_returns_none():
    llm = FakeLLM("I could not determine anything.")
    assert await extract_job_posting(llm, "https://x", "title") is None


async def test_classify_email_truncates_body_and_parses():
    llm = FakeLLM(json.dumps(VALID))
    cls = await classify_email(llm, "hr@acme.com", "Update", "x" * 5000)
    assert cls is not None and cls.type == "rejection"
    assert llm.last_call is not None
    assert len(llm.last_call["user"]) < 2000  # 1500-char body cap plus headers
    assert llm.last_call["max_tokens"] == 300
    assert "confidence" in llm.last_call["schema"]["properties"]
