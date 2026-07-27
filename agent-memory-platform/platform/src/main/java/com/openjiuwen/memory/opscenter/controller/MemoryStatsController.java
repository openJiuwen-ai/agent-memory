package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.opscenter.dto.MemoryStatsDTO;
import com.openjiuwen.memory.opscenter.service.MemoryStatsService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 记忆统计仪表盘（§7.5.2）。
 * <p>
 * GET /api/v1/ops/memory/stats — 聚合返回按类型/Scope统计、增长趋势、Top用户、存储用量。
 */
@RestController
@RequestMapping("/api/v1/ops/memory")
public class MemoryStatsController {

    private final MemoryStatsService statsService;

    public MemoryStatsController(MemoryStatsService statsService) {
        this.statsService = statsService;
    }

    @GetMapping("/stats")
    public ApiResponse<MemoryStatsDTO> getStats(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "scope_id", required = false) String scopeId,
            @RequestParam(name = "user_id", required = false) String userId) {
        return ApiResponse.ok(statsService.getMemoryStats(adminUserId, scopeId, userId));
    }
}
