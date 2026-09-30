"""Model clients: Ollama's native API or any OpenAI-compatible endpoint, chosen per model.

    LLM_PROVIDER / EMBED_PROVIDER = ollama              -> Ollama at OLLAMA_BASE_URL
                                  = openai_compatible   -> OPENAI_BASE_URL (/chat/completions, /embeddings)

"OpenAI-compatible" means the request/response shape, not OpenAI the company:
vLLM, llama.cpp's server, LM Studio, TGI, a private Azure OpenAI deployment, or
Ollama's own /v1 API all speak it. Everything upstream is unchanged: the
pipeline masks the question with the Trust Engine before calling `embed` or
`chat_stream`, whichever provider sits behind them.

Where the endpoint runs decides network isolation, not this code: an endpoint
inside the Compose `sandbox` network keeps the air-gap; one outside it needs
the external-endpoint override, which gives the backend a route out.
"""

import json
from collections.abc import Iterator
from typing import Protocol

import httpx

from app.config import Settings
from app.rag.errors import ModelServerError
from app.rag.ollama_client import OllamaClient


class ChatEmbedClient(Protocol):
    """What the pipeline needs from a model server."""

    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    def chat_stream(
        self, model: str, messages: list[dict], temperature: float | None = None, max_tokens: int | None = None
    ) -> Iterator[str]: ...

    def close(self) -> None: ...


class OpenAICompatibleClient:
    """Client for /chat/completions (streamed) and /embeddings on an OpenAI-compatible server."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout_s: float = 300.0):
        # The key goes in a header only; httpx never includes headers in errors or logs.
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=httpx.Timeout(timeout_s, connect=5.0))

    def close(self) -> None:
        self._http.close()

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """One vector per input, in input order (the API returns an `index` per item)."""
        response = self._http.post("embeddings", json={"model": model, "input": texts})
        _raise_for_error(response)
        items = sorted(response.json()["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in items]

    def chat_stream(
        self, model: str, messages: list[dict], temperature: float | None = None, max_tokens: int | None = None
    ) -> Iterator[str]:
        """Stream a chat completion as text fragments (server-sent events, `data: {...}` lines)."""
        payload: dict = {"model": model, "messages": messages, "stream": True}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        with self._http.stream("POST", "chat/completions", json=payload) as response:
            if response.is_error:
                response.read()
                _raise_for_error(response)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue  # blank keep-alives, comments, `event:` lines
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                if "error" in event:
                    raise ModelServerError(f"Model endpoint error: {_error_text(event)}")
                for choice in event.get("choices", []):
                    fragment = (choice.get("delta") or {}).get("content")
                    if fragment:
                        yield fragment


def _error_text(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_error:
        try:
            detail = _error_text(response.json())
        except ValueError:
            detail = response.text[:300]
        raise ModelServerError(f"Model endpoint {response.status_code}: {detail}")


class ModelClient:
    """Routes embeddings and generation to their configured providers (possibly different)."""

    def __init__(self, embedder: ChatEmbedClient, generator: ChatEmbedClient):
        self._embedder = embedder
        self._generator = generator

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        return self._embedder.embed(model, texts)

    def chat_stream(
        self, model: str, messages: list[dict], temperature: float | None = None, max_tokens: int | None = None
    ) -> Iterator[str]:
        return self._generator.chat_stream(model, messages, temperature=temperature, max_tokens=max_tokens)

    def close(self) -> None:
        self._embedder.close()
        if self._generator is not self._embedder:
            self._generator.close()


def build_model_client(settings: Settings) -> ModelClient:
    """One client per distinct provider, shared when embeddings and generation use the same one."""
    clients: dict[str, ChatEmbedClient] = {}

    def client_for(provider: str) -> ChatEmbedClient:
        if provider not in clients:
            if provider == "ollama":
                clients[provider] = OllamaClient(settings.ollama_base_url)
            else:
                key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
                clients[provider] = OpenAICompatibleClient(settings.openai_base_url, api_key=key or None)
        return clients[provider]

    return ModelClient(embedder=client_for(settings.embed_provider), generator=client_for(settings.llm_provider))


def endpoint_url(settings: Settings, provider: str) -> str:
    """The URL a provider is called at, without any credentials, for reports."""
    url = httpx.URL(settings.ollama_base_url if provider == "ollama" else settings.openai_base_url)
    return str(url.copy_with(username=None, password=None))
