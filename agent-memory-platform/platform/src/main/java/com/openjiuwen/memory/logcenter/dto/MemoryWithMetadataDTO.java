package com.openjiuwen.memory.logcenter.dto;

import lombok.Builder;
import lombok.Data;

import java.util.Map;

/**
 * 记忆元数据 DTO（V3-DEFECT-058 修复）。
 */
@Data
@Builder
public class MemoryWithMetadataDTO {
    /** 消息 ID */
    private String messageId;
    
    /** 所属用户 ID */
    private String userId;
    
    /** 所属 Scope ID */
    private String scopeId;
    
    /** 会话 ID */
    private String sessionId;
    
    /** 角色（user/assistant） */
    private String role;
    
    /** 内容 */
    private String content;
    
    /** 时间戳 */
    private String timestamp;
}
