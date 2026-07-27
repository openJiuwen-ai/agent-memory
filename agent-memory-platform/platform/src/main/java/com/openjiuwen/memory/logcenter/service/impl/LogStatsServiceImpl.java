package com.openjiuwen.memory.logcenter.service.impl;

import com.openjiuwen.memory.logcenter.dto.LogStatsDTO;
import com.openjiuwen.memory.logcenter.service.LogStatsService;
import com.openjiuwen.memory.logcenter.service.MessageLogService;
import com.openjiuwen.memory.logcenter.service.OperationLogService;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * 日志统计仪表盘服务实现 — 根据 logType 路由到对应日志服务的统计方法。
 * <p>
 * 2026-07-21 v5：运行日志不入库（§6.3.2），移除 runtime case + RuntimeLogService 依赖。
 * 运行日志统计由前端直接调内核 /logs/tail 获取，不再走服务层 DB 统计。
 */
@Service
public class LogStatsServiceImpl implements LogStatsService {

    private final OperationLogService operationLogService;
    private final MessageLogService messageLogService;

    public LogStatsServiceImpl(OperationLogService operationLogService,
                                MessageLogService messageLogService) {
        this.operationLogService = operationLogService;
        this.messageLogService = messageLogService;
    }

    @Override
    public LogStatsDTO getLogStats(String adminUserId, String logType, Instant startTime, Instant endTime) {
        if (logType == null || logType.isBlank()) {
            throw new IllegalArgumentException("logType must not be null or blank");
        }

        return switch (logType) {
            case "operations" -> getOperationLogStats(adminUserId, startTime, endTime);
            case "messages" -> getMessageLogStats(adminUserId, startTime, endTime);
            // 运行日志不入库（§6.3.2），不提供 DB 统计；前端通过内核 /logs/tail 瞬时查询
            case "runtime" -> throw new IllegalArgumentException(
                    "runtime logs are not stored in DB (§6.3.2), use kernel /logs/tail instead");
            default -> throw new IllegalArgumentException("Unknown log type: " + logType);
        };
    }

    private LogStatsDTO getOperationLogStats(String adminUserId, Instant start, Instant end) {
        long total = operationLogService.count(adminUserId, start, end);
        List<Map<String, Object>> byType = operationLogService.statsByType(adminUserId, start, end);
        double errorRate = operationLogService.errorRate(adminUserId, start, end);

        return LogStatsDTO.builder()
                .logType("operations")
                .total(total)
                .byDimension(byType)
                .errorRate(errorRate)
                .build();
    }

    private LogStatsDTO getMessageLogStats(String adminUserId, Instant start, Instant end) {
        long total = messageLogService.count(adminUserId, start, end);
        long memoryGenerated = messageLogService.countMemoryGenerated(adminUserId, start, end);
        double avgResponseTime = messageLogService.avgResponseTime(adminUserId, start, end);
        List<Map<String, Object>> byUser = messageLogService.statsByUser(adminUserId, start, end);

        return LogStatsDTO.builder()
                .logType("messages")
                .total(total)
                .byDimension(byUser)
                .avgResponseTimeMs(avgResponseTime)
                .memoryGeneratedCount(memoryGenerated)
                .build();
    }
}
