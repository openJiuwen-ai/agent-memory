package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 租户级 Scope 配置 DTO（1 tenant = 1 scope, UUID 同体）— 2026-07-17 P0-3 v2
 */
@Data
public class TenantScopeConfigDTO {
    private String tenantId;
    private String tenantName;
    private String scopeId;
    private String instanceId;
    private String configJson;
    private String templateId;
    private String templateName;
    private Integer templateVersion;
    private Integer currentVersion;
    private Boolean isDeviated; // templateVersion != currentVersion
    private String updatedAt;
    private String updatedBy;
}
