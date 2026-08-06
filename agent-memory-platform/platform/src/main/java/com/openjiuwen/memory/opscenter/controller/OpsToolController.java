package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.opscenter.service.OpsToolService;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/** 功能4 — 运维工具。 */
@RestController
@RequestMapping("/api/v1/ops/tools")
public class OpsToolController {

    private final OpsToolService service;
    private final TenantContextProvider tenantContextProvider;

    public OpsToolController(OpsToolService service, TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.tenantContextProvider = tenantContextProvider;
    }

    @PostMapping("/search-memory")
    public ApiResponse<List<MemoryItem>> searchMemory(@RequestBody SearchReq req) {
        return ApiResponse.ok(service.searchMemory(req.query(), req.num(), req.userId(), req.scopeId(), req.threshold()));
    }

    @PostMapping("/search-summary")
    public ApiResponse<List<MemoryItem>> searchSummary(@RequestBody SearchReq req) {
        return ApiResponse.ok(service.searchSummary(req.query(), req.num(), req.userId(), req.scopeId(), req.threshold()));
    }

    @GetMapping("/variables")
    public ApiResponse<Map<String, String>> variables(@RequestParam(name = "user_id", required = false) String userId,
                                                       @RequestParam(name = "scope_id", required = false) String scopeId,
                                                       @RequestParam(name = "names", required = false) List<String> names) {
        return ApiResponse.ok(service.viewVariables(userId, scopeId, names));
    }

    @GetMapping("/health-probe")
    public ApiResponse<Map<String, Object>> healthProbe() {
        return ApiResponse.ok(service.healthProbe());
    }

    @GetMapping("/memory-count")
    public ApiResponse<Map<String, Object>> memoryCount(@RequestParam(name = "user_id", required = false) String userId,
                                                         @RequestParam(name = "scope_id", required = false) String scopeId,
                                                         @RequestParam(name = "memory_type", required = false) String memoryType) {
        return ApiResponse.ok(service.memoryCount(userId, scopeId, memoryType));
    }

    @PostMapping("/purge-scope/preview")
    public ApiResponse<Map<String, Object>> purgePreview(@RequestBody ScopeReq req) {
        return ApiResponse.ok(service.purgeScopePreview(req.scopeId()));
    }

    @PostMapping("/purge-scope")
    public ApiResponse<Object> purgeScope(@RequestBody PurgeReq req) {
        String operator = tenantContextProvider.resolveOperator();
        return ApiResponse.ok(service.purgeScope(req.scopeId(), req.confirmToken(), operator, req.reason()));
    }

    public record SearchReq(String query, Integer num, String userId, String scopeId, Double threshold) {
    }

    public record ScopeReq(String scopeId) {
    }

    public record PurgeReq(String scopeId, String confirmToken, String reason) {
    }
}
