package com.openjiuwen.memory.configcenter.domain;

import com.baomidou.mybatisplus.annotation.TableField;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/**
 * 租户级 Scope 配置快照 (1 tenant = 1 scope, UUID 同体)
 * <p>
 * 2026-07-17 P0-3 v2 重构：1 tenant = 1 scope 关系，{@code tenant_id} 即为 scope_id。
 * 每行代表一个租户的"应用配置快照"，可通过应用 SCOPE 模板生成。
 * <p>
 * 偏离检测：{@code template_version} vs {@code currentVersion} 不一致 = 租户已自定义修改
 */
@Data
@TableName("tenant_scope_configs")
public class TenantScopeConfigEntity {

    @TableId(type = IdType.ASSIGN_UUID)
    private String tenantId;

    private String tenantName;

    private String instanceId;

    /** 完整配置 JSON（租户可改） */
    private String configJson;

    /** 应用的模板 ID (NULL = 未应用任何模板) */
    private String templateId;

    /** 应用时模板版本（用于偏离检测） */
    private Integer templateVersion;

    /** 租户自己改一次 +1 */
    private Integer currentVersion;

    private Instant updatedAt;

    private String updatedBy;
    /**
     * 列表视图专用的瞬态字段：由 listXxxWithScope 的 LEFT JOIN 带出（scope_registry.scope_id）。
     * 绑定关系权威来源是 scope_registry；本表不冗余 scope_id 列。
     */
    @TableField(exist = false)
    private String scopeId;
}
