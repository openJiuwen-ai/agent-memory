package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.opscenter.service.TraceService;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/** 功能7 — 记忆追溯。 */
@RestController
@RequestMapping("/api/v1/ops/trace")
public class TraceController {

    private final TraceService service;

    public TraceController(TraceService service) {
        this.service = service;
    }

    /** 全链路聚合 bundle。前端传入记忆字段，避免后端翻页查找。 */
    @GetMapping("/memory/{memId}")
    public ApiResponse<Map<String, Object>> bundle(@PathVariable String memId,
                                                     @RequestParam(name = "user_id", required = false) String userId,
                                                     @RequestParam(name = "scope_id", required = false) String scopeId,
                                                     @RequestParam(name = "content", required = false) String content,
                                                     @RequestParam(name = "mem_type", required = false) String memType,
                                                     @RequestParam(name = "timestamp", required = false) String timestamp,
                                                     @RequestParam(name = "source_id", required = false) String sourceId) {
        return ApiResponse.ok(service.getBundle(memId, userId, scopeId, content, memType, timestamp, sourceId));
    }

    @GetMapping("/memory/{memId}/history")
    public ApiResponse<Map<String, Object>> history(@PathVariable String memId) {
        return ApiResponse.ok(service.getHistory(memId));
    }

    @GetMapping("/memory/{memId}/audit")
    public ApiResponse<Map<String, Object>> audit(@PathVariable String memId) {
        return ApiResponse.ok(service.getAudit(memId));
    }
}
