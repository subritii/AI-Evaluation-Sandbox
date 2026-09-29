-- Idempotent schema: safe to run on every ingest.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_chunks (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT        NOT NULL,              -- file name the chunk came from
    chunk_index  INTEGER     NOT NULL,              -- position within that file
    content      TEXT        NOT NULL,              -- chunk text AFTER the Trust Engine
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- 768 = nomic-embed-text output size. Changing embedding model means
    -- changing this dimension and re-ingesting everything.
    embedding    vector(768) NOT NULL,
    embed_model  TEXT        NOT NULL,
    -- Which scrubber processed `content` before embedding (rule: anonymize
    -- before embedding). Makes any un-scrubbed rows auditable with plain SQL.
    scrubbed_by  TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, chunk_index)
);

-- HNSW = approximate nearest-neighbour graph index. vector_cosine_ops makes it
-- serve the `<=>` (cosine distance) operator used in retrieval.
CREATE INDEX IF NOT EXISTS document_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops);

-- Per-stage request timings from the gateway (Task 4). One row per stage per
-- request ("long" format), so percentiles per stage are a simple WHERE, and a
-- new stage needs no schema change. Holds no request text, by design.
CREATE TABLE IF NOT EXISTS latency_samples (
    id          BIGSERIAL        PRIMARY KEY,
    -- Groups a batch; warmup requests use a separate "<run>-warmup" run_id.
    run_id      TEXT             NOT NULL,
    request_id  UUID             NOT NULL,
    stage       TEXT             NOT NULL
        CHECK (stage IN ('pii_scan', 'retrieval', 'time_to_first_token', 'generation', 'total')),
    ms          DOUBLE PRECISION NOT NULL CHECK (ms >= 0),
    created_at  TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS latency_samples_run_stage ON latency_samples (run_id, stage);
