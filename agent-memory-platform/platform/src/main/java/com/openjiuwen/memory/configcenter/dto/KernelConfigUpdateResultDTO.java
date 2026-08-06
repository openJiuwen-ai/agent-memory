package com.openjiuwen.memory.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

/**
 * 内核配置更新结果 DTO。
 * <p>
 * 用于回显：
 * <ul>
 *   <li>{@code updated_keys}：实际写入 .env 的 key 列表（大写）</li>
 *   <li>{@code rejected_keys}：被白名单拒绝的 key 列表（架构参数不可改）</li>
 *   <li>{@code restart_triggered}：是否触发了内核重启</li>
 *   <li>{@code restart_status}：重启状态描述</li>
 *   <li>{@code message}：人类可读结果消息</li>
 * </ul>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class KernelConfigUpdateResultDTO {

    @JsonProperty("updated_keys")
    private List<String> updatedKeys;

    @JsonProperty("rejected_keys")
    private List<String> rejectedKeys;

    @JsonProperty("restart_triggered")
    private boolean restartTriggered;

    @JsonProperty("restart_status")
    private String restartStatus;

    @JsonProperty("message")
    private String message;
}
