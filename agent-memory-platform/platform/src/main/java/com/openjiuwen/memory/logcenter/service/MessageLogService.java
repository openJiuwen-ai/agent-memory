package com.openjiuwen.memory.logcenter.service;

import com.baomidou.mybatisplus.core.metadata.IPage;
import com.openjiuwen.memory.logcenter.domain.MessageLogEntity;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * 用户消息日志服务 — 查询用户API调用的消息轨迹、统计。
 */
public interface MessageLogService {

    /**
     * 分页查询用户消息日志。
     */
    IPage<MessageLogEntity> queryLogs(String adminUserId, String userId, String scopeId,
                                      Boolean successOnly, Instant startTime, Instant endTime,
                                      int page, int size);

    /**
     * 按用户统计消息数量。
     */
    List<Map<String, Object>> statsByUser(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 按Scope统计消息数量。
     */
    List<Map<String, Object>> statsByScope(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 统计总消息日志数。
     */
    long count(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 统计生成记忆的消息数。
     */
    long countMemoryGenerated(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 统计平均响应时间。
     */
    double avgResponseTime(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 导出消息日志为 CSV（§6.4.3 L2）。
     * 按过滤条件查询全量记录（不分页），转成 CSV 字符串（UTF-8 BOM）。
     */
    String exportToCsv(String adminUserId, String userId, String scopeId,
                       Boolean successOnly, Instant startTime, Instant endTime);
}
