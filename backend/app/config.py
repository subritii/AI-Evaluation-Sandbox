"""Application settings, read from environment variables (and `.env` on the host).

Inside Docker Compose the service names (`db`, `ollama`) are injected as hosts;
the defaults here target localhost for running scripts directly on the host.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres. The password has no default on purpose: it must come from .env.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "sandbox"
    postgres_password: str
    postgres_db: str = "sandbox"

    # Which API serves each model (Task 9). "ollama" = Ollama's native API at
    # OLLAMA_BASE_URL. "openai_compatible" = any server speaking the OpenAI
    # /chat/completions and /embeddings API at OPENAI_BASE_URL (vLLM,
    # llama.cpp, LM Studio, a private Azure OpenAI deployment, Ollama's /v1).
    # Set separately so embeddings can stay local while generation moves.
    llm_provider: Literal["ollama", "openai_compatible"] = "ollama"
    embed_provider: Literal["ollama", "openai_compatible"] = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    # e.g. http://vllm:8000/v1 (inside the sandbox network keeps the air-gap).
    openai_base_url: str = ""
    # SecretStr: masked in reprs and never returned by /info or written to reports.
    openai_api_key: SecretStr | None = None
    # Where the model server runs: "docker" (a container on this machine; CPU
    # only on macOS), "native" (on the host; Apple GPU), or "remote" (another
    # machine). Latency differs by an order of magnitude, so every measurement
    # records it. URLs can't tell reliably (both local options answer on
    # localhost:11434 from the host), so the compose files set it; unset stays
    # "unknown".
    model_backend: Literal["docker", "native", "remote", "unknown"] = "unknown"
    embed_model: str = "nomic-embed-text"
    llm_model: str = "llama3.2:3b"
    # 0 = greedy decoding: the same question and context give the same answer,
    # which compliance Q&A and reproducible eval metrics both need.
    llm_temperature: float = 0.0
    # Cap on generated tokens, so one runaway answer can't dominate tail latency.
    llm_max_tokens: int = 256
    # Must match the `vector(N)` column in db/schema.sql. nomic-embed-text -> 768.
    embed_dim: int = 768

    # Ingestion.
    policies_dir: Path = Path("data/policies")
    chunk_size: int = 1000
    chunk_overlap: int = 150

    @model_validator(mode="after")
    def _openai_endpoint_configured(self) -> "Settings":
        if "openai_compatible" in (self.llm_provider, self.embed_provider) and not self.openai_base_url:
            raise ValueError("OPENAI_BASE_URL is required when LLM_PROVIDER or EMBED_PROVIDER is openai_compatible")
        return self

    @property
    def database_url(self) -> str:
        """libpq connection string for psycopg."""
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user} "
            f"password={self.postgres_password}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded lazily on first use.

    Lazy loading lets pure unit tests (e.g. chunking) import modules without
    database credentials being set.
    """
    return Settings()
