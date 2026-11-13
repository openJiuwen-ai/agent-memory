-- Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
-- F09 逻辑分片 DDL 模板：按分片列表为每个分片建一套物理表。
--
-- 用法：`scripts/migrate_kv_by_shard.py --config prod.yaml --print-ddl`
-- 会把本文件中的分片占位符替换成 `shard_resolver.shards` 中的每个分片名后输出，
-- 再用 `psql -f` 执行。**不要手工执行本文件本身**（分片占位符未展开）。
--
-- 覆盖范围：
--   1. KV 真源分片表（每个分片一张，列结构与单表一致）；
--   2. 归属存储专用内联表（全部分片共用一张，不在本文件里按分片复制）；
--   3. pgvector 向量三层表（default / layers_l0 / layers_l1）；
--   4. Elasticsearch 全文三层索引（SQL 无法创建，见文件末尾说明）。
-- 实体 / 图索引的物理结构由各自后端声明；shared 模式不需要每分片建表。

CREATE EXTENSION IF NOT EXISTS vector;

-- 1. KV 真源分片表：与 agent_memory_kv 同构，数据 key 保持 /memory/{unit_id}。
CREATE TABLE IF NOT EXISTS public.agent_memory_kv_{{shard}} (
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

CREATE INDEX IF NOT EXISTS agent_memory_kv_{{shard}}_expires_idx
    ON public.agent_memory_kv_{{shard}} (expires_at)
    WHERE expires_at IS NOT NULL;

-- 2. 归属存储专用内联表：只建一次（不是每分片一张）。
--    值 = {"shard","status","token","created_at"}；同一 scope 同一 key 至多一行，
--    所以主键就是五段 scope + key。
CREATE TABLE IF NOT EXISTS public.agent_memory_shard_placement (
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

-- 3. pgvector 向量三层表：维度与 metric 必须与部署的 Embedder 一致（示例 1024）。
CREATE TABLE IF NOT EXISTS public.agent_memory_vectors_{{shard}} (
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
CREATE INDEX IF NOT EXISTS agent_memory_vectors_{{shard}}_metadata_idx
    ON public.agent_memory_vectors_{{shard}} USING gin (metadata);

CREATE TABLE IF NOT EXISTS public.agent_memory_vectors_{{shard}}_l0 (
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
CREATE TABLE IF NOT EXISTS public.agent_memory_vectors_{{shard}}_l1 (
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

-- 4. Elasticsearch 全文三层索引按 `fulltext_store.*.params.index` 的模板创建：
--      memory_fulltext_{{shard}} / memory_fulltext_{{shard}}_l0 / memory_fulltext_{{shard}}_l1
--    mapping 与单索引一致；`ensure_index` 由后端在首次写入时创建。
--    实体 shared 模式只需一张 memory_entities；split 模式每分片一张 memory_entities_{{shard}}。
