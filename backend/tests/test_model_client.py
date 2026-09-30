"""Model client selection and the OpenAI-compatible client (no server needed: httpx.MockTransport)."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rag.errors import ModelServerError
from app.rag.model_client import ModelClient, OpenAICompatibleClient, build_model_client, endpoint_url
from app.rag.ollama_client import OllamaClient, OllamaError

KEY = "sk-test-not-a-real-key"


def settings(**overrides) -> Settings:
    return Settings(postgres_password="x", _env_file=None, **overrides)


def client_with(handler, api_key: str | None = KEY) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient("http://llm.test/v1", api_key=api_key)
    headers = client._http.headers  # keep the auth header the constructor set
    client._http = httpx.Client(base_url="http://llm.test/v1", headers=headers, transport=httpx.MockTransport(handler))
    return client


def sse(*events: str) -> bytes:
    return "".join(f"data: {e}\n\n" for e in events).encode()


# --- Selection --------------------------------------------------------------------

def test_default_is_ollama_for_both():
    client = build_model_client(settings())
    assert isinstance(client._embedder, OllamaClient) and client._generator is client._embedder


def test_providers_are_chosen_independently():
    client = build_model_client(settings(llm_provider="openai_compatible", openai_base_url="http://llm.test/v1"))
    assert isinstance(client._embedder, OllamaClient)
    assert isinstance(client._generator, OpenAICompatibleClient)


def test_openai_provider_requires_a_base_url():
    with pytest.raises(ValidationError, match="OPENAI_BASE_URL is required"):
        settings(embed_provider="openai_compatible")


def test_secrets_are_never_shown():
    s = Settings(postgres_password="db-secret-value", _env_file=None, llm_provider="openai_compatible",
                 openai_base_url="http://llm.test/v1", openai_api_key=KEY)
    for secret in (KEY, "db-secret-value"):
        assert secret not in repr(s) and secret not in str(s.model_dump())
    assert "password=db-secret-value" in s.database_url  # still usable where it's needed


def test_endpoint_url_strips_credentials():
    s = settings(llm_provider="openai_compatible", openai_base_url="https://user:secret@llm.example/v1",
                 ollama_base_url="http://ollama:11434")
    assert endpoint_url(s, "openai_compatible") == "https://llm.example/v1"
    assert endpoint_url(s, "ollama") == "http://ollama:11434"


def test_router_sends_embeddings_and_chat_to_their_own_clients():
    calls = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def embed(self, model, texts):
            calls.append((self.name, "embed"))
            return [[0.0]]

        def chat_stream(self, model, messages, temperature=None, max_tokens=None):
            calls.append((self.name, "chat"))
            yield "x"

        def close(self):
            calls.append((self.name, "close"))

    router = ModelClient(embedder=Fake("E"), generator=Fake("G"))
    router.embed("m", ["t"])
    list(router.chat_stream("m", []))
    router.close()
    assert calls == [("E", "embed"), ("G", "chat"), ("E", "close"), ("G", "close")]


# --- OpenAI-compatible client --------------------------------------------------------

def test_embed_posts_to_embeddings_with_auth_and_orders_by_index():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]})

    assert client_with(handler).embed("nomic-embed-text", ["a", "b"]) == [[1.0], [2.0]]
    assert seen["url"] == "http://llm.test/v1/embeddings"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"] == {"model": "nomic-embed-text", "input": ["a", "b"]}


def test_no_auth_header_without_a_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client_with(handler, api_key=None).embed("m", ["a"])
    assert seen["auth"] is None


def test_chat_stream_parses_server_sent_events():
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        chunk = lambda text: json.dumps({"choices": [{"delta": {"content": text}}]})  # noqa: E731
        body = sse(json.dumps({"choices": [{"delta": {"role": "assistant"}}]}), chunk("Card data "),
                   chunk(""), chunk("must never be stored."), "[DONE]", chunk("after done is ignored"))
        return httpx.Response(200, content=b": keep-alive\n\n" + body)

    messages = [{"role": "user", "content": "q"}]
    out = list(client_with(handler).chat_stream("llama3.2:3b", messages, temperature=0.0, max_tokens=256))
    assert out == ["Card data ", "must never be stored."]
    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["body"] == {"model": "llama3.2:3b", "messages": messages, "stream": True,
                            "temperature": 0.0, "max_tokens": 256}


def test_chat_stream_omits_unset_options():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse("[DONE]"))

    list(client_with(handler).chat_stream("m", []))
    assert "temperature" not in seen["body"] and "max_tokens" not in seen["body"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, json={"error": {"message": "Invalid API key"}}), "401: Invalid API key"),
        (httpx.Response(404, json={"error": "model not found"}), "404: model not found"),
        (httpx.Response(500, text="upstream exploded"), "500: upstream exploded"),
    ],
)
def test_http_errors_become_model_server_errors_without_the_key(response, message):
    client = client_with(lambda request: response)
    for call in (lambda: client.embed("m", ["a"]), lambda: list(client.chat_stream("m", []))):
        with pytest.raises(ModelServerError, match=message) as exc:
            call()
        assert KEY not in str(exc.value)


def test_error_event_mid_stream_raises():
    client = client_with(lambda r: httpx.Response(200, content=sse(json.dumps({"error": {"message": "overloaded"}}))))
    with pytest.raises(ModelServerError, match="overloaded"):
        list(client.chat_stream("m", []))


def test_ollama_errors_are_model_server_errors():
    # The gateway catches ModelServerError for both providers.
    assert issubclass(OllamaError, ModelServerError)
