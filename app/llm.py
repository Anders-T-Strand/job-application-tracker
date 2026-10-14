"""LLM backends behind one call: a local Ollama model (default) or the Anthropic API."""

from typing import Protocol

import anthropic
import httpx

from app.config import Settings

# Generous read timeout: the first call after Ollama unloads the model pays a ~15s load.
OLLAMA_TIMEOUT = httpx.Timeout(300, connect=5)


class LLMError(Exception):
    """Backend unreachable or erroring — transient, so callers leave work for the next cycle."""


class LLM(Protocol):
    async def complete(
        self, system: str, user: str, max_tokens: int, schema: dict | None = None
    ) -> str:
        """Return the model's text reply. `schema` (JSON Schema) constrains output if supported."""
        ...


class OllamaLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        num_ctx: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.num_ctx = num_ctx
        self._transport = transport

    async def complete(
        self, system: str, user: str, max_tokens: int, schema: dict | None = None
    ) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,  # Qwen3 reasons by default; these are short extraction tasks
            # num_ctx is explicit because Ollama's default window silently truncates
            # the discovery scorer's batched prompts.
            "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": self.num_ctx},
        }
        if schema is not None:
            payload["format"] = schema
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=OLLAMA_TIMEOUT, transport=self._transport
            ) as http:
                response = await http.post("/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama ({self.model}): {exc!r}") from exc
        return response.json()["message"]["content"]


class AnthropicLLM:
    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self, system: str, user: str, max_tokens: int, schema: dict | None = None
    ) -> str:
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIError as exc:
            raise LLMError(f"anthropic ({self.model}): {exc}") from exc
        return "".join(block.text for block in response.content if block.type == "text")


def make_llm(settings: Settings) -> LLM | None:
    """The configured backend, or None when LLM features should be skipped."""
    if settings.llm_provider == "ollama":
        return OllamaLLM(settings.ollama_url, settings.ollama_model, settings.ollama_num_ctx)
    if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicLLM(settings.anthropic_api_key, settings.anthropic_model)
    return None
