package com.openjiuwen.memory.configcenter.dto;

import lombok.Builder;
import lombok.Data;

/**
 * 模板使用租户轻量 DTO
 */
@Data
@Builder
public class TemplateTenantUsageDTO {

    private String tenantId;
    private String tenantName;
}
