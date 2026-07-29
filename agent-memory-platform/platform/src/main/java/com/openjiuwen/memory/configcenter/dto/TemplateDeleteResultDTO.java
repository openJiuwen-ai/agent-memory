package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

/**
 * 模板删除结果 DTO
 * <p>
 * 当模板被租户绑定时，删除会级联清理内核 scope 配置 + DB 绑定记录。
 * 本 DTO 携带受影响的 scope 列表，供前端向用户展示清理详情。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class TemplateDeleteResultDTO {
    private String templateId;
    private String templateName;

    /** 受影响的 scope 清理结果列表（每个绑定的租户对应一条） */
    private List<ScopeCleanupResult> cleanedScopes;

    /** 内核 scope 配置删除成功数 */
    @JsonProperty("kernel_success_count")
    private int kernelSuccessCount;

    /** 内核 scope 配置删除失败数 */
    @JsonProperty("kernel_fail_count")
    private int kernelFailCount;

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    public static class ScopeCleanupResult {
        private String tenantId;
        private String tenantName;
        private String scopeId;
        /** 内核 scope 配置是否删除成功 */
        private Boolean kernelDeleted;
        /** DB 绑定记录是否删除成功 */
        @JsonProperty("db_binding_deleted")
        private Boolean dbBindingDeleted;
        private String errorMessage;
    }
}
