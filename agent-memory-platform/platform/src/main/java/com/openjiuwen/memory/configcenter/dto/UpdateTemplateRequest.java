package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 更新模板参数请求（2026-07-19 P0-3 v3）
 * <p>
 * 修改 INSTANCE 模板自动 Push 到 instance_config（单例）— 平台管理员限定。
 * <p>
 * INSTANCE 模板更新时，若 {@code restart=true} 且操作人拥有 kernel:restart 权限，
 * 则在 Push 到 .env 后调用内核 POST /admin/restart 触发引擎重启。
 * 重启属于高危操作，必须携带 {@code confirm_token}。
 */
@Data
public class UpdateTemplateRequest {
    @JsonProperty("display_name")
    private String displayName;

    @JsonProperty("description")
    private String description;

    @JsonProperty("config_json")
    private String configJson;

    @JsonProperty("reason")
    private String reason;

    /** 是否应用到内核：true=保存并下发，false=仅保存草稿 */
    @JsonProperty("apply")
    private boolean apply = true;

    /**
     * SCOPE 模板专用：编辑保存时指定要应用（绑定）的目标租户列表。
     * <p>
     * 为 null 时保持原行为（仅对已绑定租户重新下发）；
     * 为非空列表时，将这些租户与已绑定租户合并后一并应用（新增绑定 + 已绑定重下发）。
     * 这样编辑页选了"应用目标租户"并保存修改后，所选租户会被实际绑定到该模板。
     */
    @JsonProperty("target_tenant_ids")
    private java.util.List<String> targetTenantIds;

    /** INSTANCE 模板专用：是否触发引擎重启 */
    @JsonProperty("restart")
    private boolean restart;

    /** INSTANCE 模板专用：restart=true 时必填的二次确认令牌 */
    @JsonProperty("confirm_token")
    private String confirmToken;
}
