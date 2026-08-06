-- 配置中心表（功能2/3）：模板 + 租户快照 + 实例配置 + 审计。GaussDB / openGauss 方言，PG 兼容。
-- 参考 §5.4.4 模板存储表、§5.5 配置版本管理表。
-- 2026-07-17 P0-3 v2: 1 tenant = 1 scope (UUID 同体), 改用 tenant_scope_configs 1:1 替代 scope_registry N

-- -----------------------------------------------------
-- 1. tenant_scope_configs: 租户级 Scope 配置快照 (1 tenant = 1 scope, UUID 同体)
-- 替代 scope_registry + scope_configs + scope_admins + tenant_default_configs + sys_default_configs
-- 每行 = 一个租户的"应用配置快照"，可通过应用 SCOPE 模板生成
-- 偏离检测：template_version vs current_version 不一致即"已偏离"
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_scope_configs (
    tenant_id          VARCHAR(64)  PRIMARY KEY,          -- UUID, 与 tenants.id 同体
    tenant_name        VARCHAR(128) NOT NULL,             -- 冗余 denormalize 方便查
    instance_id        VARCHAR(64)  NOT NULL DEFAULT 'default',
    config_json        TEXT         NOT NULL,             -- 完整配置 JSON (租户可改)
    template_id        VARCHAR(64),                       -- 应用的模板 ID (NULL = 未应用任何模板)
    template_version   INTEGER      DEFAULT 0,            -- 应用时模板版本 (用于偏离检测)
    current_version    INTEGER      NOT NULL DEFAULT 1,   -- 租户自己改一次 +1
    updated_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_by         VARCHAR(64)
);
COMMENT ON TABLE tenant_scope_configs IS '租户级 Scope 配置快照表（1 tenant = 1 scope）';
CREATE INDEX IF NOT EXISTS idx_tenant_scope_instance ON tenant_scope_configs(instance_id);
CREATE INDEX IF NOT EXISTS idx_tenant_scope_template ON tenant_scope_configs(template_id);

CREATE TABLE IF NOT EXISTS config_templates (
    id              VARCHAR(64)  NOT NULL,
    template_name   VARCHAR(64)  NOT NULL,
    display_name    VARCHAR(128) NOT NULL,
    description     TEXT,
    template_type   VARCHAR(16)  NOT NULL DEFAULT 'SCOPE',
    config_json     TEXT         NOT NULL,
    is_builtin      SMALLINT     DEFAULT 0,               -- 0=自定义, 1=预置
    parent_id       VARCHAR(64),                       -- 复制来源模板 (NULL=原创)
    version         INTEGER      NOT NULL DEFAULT 1,
    status          VARCHAR(16)  NOT NULL DEFAULT 'published', -- published=已发布, draft=草稿
    created_by      VARCHAR(64)  NOT NULL,
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE(template_name, template_type)
);
COMMENT ON TABLE config_templates IS '配置模板表（Scope级+内核级统一存储）';
CREATE INDEX IF NOT EXISTS idx_template_type ON config_templates(template_type);
CREATE INDEX IF NOT EXISTS idx_template_parent ON config_templates(parent_id);
CREATE INDEX IF NOT EXISTS idx_template_status ON config_templates(status);

CREATE TABLE IF NOT EXISTS template_scope_bindings (
    id              VARCHAR(64)  NOT NULL,
    template_id     VARCHAR(64)  NOT NULL,
    admin_user_id   VARCHAR(64)  NOT NULL,
    scope_name      VARCHAR(128) NOT NULL,
    bound_by        VARCHAR(64)  NOT NULL,
    bound_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE(admin_user_id, scope_name)
);
CREATE INDEX IF NOT EXISTS idx_tpl_binding_tpl ON template_scope_bindings(template_id);

CREATE TABLE IF NOT EXISTS config_versions (
    id              VARCHAR(64)  NOT NULL,
    admin_user_id   VARCHAR(64)  NOT NULL,
    scope_name      VARCHAR(128) NOT NULL,
    version         INT          NOT NULL,
    config_json     TEXT         NOT NULL,
    template_id     VARCHAR(64),
    change_type     VARCHAR(16)  NOT NULL,
    changed_by      VARCHAR(64)  NOT NULL,
    changed_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    change_reason   TEXT,
    is_current      BOOLEAN      DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE(admin_user_id, scope_name, version)
);
CREATE INDEX IF NOT EXISTS idx_cfg_ver_admin_scope ON config_versions(admin_user_id, scope_name, is_current);

CREATE TABLE IF NOT EXISTS config_audit_logs (
    id              VARCHAR(64)  NOT NULL,
    operator_id     VARCHAR(64)  NOT NULL,                -- 操作人
    tenant_id       TEXT,                                   -- 涉及租户 (NULL=平台级)
    template_id     VARCHAR(64),                           -- 涉及模板
    instance_id     VARCHAR(64) NOT NULL DEFAULT 'default',
    operation       VARCHAR(64)  NOT NULL,
    before_value    TEXT,                                   -- before JSON
    after_value     TEXT,                                   -- after JSON
    success         BOOLEAN      NOT NULL,
    error_message   TEXT,
    operated_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    reason          TEXT                                    -- 操作原因
    ,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_cfg_audit_tenant_time ON config_audit_logs(tenant_id, operated_at);

-- -----------------------------------------------------
-- 6. 预置数据：3 个 SCOPE 模板 + 2 个 INSTANCE 模板
-- -----------------------------------------------------
-- 6.1 基础版（SCOPE 模板）
INSERT INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, parent_id, version, created_by, updated_at)
SELECT 'tpl_scope_basic', 'scope_basic', '基础版', '适用于一般 agent，单 LLM + 轻量配置', 'SCOPE',
   '{"model_cfg":{"model":"qwen-plus","temperature":0.1,"max_tokens":2000},"model_client_cfg":{"client_provider":"SiliconFlow","api_key":"","api_base":"https://api.siliconflow.cn/v1","verify_ssl":false,"timeout":90.0},"embedding_cfg":{"model_name":"BAAI/bge-m3","api_key":"","base_url":"https://api.siliconflow.cn/v1/embeddings"},"user_profile_definition":"用户本人的肯定或否定表述（包含不限于基本身份、兴趣偏好、人际关系、资产状况）","semantic_memory_definition":"用户对话中涉及的和时间无明确关系的事实性内容或概念","episodic_memory_definition":"用户对话中涉及的和时间有明确关系的事实性内容或概念","extract_assistant_memory":false,"use_query_rewrite":false,"use_when_to_use":false}',
   1, NULL, 1, 'system', CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM config_templates WHERE id = 'tpl_scope_basic');

-- 6.2 增强版（SCOPE 模板）
INSERT INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, parent_id, version, created_by, updated_at)
SELECT 'tpl_scope_enhanced', 'scope_enhanced', '增强版', '适用于复杂业务，高 LLM + 向量优化', 'SCOPE',
   '{"model_cfg":{"model":"qwen-max","temperature":0.1,"max_tokens":3000},"model_client_cfg":{"client_provider":"SiliconFlow","api_key":"","api_base":"https://api.siliconflow.cn/v1","verify_ssl":false,"timeout":120.0},"embedding_cfg":{"model_name":"BAAI/bge-m3","api_key":"","base_url":"https://api.siliconflow.cn/v1/embeddings"},"user_profile_definition":"详细提取用户身份信息、职业背景、技能专长、兴趣偏好、人际关系、资产状况、健康状况、地理位置等全方位画像信息","semantic_memory_definition":"提取用户对话中所有事实性内容，包括概念定义、技术原理、产品信息、行业知识等","episodic_memory_definition":"提取用户对话中所有时间相关的事件，包括会议、约会、里程碑、历史事件等","extract_assistant_memory":true,"use_query_rewrite":true,"use_when_to_use":true}',
   1, NULL, 1, 'system', CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM config_templates WHERE id = 'tpl_scope_enhanced');

-- 6.3 高性能版（SCOPE 模板）
INSERT INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, parent_id, version, created_by, updated_at)
SELECT 'tpl_scope_highperf', 'scope_highperf', '高性能版', '企业级，高并发 + 长保留', 'SCOPE',
   '{"model_cfg":{"model":"qwen-turbo","temperature":0.05,"max_tokens":1500},"model_client_cfg":{"client_provider":"SiliconFlow","api_key":"","api_base":"https://api.siliconflow.cn/v1","verify_ssl":false,"timeout":30.0},"embedding_cfg":{"model_name":"BAAI/bge-small-zh","api_key":"","base_url":"https://api.siliconflow.cn/v1/embeddings"},"user_profile_definition":"用户核心身份","semantic_memory_definition":"用户对话核心事实","episodic_memory_definition":"用户对话关键事件","extract_assistant_memory":false,"use_query_rewrite":false,"use_when_to_use":true}',
   1, NULL, 1, 'system', CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM config_templates WHERE id = 'tpl_scope_highperf');

-- 6.4 热启动实例模板（INSTANCE）
INSERT INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, parent_id, version, created_by, updated_at)
SELECT 'tpl_instance_hot', 'instance_hot', '热启动实例配置', '热启动参数（修改后立即生效，无需重启引擎）：记忆索引、重排序、存储路径', 'INSTANCE',
   '{"MEMORY_INDEX_TYPE":"vector","RERANK_API_BASE":"","RERANK_API_KEY":"","RERANK_MODEL_NAME":"BAAI/bge-reranker-v2","RERANK_THRESHOLD":"0.3","RERANK_POOL_FACTOR":"3","DB_URL":"","KV_SHELVE_PATH":"","VECTOR_CHROMA_PERSIST_DIR":""}',
   1, NULL, 1, 'system', CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM config_templates WHERE id = 'tpl_instance_hot');

-- 6.5 冷启动实例模板（INSTANCE）
INSERT INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, parent_id, version, created_by, updated_at)
SELECT 'tpl_instance_cold', 'instance_cold', '冷启动实例配置', '冷启动参数（修改后需重启引擎才能生效）：做梦引擎、中间记忆、遗忘机制', 'INSTANCE',
   '{"DREAMING_ENABLED":"false","DREAMING_INTERVAL_SECONDS":"14400","MEMORY_ENABLE_MIDDLE_MEMORY":"true","MEMORY_MIDDLE_CHECK_INTERVAL":"50","MEMORY_ENABLE_FORGETTING":"false","MEMORY_FORGET_INTERVAL":"86400","MEMORY_FORGET_LAMBDA":"0.1","MEMORY_FORGET_THRESHOLD":"0.5","MEMORY_FORGET_COOLDOWN":"3600","MEMORY_FORGET_DEFAULT_IMPORTANCE":"5","MEMORY_FORGET_EXEMPT_IMPORTANCE":"8"}',
   1, NULL, 1, 'system', CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM config_templates WHERE id = 'tpl_instance_cold');

-- 6.5.1 清理遗留的旧版 instance_default 模板（已被 instance_hot + instance_cold 替代）
DELETE FROM config_templates WHERE id = 'tpl_instance_default' OR template_name = 'instance_default';
