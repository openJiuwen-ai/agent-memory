package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.Map;

/**
 * 内核配置更新请求（Push 模型）。
 * <p>
 * 内核 .env 始终是唯一配置源。服务层做白名单过滤后通过 PUT /admin/config 写入。
 * <p>
 * 关键参数：
 * <ul>
 *   <li>{@code updates}：key=value 形式的参数集合（key 大小写不敏感，内部转大写）</li>
 *   <li>{@code restart=true}：写入 .env 后调用 POST /admin/restart 触发重启（需 kernel:restart 权限 + confirmToken）</li>
 *   <li>{@code reason}：操作原因（审计）</li>
 *   <li>{@code confirmToken}：仅 restart=true 时必填，5 分钟 TTL 防重放</li>
 * </ul>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class KernelConfigUpdateRequest {

    @JsonProperty("updates")
    private Map<String, String> updates;

    @JsonProperty("restart")
    private boolean restart;

    @JsonProperty("reason")
    private String reason;

    @JsonProperty("confirm_token")
    private String confirmToken;
}
