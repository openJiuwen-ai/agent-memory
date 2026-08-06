package com.openjiuwen.memory.logcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.logcenter.dto.LogStatsDTO;
import com.openjiuwen.memory.logcenter.dto.MessageRoleStatsDTO;
import com.openjiuwen.memory.logcenter.service.LogStatsService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 日志统计仪表盘控制器。
 * <p>
 * 根据日志类型返回不同维度的统计信息，供前端仪表盘展示。
 */
@RestController
@RequestMapping("/api/v1/logs/stats")
public class LogStatsController {

    private final LogStatsService logStatsService;
    private final PermissionChecker permissionChecker;
    private final MemoryEngineClient memoryEngineClient;

    @Autowired
    public LogStatsController(LogStatsService logStatsService,
                             PermissionChecker permissionChecker,
                             MemoryEngineClient memoryEngineClient) {
        this.logStatsService = logStatsService;
        this.permissionChecker = permissionChecker;
        this.memoryEngineClient = memoryEngineClient;
    }

    /**
     * 获取日志统计数据。
     *
     * @param adminUserId 管理员 ID（租户隔离，可空表示全局）
     * @param logType     日志类型：operations / runtime / messages
     * @param startTime   开始时间（ISO 8601，可空）
     * @param endTime     结束时间（ISO 8601，可空）
     */
    @GetMapping
    public ApiResponse<LogStatsDTO> getStats(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam("log_type") String logType,
            @RequestParam(name = "start", required = false) String startTime,
            @RequestParam(name = "end", required = false) String endTime) {
        // P1-4 Fix: 补齐权限检查，与其他日志中心 Controller 保持一致（log:read 权限码）
        permissionChecker.require("log:read");

        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);

        LogStatsDTO stats = logStatsService.getLogStats(adminUserId, logType, start, end);
        return ApiResponse.ok(stats);
    }

    /**
     * V3-DEFECT-059: 按角色统计用户消息数量
     */
    @GetMapping("/messages/stats/by-role")
    public ApiResponse<MessageRoleStatsDTO> countMessagesByRole(
            @RequestParam(name = "user_id", required = false) String userId,
            @RequestParam(name = "scope_id", required = false) String scopeId,
            @RequestParam(name = "session_id", required = false) String sessionId) {
        
        permissionChecker.require("log:read");
        
        // 通过内核 API 获取统计数据
        Map<String, Object> statsMap = memoryEngineClient.countMessagesByRole(userId, scopeId, sessionId);
        
        if (statsMap == null || statsMap.isEmpty()) {
            return ApiResponse.fail(50000, "无法获取消息统计数据");
        }
        
        MessageRoleStatsDTO stats = MessageRoleStatsDTO.builder()
                .byRole(new LinkedHashMap<>((Map<String, Long>) statsMap.getOrDefault("by_role", new LinkedHashMap<>())))
                .total(Long.valueOf(String.valueOf(statsMap.getOrDefault("total", 0L))))
                .build();
        
        return ApiResponse.ok(stats);
    }
    
    private Instant parseInstant(String iso) {
        if (iso == null || iso.isBlank()) {
            return null;
        }
        try {
            return Instant.parse(iso);
        } catch (Exception e) {
            return null;
        }
    }
}
