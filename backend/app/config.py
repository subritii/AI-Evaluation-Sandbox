"""Application settings, read from environment variables (and `.env` on the host).

Inside Docker Compose the service names (`db`, `ollama`) are injected as hosts;
the defaults here target localhost for running scripts directly on the host.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres. The password has no default on purpose: it must come from .env.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "sandbox"
    postgres_password: str
    postgres_db: str = "sandbox"

    # Ollama (the only model endpoint; no cloud APIs).
    ollama_base_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    llm_model: str = "llama3.2:3b"
    # 0 = greedy decoding: the same question and context give the same answer,
    # which compliance Q&A and reproducible eval metrics both need.
    llm_temperature: float = 0.0
    # Must match the `vector(N)` column in db/schema.sql. nomic-embed-text -> 768.
    embed_dim: int = 768

    # Ingestion.
    policies_dir: Path = Path("data/policies")
    chunk_size: int = 1000
    chunk_overlap: int = 150

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
