package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 实例级配置 DTO（2026-07-17 P0-3 v2）
 * <p>
 * 单例表 id=1。修改时提示客户需重启实例。
 */
@Data
public class InstanceConfigDTO {
    @JsonProperty("template_id")
    private String templateId;

    @JsonProperty("config_json")
    private String configJson;

    @JsonProperty("version")
    private Integer version;

    @JsonProperty("updated_at")
    private String updatedAt;

    @JsonProperty("updated_by")
    private String updatedBy;
}
