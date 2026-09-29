"""Unit tests for the Ollama client's request payloads (no Ollama needed)."""

import json

import httpx

from app.rag.ollama_client import OllamaClient


def _client_capturing_requests(sent: list[dict]) -> OllamaClient:
    """Return a client whose HTTP calls are answered by a fake transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body = json.dumps({"message": {"content": "ok"}, "done": True}) + "\n"
        return httpx.Response(200, content=body)

    client = OllamaClient("http://ollama.test")
    client._http = httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(handler))
    return client


def test_chat_stream_sends_temperature_option():
    sent: list[dict] = []
    client = _client_capturing_requests(sent)
    assert list(client.chat_stream("m", [{"role": "user", "content": "q"}], temperature=0.0)) == ["ok"]
    assert sent[0]["options"] == {"temperature": 0.0}


def test_chat_stream_omits_options_when_temperature_unset():
    sent: list[dict] = []
    client = _client_capturing_requests(sent)
    list(client.chat_stream("m", [{"role": "user", "content": "q"}]))
    assert "options" not in sent[0]


def test_chat_stream_sends_max_tokens_as_num_predict():
    sent: list[dict] = []
    client = _client_capturing_requests(sent)
    list(client.chat_stream("m", [{"role": "user", "content": "q"}], temperature=0.0, max_tokens=256))
    assert sent[0]["options"] == {"temperature": 0.0, "num_predict": 256}
