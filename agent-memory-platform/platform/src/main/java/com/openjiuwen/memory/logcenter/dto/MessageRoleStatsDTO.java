package com.openjiuwen.memory.logcenter.dto;

import lombok.Builder;
import lombok.Data;

import java.util.Map;

/**
 * 消息角色统计 DTO（V3-DEFECT-059 修复）。
 */
@Data
@Builder
public class MessageRoleStatsDTO {
    /** 按角色统计的数量 {user: N, assistant: N} */
    private Map<String, Long> byRole;
    
    /** 总消息数 */
    private Long total;
}
