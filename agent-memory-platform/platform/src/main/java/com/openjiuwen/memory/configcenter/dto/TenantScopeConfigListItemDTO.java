package com.openjiuwen.memory.configcenter.dto;

import lombok.Data;

import java.util.List;

/**
 * 租户级 Scope 配置 — 列表视图专用 DTO（2026-07-23）。
 * <p>
 * 与 {@link TenantScopeConfigDTO} 的区别：<b>物理上没有 configJson 字段</b>，
 * 避免列表场景把大字段序列化出去（即使值为 null 也会污染响应）。
 * 单条快照/编辑仍走 {@link TenantScopeConfigDTO}（含 configJson 用于下发与比对）。
 * <p>
 * scopeIds 用 List 承载：当前业务约束 1 租户 = 1 scope（TenantController 限制），
 * 但表结构 scope_registry.assigned_to_tenant_id 天然支持 1 对多，List 形态为未来扩展留口。
 */
@Data
public class TenantScopeConfigListItemDTO {
    private String tenantId;
    private String tenantName;
    private List<String> scopeIds;
    private String instanceId;
    private String templateId;
    private String templateName;
    private Integer templateVersion;
    private Integer currentVersion;
    private Boolean isDeviated; // templateVersion != currentVersion
    private String updatedAt;
    private String updatedBy;
}
