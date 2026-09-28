"""Minimal HTTP client for the local Ollama server.

Ollama is the only model endpoint in this project: embeddings and generation
both stay on the machine. We call its REST API directly with httpx (rather
than a LangChain wrapper) so every request and timing is explicit.
"""

import json
from collections.abc import Iterator

import httpx


class OllamaError(RuntimeError):
    """Raised when Ollama returns an error (e.g. model not pulled)."""


class OllamaClient:
    def __init__(self, base_url: str, timeout_s: float = 300.0):
        # Long read timeout: CPU-only generation in Docker on a Mac is slow.
        self._http = httpx.Client(
            base_url=base_url, timeout=httpx.Timeout(timeout_s, connect=5.0)
        )

    def close(self) -> None:
        self._http.close()

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text (batched in one request)."""
        response = self._http.post("/api/embed", json={"model": model, "input": texts})
        _raise_for_ollama_error(response)
        return response.json()["embeddings"]

    def chat_stream(self, model: str, messages: list[dict]) -> Iterator[str]:
        """Stream a chat completion, yielding text fragments as they arrive.

        Streaming lets callers measure time-to-first-token separately from
        total generation time.
        """
        payload = {"model": model, "messages": messages, "stream": True}
        with self._http.stream("POST", "/api/chat", json=payload) as response:
            if response.is_error:
                response.read()
                _raise_for_ollama_error(response)
            # Ollama streams newline-delimited JSON objects.
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if "error" in event:
                    raise OllamaError(event["error"])
                fragment = event.get("message", {}).get("content", "")
                if fragment:
                    yield fragment
                if event.get("done"):
                    break


def _raise_for_ollama_error(response: httpx.Response) -> None:
    """Turn an HTTP error into an OllamaError carrying Ollama's own message."""
    if response.is_error:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        hint = " (did you run ./scripts/pull_models.sh?)" if response.status_code == 404 else ""
        raise OllamaError(f"Ollama {response.status_code}: {detail}{hint}")
