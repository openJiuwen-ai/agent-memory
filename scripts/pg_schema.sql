-- Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
-- Default production schema for the PostgreSQL KVStore and pgvector VectorStore.
-- Copy and adjust this file when schema, table names, dimension, or metric differ.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.agent_memory_kv (
    scope_org text NOT NULL,
    scope_space text NOT NULL,
    scope_user text NOT NULL,
    scope_agent text NOT NULL,
    scope_session text NOT NULL,
    key text NOT NULL,
    value bytea NOT NULL,
    expires_at double precision,
    PRIMARY KEY (
        scope_org, scope_space, scope_user, scope_agent, scope_session, key
    )
);

CREATE INDEX IF NOT EXISTS agent_memory_kv_expires_idx
    ON public.agent_memory_kv (expires_at)
    WHERE expires_at IS NOT NULL;

-- Optional maintenance statement; schedule externally if expired-row cleanup is desired.
-- DELETE FROM public.agent_memory_kv
-- WHERE expires_at IS NOT NULL
--   AND expires_at <= extract(epoch from clock_timestamp());

CREATE TABLE IF NOT EXISTS public.agent_memory_vectors (
    id text NOT NULL,
    embedding vector(1024) NOT NULL,
    scope_org text NOT NULL DEFAULT '',
    scope_space text NOT NULL DEFAULT '',
    scope_user text NOT NULL DEFAULT '',
    scope_agent text NOT NULL DEFAULT '',
    scope_session text NOT NULL DEFAULT '',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (
        scope_org, scope_space, scope_user, scope_agent, scope_session, id
    )
);

CREATE TABLE IF NOT EXISTS public.agent_memory_vectors_l0 (
    id text NOT NULL,
    embedding vector(1024) NOT NULL,
    scope_org text NOT NULL DEFAULT '',
    scope_space text NOT NULL DEFAULT '',
    scope_user text NOT NULL DEFAULT '',
    scope_agent text NOT NULL DEFAULT '',
    scope_session text NOT NULL DEFAULT '',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (
        scope_org, scope_space, scope_user, scope_agent, scope_session, id
    )
);

CREATE TABLE IF NOT EXISTS public.agent_memory_vectors_l1 (
    id text NOT NULL,
    embedding vector(1024) NOT NULL,
    scope_org text NOT NULL DEFAULT '',
    scope_space text NOT NULL DEFAULT '',
    scope_user text NOT NULL DEFAULT '',
    scope_agent text NOT NULL DEFAULT '',
    scope_session text NOT NULL DEFAULT '',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (
        scope_org, scope_space, scope_user, scope_agent, scope_session, id
    )
);

CREATE INDEX IF NOT EXISTS agent_memory_vectors_scope_idx
    ON public.agent_memory_vectors (
        scope_org, scope_space, scope_user, scope_agent, scope_session
    );
CREATE INDEX IF NOT EXISTS agent_memory_vectors_metadata_idx
    ON public.agent_memory_vectors USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS agent_memory_vectors_embedding_hnsw_idx
    ON public.agent_memory_vectors USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS agent_memory_vectors_l0_scope_idx
    ON public.agent_memory_vectors_l0 (
        scope_org, scope_space, scope_user, scope_agent, scope_session
    );
CREATE INDEX IF NOT EXISTS agent_memory_vectors_l0_metadata_idx
    ON public.agent_memory_vectors_l0 USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS agent_memory_vectors_l0_embedding_hnsw_idx
    ON public.agent_memory_vectors_l0 USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS agent_memory_vectors_l1_scope_idx
    ON public.agent_memory_vectors_l1 (
        scope_org, scope_space, scope_user, scope_agent, scope_session
    );
CREATE INDEX IF NOT EXISTS agent_memory_vectors_l1_metadata_idx
    ON public.agent_memory_vectors_l1 USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS agent_memory_vectors_l1_embedding_hnsw_idx
    ON public.agent_memory_vectors_l1 USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
