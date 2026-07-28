package com.openjiuwen.memory.common.client.dto;

import lombok.Data;

import java.util.List;
import java.util.Map;

/**
 * 对齐 :8516 AddMessagesRequest。
 * <p>
 * messages: List[{role, content}]；enable_* 由运维中心 FeatureFlagService.resolve(scopeId) 注入，
 * 不由业务调用方直接传。
 */
@Data
public class AddMessagesRequest {

    private List<Map<String, String>> messages;
    private String userId = "__default__";
    private String scopeId = "__default__";
    private List<MemVariable> memVariables;

    private Boolean enableLongTermMem = true;
    private Boolean enableUserProfile = true;
    private Boolean enableSemanticMemory = true;
    private Boolean enableEpisodicMemory = true;
    private Boolean enableSummaryMemory = true;
}
