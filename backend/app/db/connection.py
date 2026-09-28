"""Postgres connection helpers."""

from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from app.config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_connection() -> psycopg.Connection:
    """Open a connection with the pgvector type adapter registered.

    The `vector` extension must exist before `register_vector` can look up its
    type OID, so we ensure it here (idempotent) rather than relying on callers.
    """
    conn = psycopg.connect(get_settings().database_url)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    register_vector(conn)
    return conn


def init_schema(conn: psycopg.Connection) -> None:
    """Create tables and indexes if they don't exist yet."""
    conn.execute(SCHEMA_PATH.read_text())
    conn.commit()
