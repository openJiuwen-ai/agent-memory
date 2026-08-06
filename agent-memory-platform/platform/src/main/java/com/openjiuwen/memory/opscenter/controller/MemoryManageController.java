package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.client.dto.MemVariable;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.opscenter.service.MemoryManageService;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/** 功能5 — 记忆服务列表管理（含用户变量，变量属记忆内容）。 */
@RestController
@RequestMapping("/api/v1/ops/memory")
public class MemoryManageController {

    private final MemoryManageService service;
    private final TenantContextProvider tenantContextProvider;

    public MemoryManageController(MemoryManageService service,
                                   TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.tenantContextProvider = tenantContextProvider;
    }

    @GetMapping
    public ApiResponse<PageResult<MemoryItem>> list(@RequestParam(name = "user_id", required = false) String userId,
                                                     @RequestParam(name = "scope_id", required = false) String scopeId,
                                                     @RequestParam(name = "memory_type", required = false) String memoryType,
                                                     @RequestParam(name = "page_idx", defaultValue = "1") int pageIdx,
                                                     @RequestParam(name = "page_size", defaultValue = "20") int pageSize) {
        return ApiResponse.ok(service.list(userId, scopeId, memoryType, pageIdx, pageSize));
    }

    @GetMapping("/{memId}")
    public ApiResponse<MemoryItem> detail(@PathVariable String memId,
                                          @RequestParam(name = "user_id", required = false) String userId,
                                          @RequestParam(name = "scope_id", required = false) String scopeId) {
        return ApiResponse.ok(service.detail(memId, userId, scopeId));
    }

    @PostMapping
    public ApiResponse<Object> create(@RequestBody CreateReq req) {
        return ApiResponse.ok(service.create(req.userId(), req.scopeId(), req.messages(),
                req.memVariables(), tenantContextProvider.resolveOperator(), req.reason()));
    }

    @PutMapping("/{memId}")
    public ApiResponse<Object> update(@PathVariable String memId, @RequestBody UpdateReq req) {
        return ApiResponse.ok(service.update(memId, req.memory(), req.oldContent(), req.userId(), req.scopeId(), tenantContextProvider.resolveOperator(), req.reason()));
    }

    @DeleteMapping("/{memId}")
    public ApiResponse<Object> deleteOne(@PathVariable String memId,
                                         @RequestParam(name = "user_id", required = false) String userId,
                                         @RequestParam(name = "scope_id", required = false) String scopeId,
                                         @RequestParam(name = "reason", required = false) String reason,
                                         @RequestParam(name = "old_content", required = false) String oldContent) {
        // ⚠️ 缺口：:8516 未暴露 delete_mem_by_id → 抛 GapException(50010)
        return ApiResponse.ok(service.deleteOne(memId, userId, scopeId, oldContent, tenantContextProvider.resolveOperator(), reason));
    }

    @DeleteMapping
    public ApiResponse<Object> deleteByScope(@RequestParam(name = "scope_id") String scopeId,
                                             @RequestParam(name = "confirm_token", required = false) String confirmToken,
                                             @RequestParam(name = "reason", required = false) String reason) {
        return ApiResponse.ok(service.deleteByScope(scopeId, confirmToken, tenantContextProvider.resolveOperator(), reason));
    }

    @PostMapping("/batch-delete")
    public ApiResponse<Object> batchDelete(@RequestBody BatchDeleteReq req) {
        return ApiResponse.ok(service.batchDelete(req.memIds(), req.userId(), req.scopeId(), tenantContextProvider.resolveOperator(), req.reason()));
    }

    // —— 变量（记忆内容） ——
    @GetMapping("/variables")
    public ApiResponse<Map<String, String>> getVariables(@RequestParam(name = "user_id", required = false) String userId,
                                                          @RequestParam(name = "scope_id", required = false) String scopeId,
                                                          @RequestParam(name = "names", required = false) List<String> names) {
        return ApiResponse.ok(service.getVariables(userId, scopeId, names));
    }

    @PutMapping("/variables")
    public ApiResponse<Object> updateVariables(@RequestBody VariablesReq req) {
        return ApiResponse.ok(service.updateVariables(req.userId(), req.scopeId(), req.variables(), tenantContextProvider.resolveOperator()));
    }

    @DeleteMapping("/variables")
    public ApiResponse<Object> deleteVariables(@RequestBody VariablesDeleteReq req) {
        return ApiResponse.ok(service.deleteVariables(req.userId(), req.scopeId(), req.names(), tenantContextProvider.resolveOperator()));
    }

    public record CreateReq(String userId, String scopeId, List<Map<String, String>> messages,
                            List<MemVariable> memVariables, String reason) {
    }

    public record UpdateReq(String memory, String oldContent, String userId, String scopeId, String reason) {
    }

    public record VariablesReq(String userId, String scopeId, Map<String, String> variables) {
    }

    public record VariablesDeleteReq(String userId, String scopeId, List<String> names) {
    }

    public record BatchDeleteReq(List<String> memIds, String userId, String scopeId, String reason) {
    }
}
