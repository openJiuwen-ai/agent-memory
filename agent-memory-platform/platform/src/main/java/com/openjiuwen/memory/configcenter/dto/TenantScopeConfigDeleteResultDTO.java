package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 租户 Scope 配置删除结果 DTO。
 * <p>
 * 用于"清除租户 Scope 配置"操作：删除内核 KV 中的 scope 配置 + DB 绑定记录，
 * 使该租户回退到默认配置。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class TenantScopeConfigDeleteResultDTO {

    private String tenantId;

    private String tenantName;

    /** 该租户绑定的 scope_id（来自 tenants 表 scope_ids） */
    private String scopeId;

    /** 内核 scope 配置是否删除成功 */
    @JsonProperty("kernel_deleted")
    private Boolean kernelDeleted;

    /** DB 绑定记录（tenant_scope_configs 行）是否删除成功 */
    @JsonProperty("db_binding_deleted")
    private Boolean dbBindingDeleted;

    /** 失败时的错误信息（kernelDeleted=false 或 dbBindingDeleted=false 时填充） */
    private String errorMessage;
}
