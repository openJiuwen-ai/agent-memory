package com.openjiuwen.memory.common.spi;

import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;

/**
 * 权限校验（属"安全中心"模块）。对齐 730 §6.2 权限码：ops:read/ops:write/memory:read/.../config:read 等。
 * <p>
 * 权限码体系（§1.5 RBAC）：
 * <ul>
 *   <li>config:read  — 查看配置（所有配置类页面）</li>
 *   <li>config:write — 修改 Scope/Agent/引擎配置（租户级 + 平台级）</li>
 *   <li>kernel:restart — 触发内核全局重启（仅平台级管理员）</li>
 *   <li>ops:read / ops:write — 运维命令查看/下发</li>
 * </ul>
 * <p>
 * 租户权限模型：
 * <ul>
 *   <li>SCOPE_ADMIN（租户管理员）：可 config:write（修改 Scope 级配置），但 <b>不可</b> kernel:restart</li>
 *   <li>SUPER_ADMIN / PLATFORM_ADMIN：可 config:write + kernel:restart</li>
 *   <li>SECURITY_ADMIN：仅 config:read</li>
 * </ul>
 */
public interface PermissionChecker {

    /**
     * 校验当前用户是否拥有权限；缺省实现放行并打 WARN。
     *
     * @param permission 权限码
     * @return true=拥有权限
     */
    boolean check(String permission);

    /**
     * 强制要求权限 — 不通过则抛 {@link BizException}（FORBIDDEN）。
     * <p>
     * 用于高危操作的代码级拦截（Layer 1），即使 JWT Token 有效也必须通过此校验。
     *
     * @param permission 权限码
     * @throws BizException 权限不足时抛出 FORBIDDEN
     */
    default void require(String permission) {
        if (!check(permission)) {
            throw new BizException(ResultCode.FORBIDDEN,
                    "权限不足，缺少权限: " + permission);
        }
    }
}
