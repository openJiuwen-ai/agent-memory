package com.openjiuwen.memory.logcenter.domain;

import lombok.Data;

import java.time.Instant;

/**
 * 用户消息日志（V3 §6.6 重构）— 非持久化 POJO，不对应任何数据库表。
 * 数据源为内核 user_message 表，经 KR-MSG-01~04（/admin/messages/*）拉取后由
 * MessageLogServiceImpl#toEntity 组装。仅保留内核可提供的字段。
 */
@Data
public class MessageLogEntity {

    private String id;

    /** 请求唯一ID */
    private String requestId;
    private String adminUserId;
    private String userId;
    private String scopeName;
    private String apiPath;
    private String apiMethod;
    /** 消息数量(add_messages时) */
    private Integer messageCount;
    /** 消息角色列表(JSON字符串) */
    private String messageRoles;
    private String errorMessage;
    private Instant createdAt;
}
