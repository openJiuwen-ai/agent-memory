package com.openjiuwen.memory.configcenter.dto;

import lombok.Builder;
import lombok.Data;

/**
 * 模板使用量轻量 DTO（模板列表页渲染用，不含 config_json）
 */
@Data
@Builder
public class TemplateUsageDTO {

    /** 租户 ID */
    private String tenantId;

    /** 租户名称 */
    private String tenantName;

    /** 模板 ID */
    private String templateId;

    /** 模板版本 */
    private Integer templateVersion;

    /** 当前租户配置版本 */
    private Integer currentVersion;

    /** 是否偏离模板 */
    private Boolean isDeviated;

    /** 更新时间 */
    private String updatedAt;

    /** 更新人 */
    private String updatedBy;
}
