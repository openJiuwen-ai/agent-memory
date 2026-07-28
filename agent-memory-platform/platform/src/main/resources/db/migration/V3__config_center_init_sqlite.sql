-- =====================================================
-- 配置中心表（功能2/3）：模板 + 租户快照 + 实例配置 + 审计。IF NOT EXISTS 幂等。
-- 参照设计文档 §1.3.3 + §5.2 + §5.4 模板 + §5.11 审计
-- 2026-07-16 P0-1.5: Scope=隔离单元 (1:N over Tenant), 4 层继承
-- 2026-07-16 P0-2:   多管理员共管 + confirm_tokens + dreaming 2 层拆分
-- 2026-07-17 P0-2 v2: 引擎级 Dreaming 进 KernelConfig, scope-level 走 4 层
-- 2026-07-17 P0-3 v2: 1 tenant = 1 scope (UUID 同体), 改用 tenant_scope_configs 1:1 替代 scope_registry N
-- =====================================================

-- 0. 清理 P0-3 v2 已删除的旧表（4 层继承 + scope 多管理员）
-- 这些表是 P0-3 v2 之前的版本创建的，本次重构彻底删除
-- 注意：生产环境不要每次启动都 DROP，否则用户数据会丢失。
-- 这里仅保留对旧表的清理，且使用 IF EXISTS 避免首次启动报错。
DROP TABLE IF EXISTS scope_admins;
DROP TABLE IF EXISTS template_scope_bindings;
DROP TABLE IF EXISTS config_versions;
DROP TABLE IF EXISTS dreaming_scope_configs;
DROP TABLE IF EXISTS dreaming_engine_configs;
DROP TABLE IF EXISTS tenant_default_configs;
DROP TABLE IF EXISTS sys_default_configs;
DROP TABLE IF EXISTS scope_configs;
DROP TABLE IF EXISTS scope_registry;

-- -----------------------------------------------------
-- 1. tenant_scope_configs: 租户级 Scope 配置快照 (1 tenant = 1 scope, UUID 同体)
-- 替代 scope_registry + scope_configs + scope_admins + tenant_default_configs + sys_default_configs
-- 每行 = 一个租户的"应用配置快照"，可通过应用 SCOPE 模板生成
-- 偏离检测：template_version vs current_version 不一致即"已偏离"
-- 继承链退化为：tenant_scope_configs.config_json (无 4 层, 客户操作简单)
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_scope_configs (
    tenant_id          TEXT PRIMARY KEY,                 -- UUID, 与 tenants.id 同体
    tenant_name        VARCHAR(128) NOT NULL,            -- 冗余 denormalize 方便查
    instance_id        VARCHAR(64) NOT NULL DEFAULT 'default',
    config_json        TEXT NOT NULL,                    -- 完整配置 JSON (租户可改)
    template_id        TEXT,                             -- 应用的模板 ID (NULL = 未应用任何模板)
    template_version   INTEGER DEFAULT 0,                -- 应用时模板版本 (用于偏离检测)
    current_version    INTEGER NOT NULL DEFAULT 1,       -- 租户自己改一次 +1
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by         VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_tenant_scope_instance ON tenant_scope_configs(instance_id);
CREATE INDEX IF NOT EXISTS idx_tenant_scope_template ON tenant_scope_configs(template_id);

-- -----------------------------------------------------
-- 2. config_templates: 配置模板表
-- 简化为 2 种类型：SCOPE / INSTANCE
--   - SCOPE: 应用到租户, 生成 tenant_scope_configs 行
--   - INSTANCE: 单例, 修改后写 instance_config 并 Push 到内核
-- is_builtin=1 预置模板不可改
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS config_templates (
    id              TEXT PRIMARY KEY,                    -- UUID
    template_name   VARCHAR(64) NOT NULL,
    display_name    VARCHAR(128) NOT NULL,
    description     TEXT,
    template_type   VARCHAR(16) NOT NULL DEFAULT 'SCOPE',  -- SCOPE / INSTANCE
    config_json     TEXT NOT NULL,                       -- 参数 JSON
    is_builtin      INTEGER DEFAULT 0,                   -- 1=预置不可改, 0=自定义
    parent_id       TEXT,                                -- 复制来源模板 (NULL=原创)
    version         INTEGER NOT NULL DEFAULT 1,
    status          VARCHAR(16) NOT NULL DEFAULT 'published', -- published=已发布, draft=草稿
    created_by      VARCHAR(64) NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(template_name, template_type)
);
CREATE INDEX IF NOT EXISTS idx_template_type ON config_templates(template_type);
CREATE INDEX IF NOT EXISTS idx_template_parent ON config_templates(parent_id);
CREATE INDEX IF NOT EXISTS idx_template_status ON config_templates(status);

-- -----------------------------------------------------
-- 3. instance_config: 实例级配置 (单例)
-- 替代 dreaming_sys_default_config + 之前 kernel_config
-- id=1 强制单例
-- 改后触发 kernel Push + 提示需重启实例
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS instance_config (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    template_id             TEXT,                          -- 关联的 INSTANCE 模板 (默认指向 "系统默认实例配置")
    config_json             TEXT NOT NULL,                 -- 完整配置 JSON
    version                 INTEGER NOT NULL DEFAULT 1,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by              VARCHAR(64)
);

-- -----------------------------------------------------
-- 4. config_audit_logs: 配置审计表
-- 操作类型：TEMPLATE_CREATE / TEMPLATE_UPDATE / TEMPLATE_COPY / TEMPLATE_APPLY_TENANT /
--         INSTANCE_CONFIG_UPDATE / TENANT_CONFIG_UPDATE / SYNC_FROM_TEMPLATE / DREAMING_ENGINE_UPDATE
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS config_audit_logs (
    id              TEXT PRIMARY KEY,
    operator_id     VARCHAR(64) NOT NULL,                -- 操作人
    tenant_id       TEXT,                                -- 涉及租户 (NULL=平台级)
    template_id     TEXT,                                -- 涉及模板
    instance_id     VARCHAR(64) NOT NULL DEFAULT 'default',
    operation       VARCHAR(64) NOT NULL,
    before_value    TEXT,                                -- before JSON
    after_value     TEXT,                                -- after JSON
    success         INTEGER NOT NULL,
    error_message   TEXT,
    operated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason          TEXT                                 -- 操作原因
);
CREATE INDEX IF NOT EXISTS idx_cfg_audit_tenant_time ON config_audit_logs(tenant_id, operated_at);
CREATE INDEX IF NOT EXISTS idx_cfg_audit_template_time ON config_audit_logs(template_id, operated_at);
CREATE INDEX IF NOT EXISTS idx_cfg_audit_operator_time ON config_audit_logs(operator_id, operated_at);
CREATE INDEX IF NOT EXISTS idx_cfg_audit_instance ON config_audit_logs(instance_id);

-- -----------------------------------------------------
-- 5. confirm_tokens: 二次确认令牌表（P0-2 流程层）
-- 用于高危操作 (INSTANCE_CONFIG_UPDATE, SYNC_FROM_TEMPLATE 等) 的二次确认
-- 流程：issue(签发) -> validate(校验) -> consume(消费，防重放)
-- TTL 默认 5 分钟，消费后失效
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS confirm_tokens (
    token           TEXT PRIMARY KEY,                -- 一次性令牌 (UUID)
    operator_id     VARCHAR(64) NOT NULL,            -- 操作人
    action          VARCHAR(64) NOT NULL,            -- 操作类型
    resource        VARCHAR(256) NOT NULL,           -- 资源标识
    payload         TEXT,                            -- 上下文 payload (JSON)
    expires_at      TIMESTAMP NOT NULL,              -- 过期时间
    consumed        INTEGER NOT NULL DEFAULT 0,
    consumed_at     TIMESTAMP,
    issued_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_confirm_token_operator ON confirm_tokens(operator_id, action);
CREATE INDEX IF NOT EXISTS idx_confirm_token_expires ON confirm_tokens(expires_at);

-- -----------------------------------------------------
-- 6. 预置数据：3 个 SCOPE 模板 + 2 个 INSTANCE 模板（热启动 + 冷启动）
-- -----------------------------------------------------
-- 6.1 基础版（SCOPE 模板）
INSERT OR IGNORE INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, version, created_by, updated_at)
VALUES
  ('tpl_scope_basic', 'scope_basic', '基础版', '适用于一般 agent，单 LLM + 轻量配置', 'SCOPE',
   '{"model_cfg":{"model":"qwen-plus","temperature":0.1,"max_tokens":2000},"model_client_cfg":{"client_provider":"SiliconFlow","api_key":"","api_base":"https://api.siliconflow.cn/v1","verify_ssl":false,"timeout":90.0},"embedding_cfg":{"model_name":"BAAI/bge-m3","api_key":"","base_url":"https://api.siliconflow.cn/v1/embeddings"},"user_profile_definition":"用户本人的肯定或否定表述（包含不限于基本身份、兴趣偏好、人际关系、资产状况）","semantic_memory_definition":"用户对话中涉及的和时间无明确关系的事实性内容或概念","episodic_memory_definition":"用户对话中涉及的和时间有明确关系的事实性内容或概念","extract_assistant_memory":false,"use_query_rewrite":false,"use_when_to_use":false}',
   1, 1, 'system', CURRENT_TIMESTAMP);

-- 6.2 增强版（SCOPE 模板）
INSERT OR IGNORE INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, version, created_by, updated_at)
VALUES
  ('tpl_scope_enhanced', 'scope_enhanced', '增强版', '适用于复杂业务，高 LLM + 向量优化', 'SCOPE',
   '{"model_cfg":{"model":"qwen-max","temperature":0.1,"max_tokens":3000},"model_client_cfg":{"client_provider":"SiliconFlow","api_key":"","api_base":"https://api.siliconflow.cn/v1","verify_ssl":false,"timeout":120.0},"embedding_cfg":{"model_name":"BAAI/bge-m3","api_key":"","base_url":"https://api.siliconflow.cn/v1/embeddings"},"user_profile_definition":"详细提取用户身份信息、职业背景、技能专长、兴趣偏好、人际关系、资产状况、健康状况、地理位置等全方位画像信息","semantic_memory_definition":"提取用户对话中所有事实性内容，包括概念定义、技术原理、产品信息、行业知识等","episodic_memory_definition":"提取用户对话中所有时间相关的事件，包括会议、约会、里程碑、历史事件等","extract_assistant_memory":true,"use_query_rewrite":true,"use_when_to_use":true}',
   1, 1, 'system', CURRENT_TIMESTAMP);

-- 6.3 高性能版（SCOPE 模板）
INSERT OR IGNORE INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, version, created_by, updated_at)
VALUES
  ('tpl_scope_highperf', 'scope_highperf', '高性能版', '企业级，高并发 + 长保留', 'SCOPE',
   '{"model_cfg":{"model":"qwen-turbo","temperature":0.05,"max_tokens":1500},"model_client_cfg":{"client_provider":"SiliconFlow","api_key":"","api_base":"https://api.siliconflow.cn/v1","verify_ssl":false,"timeout":30.0},"embedding_cfg":{"model_name":"BAAI/bge-small-zh","api_key":"","base_url":"https://api.siliconflow.cn/v1/embeddings"},"user_profile_definition":"用户核心身份","semantic_memory_definition":"用户对话核心事实","episodic_memory_definition":"用户对话关键事件","extract_assistant_memory":false,"use_query_rewrite":false,"use_when_to_use":true}',
   1, 1, 'system', CURRENT_TIMESTAMP);

-- 6.4 热启动实例模板（INSTANCE，修改后立即生效，无需重启引擎）
-- 包含：记忆索引类型、重排序参数、存储路径等可热生效参数
INSERT OR IGNORE INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, version, created_by, updated_at)
VALUES
  ('tpl_instance_hot', 'instance_hot', '热启动实例配置', '热启动参数（修改后立即生效，无需重启引擎）：记忆索引、重排序、存储路径', 'INSTANCE',
   '{"MEMORY_INDEX_TYPE":"vector","RERANK_API_BASE":"","RERANK_API_KEY":"","RERANK_MODEL_NAME":"BAAI/bge-reranker-v2","RERANK_THRESHOLD":"0.3","RERANK_POOL_FACTOR":"3","DB_URL":"","KV_SHELVE_PATH":"","VECTOR_CHROMA_PERSIST_DIR":""}',
   1, 1, 'system', CURRENT_TIMESTAMP);

-- 6.5 冷启动实例模板（INSTANCE，修改后需重启引擎才能生效）
-- 包含：做梦引擎、中间记忆、遗忘机制等需重启的参数
INSERT OR IGNORE INTO config_templates
  (id, template_name, display_name, description, template_type, config_json, is_builtin, version, created_by, updated_at)
VALUES
  ('tpl_instance_cold', 'instance_cold', '冷启动实例配置', '冷启动参数（修改后需重启引擎才能生效）：做梦引擎、中间记忆、遗忘机制', 'INSTANCE',
   '{"DREAMING_ENABLED":"false","DREAMING_INTERVAL_SECONDS":"14400","MEMORY_ENABLE_MIDDLE_MEMORY":"true","MEMORY_MIDDLE_CHECK_INTERVAL":"50","MEMORY_ENABLE_FORGETTING":"false","MEMORY_FORGET_INTERVAL":"86400","MEMORY_FORGET_LAMBDA":"0.1","MEMORY_FORGET_THRESHOLD":"0.5","MEMORY_FORGET_COOLDOWN":"3600","MEMORY_FORGET_DEFAULT_IMPORTANCE":"5","MEMORY_FORGET_EXEMPT_IMPORTANCE":"8"}',
   1, 1, 'system', CURRENT_TIMESTAMP);

-- 6.5.1 清理遗留的旧版 instance_default 模板（已被 instance_hot + instance_cold 替代）
DELETE FROM config_templates WHERE id = 'tpl_instance_default' OR template_name = 'instance_default';

-- 6.6 instance_config 单例初始化（热启动 + 冷启动参数合并）
INSERT OR IGNORE INTO instance_config
  (id, template_id, config_json, version, updated_at, updated_by)
VALUES
  (1, NULL,
   '{"MEMORY_INDEX_TYPE":"vector","RERANK_API_BASE":"","RERANK_API_KEY":"","RERANK_MODEL_NAME":"BAAI/bge-reranker-v2","RERANK_THRESHOLD":"0.3","RERANK_POOL_FACTOR":"3","DB_URL":"","KV_SHELVE_PATH":"","VECTOR_CHROMA_PERSIST_DIR":"","DREAMING_ENABLED":"false","DREAMING_INTERVAL_SECONDS":"14400","MEMORY_ENABLE_MIDDLE_MEMORY":"true","MEMORY_MIDDLE_CHECK_INTERVAL":"50","MEMORY_ENABLE_FORGETTING":"false","MEMORY_FORGET_INTERVAL":"86400","MEMORY_FORGET_LAMBDA":"0.1","MEMORY_FORGET_THRESHOLD":"0.5","MEMORY_FORGET_COOLDOWN":"3600","MEMORY_FORGET_DEFAULT_IMPORTANCE":"5","MEMORY_FORGET_EXEMPT_IMPORTANCE":"8"}',
   1, CURRENT_TIMESTAMP, 'system');

-- 6.7 修复遗留 instance_config：将旧版数字 template_id 重置为 NULL（当前设计不绑定模板）
UPDATE instance_config SET template_id = NULL WHERE template_id IS NOT NULL;
