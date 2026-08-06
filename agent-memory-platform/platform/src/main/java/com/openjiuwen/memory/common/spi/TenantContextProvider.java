package com.openjiuwen.memory.common.spi;

/**
 * 多租户上下文（属"租户管理/安全中心"模块，本模块不实现，仅占位）。
 * <p>
 * 内核使用 scope_id + user_id 二维隔离，无 tenant_id；服务化层在中间件做 ID 前缀映射
 * {@code kernel_scope_id = tenant_id + "__" + scope_id}。本模块取上下文拿到映射后的内核 ID 透传给 :8516。
 */
public interface TenantContextProvider {

    /** 当前请求的租户上下文；缺省实现返回 identity（透传原值）。 */
    TenantContext current();

    record TenantContext(String tenantId, String userId, String role,
                         String kernelScopeId, String kernelUserId) {
    }

    /** 从上下文解析操作人 ID，无上下文时返回 "system"。 */
    default String resolveOperator() {
        TenantContext ctx = current();
        return ctx != null && ctx.userId() != null ? ctx.userId() : "system";
    }

    /** 从上下文解析租户 ID，无上下文时返回 "default"。 */
    default String resolveTenant() {
        TenantContext ctx = current();
        return ctx != null && ctx.tenantId() != null ? ctx.tenantId() : "default";
    }
}
