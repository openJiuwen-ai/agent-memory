package com.openjiuwen.memory.configcenter.domain;

import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/**
 * 配置模板表 — 2026-07-17 P0-3 v2 重构
 * <p>
 * template_type: SCOPE（应用型，应用到租户生成 tenant_scope_configs 快照）
 *                INSTANCE（实例型，单例，对应 instance_config 表）
 * is_builtin: 0=自定义 1=预置（预置不可删/改）
 * parent_id: 复制来源模板 ID（NULL=原创）
 * version: 模板版本号，修改一次 +1
 */
@Data
@TableName("config_templates")
public class ConfigTemplateEntity {

    @TableId
    private String id;

    private String templateName;
    private String displayName;
    private String description;
    private String templateType;
    private String configJson;
    private Integer isBuiltin;
    private String parentId;
    private Integer version;
    private String status;  // published=已发布, draft=草稿
    private String createdBy;
    private Instant createdAt;
    private Instant updatedAt;
}
