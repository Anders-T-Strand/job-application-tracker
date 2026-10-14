import json

import pytest
from fastapi.testclient import TestClient

from app import db, tailor
from app.main import create_app
from app.tailor import TailorAnalysis, analyze, parse_analysis
from tests.helpers import add_company, add_posting


@pytest.fixture
def web(settings):
    application = create_app(settings)
    with TestClient(application) as client:  # lifespan runs migrations; pollers disabled
        yield client, settings


def test_parse_analysis_extracts_and_coerces_strings():
    raw = (
        "Sure:\n"
        '{"matched": [{"skill": "PyTorch", "note": "BERT project"}, "Python"],'
        ' "missing": [{"skill": "Spark"}],'
        ' "advice": ["Lead with your LLM eval experience."]}'
    )
    result = parse_analysis(raw)
    assert result is not None
    assert [m.skill for m in result.matched] == ["PyTorch", "Python"]
    assert result.matched[1].note == ""  # bare string coerced into a SkillNote
    assert result.missing[0].skill == "Spark"
    assert result.advice == ["Lead with your LLM eval experience."]


def test_parse_analysis_garbage_returns_none():
    assert parse_analysis("I can't help with that.") is None
    assert parse_analysis("{not json}") is None


class FakeLLM:
    """Implements app.llm.LLM: echoes a fixed analysis and records the prompt."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.prompts: list[str] = []

    async def complete(self, system, user, max_tokens, schema=None):
        self.prompts.append(user)
        return json.dumps(self.payload)


async def test_analyze_sends_resume_and_posting_and_parses():
    llm = FakeLLM(
        {"matched": [{"skill": "XGBoost", "note": "Skills"}], "missing": [], "advice": []}
    )
    result = await analyze(llm, "resume: XGBoost, PyTorch", "ML Intern", "Acme", "We use XGBoost.")
    assert isinstance(result, TailorAnalysis)
    assert result.matched[0].skill == "XGBoost"
    prompt = llm.prompts[0]
    assert "resume: XGBoost, PyTorch" in prompt
    assert "We use XGBoost." in prompt
    assert "ML Intern" in prompt


def test_tailor_renders_form(web):
    client, _ = web
    response = client.get("/tailor")
    assert response.status_code == 200
    assert "Tailor résumé" in response.text
    assert 'name="description"' in response.text


def test_tailor_prefills_from_scraped_posting(web):
    client, settings = web
    conn = db.connect(settings.db_path)
    company_id = add_company(conn)
    posting_id = add_posting(
        conn, company_id, title="NLP Intern", description="Build BERT pipelines with PyTorch."
    )
    conn.close()

    response = client.get("/tailor", params={"posting_id": posting_id})
    assert response.status_code == 200
    assert "Build BERT pipelines with PyTorch." in response.text  # description in the textarea
    assert 'value="NLP Intern"' in response.text


def test_tailor_analyze_renders_results(web, monkeypatch):
    async def fake_analyze(llm, resume, title, company, description):
        return TailorAnalysis(
            matched=[tailor.SkillNote(skill="PyTorch", note="BERT project")],
            missing=[tailor.SkillNote(skill="Apache Spark", note="posting requires it")],
            advice=["Move the LLM eval project to the top."],
        )

    monkeypatch.setattr("app.tailor.analyze", fake_analyze)
    client, _ = web
    response = client.post(
        "/tailor", data={"description": "We need Spark and PyTorch.", "title": "MLE"}
    )
    assert response.status_code == 200
    assert "PyTorch" in response.text
    assert "Apache Spark" in response.text
    assert "Move the LLM eval project to the top." in response.text


def test_tailor_analyze_requires_a_description(web):
    client, _ = web
    response = client.post("/tailor", data={"description": "   "})
    assert response.status_code == 200
    assert "Paste a job description" in response.text
