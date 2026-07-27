package com.openjiuwen.memory.configcenter.dto;

import lombok.Builder;
import lombok.Data;

import java.time.Instant;
import java.util.List;

/**
 * 配置模板列表项 DTO
 * <p>
 * 在模板基础信息上附加当前使用此模板的租户列表，供模板列表页一次性渲染。
 */
@Data
@Builder
public class ConfigTemplateListItemDTO {

    private String id;
    private String templateName;
    private String displayName;
    private String description;
    private String templateType;
    private Integer isBuiltin;
    private String parentId;
    private Integer version;
    /** 状态：published=已发布，draft=草稿 */
    private String status;
    private String createdBy;
    private Instant createdAt;
    private Instant updatedAt;

    /** 当前正在使用此模板的租户（仅 SCOPE 模板有意义） */
    private List<TemplateTenantUsageDTO> tenantUsage;
}
