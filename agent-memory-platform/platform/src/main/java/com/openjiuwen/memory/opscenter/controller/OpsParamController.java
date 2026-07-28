package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.opscenter.service.OpsParamService;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/** 功能2 — 运维参数配置（系统自身全局参数）。 */
@RestController
@RequestMapping("/api/v1/ops/params")
public class OpsParamController {

    private final OpsParamService service;
    private final TenantContextProvider tenantContextProvider;

    public OpsParamController(OpsParamService service, TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.tenantContextProvider = tenantContextProvider;
    }

    @GetMapping
    public ApiResponse<Map<String, Object>> overview() {
        return ApiResponse.ok(service.overview());
    }

    @GetMapping("/{category}")
    public ApiResponse<Map<String, Object>> get(@PathVariable String category,
                                                @RequestParam(name = "scope_id", required = false) String scopeId) {
        return ApiResponse.ok(service.get(category, scopeId));
    }

    @PutMapping("/{category}")
    public ApiResponse<Map<String, Object>> update(@PathVariable String category,
                                                    @RequestParam(name = "scope_id", required = false) String scopeId,
                                                    @RequestBody Map<String, Object> value) {
        String operator = tenantContextProvider.resolveOperator();
        return ApiResponse.ok(service.update(category, scopeId, value, operator));
    }

    @PostMapping("/draft/save")
    public ApiResponse<Void> saveDraft(@RequestBody DraftRequest req) {
        String operator = tenantContextProvider.resolveOperator();
        service.saveDraft(req.category(), req.scopeId(), req.value(), operator);
        return ApiResponse.ok();
    }

    public record DraftRequest(String category, String scopeId, Map<String, Object> value) {
    }
}
