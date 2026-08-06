package com.openjiuwen.memory.scopecenter.dto;

import lombok.Builder;
import lombok.Data;

/**
 * Scope 统计信息
 */
@Data
@Builder
public class ScopeStatsDTO {
    /**
     * Scope ID
     */
    private String scopeId;

    /**
     * Scope 名称
     */
    private String scopeName;

    /**
     * 描述
     */
    private String description;

    /**
     * 状态：已绑定租户 -- yes / 未绑定租户 -- no
     */
    private String boundToTenant;

    /**
     * 关联的租户 ID（如果已绑定）
     */
    private String tenantId;

    /**
     * 关联的租户名称（如果已绑定）
     */
    private String tenantName;
}
