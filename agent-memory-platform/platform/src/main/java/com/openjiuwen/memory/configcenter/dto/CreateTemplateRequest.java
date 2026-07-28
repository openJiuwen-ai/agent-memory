package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

/**
 * 创建/复制模板请求（2026-07-17 P0-3 v2）
 * <p>
 * isCopy=true 时 parentId 必须指源模板；为 false 时为原创。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class CreateTemplateRequest {
    @JsonProperty("template_name")
    private String templateName;

    @JsonProperty("display_name")
    private String displayName;

    @JsonProperty("description")
    private String description;

    @JsonProperty("template_type")
    private String templateType; // SCOPE / INSTANCE

    @JsonProperty("config_json")
    private String configJson;

    @JsonProperty("parent_id")
    private String parentId; // 复制场景

    /** 可选：创建后立即应用到这些租户（仅 SCOPE 模板） */
    @JsonProperty("target_tenant_ids")
    private List<String> targetTenantIds;

    @JsonProperty("reason")
    private String reason;
}
