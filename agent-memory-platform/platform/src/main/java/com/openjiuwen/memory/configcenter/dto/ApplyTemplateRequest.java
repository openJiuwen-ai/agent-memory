package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.List;

/**
 * 模板应用请求 DTO（2026-07-19 P0-3 v3）
 * <p>
 * 两个按钮共享：
 * <ul>
 *   <li>SCOPE 模板：必填 targetTenantIds（同步生效）</li>
 *   <li>INSTANCE 模板：不需要 targetTenantIds（自动影响全局）</li>
 * </ul>
 * <p>
 * INSTANCE 模板应用/更新时，若 {@code restart=true} 且操作人拥有 kernel:restart 权限，
 * 则在 Push 到 .env 后调用内核 POST /admin/restart 触发引擎重启。
 * 重启属于高危操作，必须携带 {@code confirm_token}（由 /api/v1/config/kernel/confirm-token 签发）。
 */
@Data
public class ApplyTemplateRequest {
    private String templateId;

    /** SCOPE 模板必填，从 tenants 表拉取（用户面只说"租户"，不出现 scope_id） */
    private List<String> targetTenantIds;

    /** 操作原因（审计） */
    private String reason;

    /** INSTANCE 模板专用：是否触发引擎重启 */
    @JsonProperty("restart")
    private boolean restart;

    /** INSTANCE 模板专用：restart=true 时必填的二次确认令牌 */
    @JsonProperty("confirm_token")
    private String confirmToken;
}
