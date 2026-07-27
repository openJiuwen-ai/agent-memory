package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

/**
 * 模板应用结果 DTO（2026-07-19 P0-3 v3）
 * <p>
 * INSTANCE 模板应用/更新时，若触发引擎重启，结果中携带：
 * <ul>
 *   <li>{@code restart_triggered} — 是否已调用内核 restart</li>
 *   <li>{@code restart_status} — 重启状态描述</li>
 * </ul>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class TemplateApplyResultDTO {
    private String templateId;
    private String templateName;
    private String templateType;
    private List<TenantApplyResult> results;
    private Integer successCount;
    private Integer failCount;

    @JsonProperty("restart_triggered")
    private boolean restartTriggered;

    @JsonProperty("restart_status")
    private String restartStatus;

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    public static class TenantApplyResult {
        private String tenantId;
        private String tenantName;
        private Boolean success;
        private Integer currentVersion;
        private String errorMessage;
    }
}
