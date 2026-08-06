package com.openjiuwen.memory.logcenter.service;

import com.openjiuwen.memory.logcenter.dto.LogStatsDTO;

import java.time.Instant;

/**
 * 日志统计仪表盘服务 — 根据日志类型返回不同维度的统计信息。
 */
public interface LogStatsService {

    /**
     * 获取日志统计数据。
     *
     * @param adminUserId 管理员ID（租户隔离，null 表示全局）
     * @param logType     日志类型: operations / runtime / messages
     * @param startTime   开始时间（可空）
     * @param endTime     结束时间（可空）
     */
    LogStatsDTO getLogStats(String adminUserId, String logType, Instant startTime, Instant endTime);
}
