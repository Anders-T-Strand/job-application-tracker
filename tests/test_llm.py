import json

import httpx
import pytest

from app.llm import AnthropicLLM, LLMError, OllamaLLM, make_llm


async def test_ollama_sends_chat_request_and_returns_content():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"a": 1}'}})

    llm = OllamaLLM("http://ollama:11434", "qwen-test", 8192, httpx.MockTransport(handler))
    schema = {"type": "object"}
    assert await llm.complete("sys", "hello", max_tokens=50, schema=schema) == '{"a": 1}'

    body = seen[0]
    assert body["model"] == "qwen-test"
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    assert body["stream"] is False and body["think"] is False
    assert body["format"] == schema
    assert body["options"] == {"temperature": 0, "num_predict": 50, "num_ctx": 8192}


async def test_ollama_errors_become_llm_error():
    llm = OllamaLLM(
        "http://ollama:11434", "qwen-test", 8192, httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(LLMError):
        await llm.complete("sys", "hello", max_tokens=50)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    down = OllamaLLM("http://ollama:11434", "qwen-test", 8192, httpx.MockTransport(refuse))
    with pytest.raises(LLMError):
        await down.complete("sys", "hello", max_tokens=50)


def test_make_llm_picks_configured_backend(settings):
    ollama = make_llm(settings.model_copy(update={"llm_provider": "ollama"}))
    assert isinstance(ollama, OllamaLLM)
    assert ollama.model == settings.ollama_model

    assert isinstance(make_llm(settings), AnthropicLLM)  # fixture: anthropic + test key
    assert make_llm(settings.model_copy(update={"anthropic_api_key": ""})) is None
    assert make_llm(settings.model_copy(update={"llm_provider": "none"})) is None
