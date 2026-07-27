package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.opscenter.service.GovernanceService;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/** 功能6 — 记忆治理。 */
@RestController
public class GovernanceController {

    private final GovernanceService service;
    private final TenantContextProvider tenantContextProvider;

    public GovernanceController(GovernanceService service,
                                 TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.tenantContextProvider = tenantContextProvider;
    }

    /** 组装四类策略 + 配额。 */
    @GetMapping("/api/v1/ops/governance/strategy")
    public ApiResponse<Map<String, Object>> getStrategy() {
        return ApiResponse.ok(service.getStrategy(tenantContextProvider.resolveTenant()));
    }

    /** 保存四类策略 + 配额。 */
    @PutMapping("/api/v1/ops/governance/strategy")
    public ApiResponse<Void> saveStrategy(@RequestBody Map<String, Object> strategy) {
        service.saveStrategy(tenantContextProvider.resolveTenant(), strategy, tenantContextProvider.resolveOperator());
        return ApiResponse.ok();
    }

    /** 质量扫描：duplicate/stale/empty。 */
    @PostMapping("/api/v1/ops/governance/scan")
    public ApiResponse<Map<String, Object>> scan(@RequestBody ScanReq req,
                                                  @RequestParam(name = "user_id", required = false) String userId,
                                                  @RequestParam(name = "scope_id", required = false) String scopeId) {
        return ApiResponse.ok(service.scan(req.scanType(), userId, scopeId, req.threshold()));
    }

    /** 合规扫描；auto_fix=true 清理违规变量。 */
    @PostMapping("/api/v1/ops/governance/compliance")
    public ApiResponse<Map<String, Object>> compliance(@RequestBody ComplianceReq req,
                                                        @RequestParam(name = "user_id", required = false) String userId,
                                                        @RequestParam(name = "scope_id", required = false) String scopeId) {
        return ApiResponse.ok(service.compliance(userId, scopeId, req.autoFix()));
    }

    /** 治理页聚合 bundle。 */
    @GetMapping("/api/v1/ui/governance/page")
    public ApiResponse<Map<String, Object>> page(@RequestParam(name = "user_id", required = false) String userId,
                                                  @RequestParam(name = "scope_id", required = false) String scopeId) {
        return ApiResponse.ok(service.getGovernancePage(tenantContextProvider.resolveTenant(), userId, scopeId));
    }

    public record ScanReq(String scanType, Double threshold) {
    }

    public record ComplianceReq(Boolean autoFix) {
    }
}
