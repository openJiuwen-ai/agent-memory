package com.openjiuwen.memory.scopecenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
import com.openjiuwen.memory.scopecenter.dto.ScopeStatsDTO;
import com.openjiuwen.memory.scopecenter.service.ScopeRegistryService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * Scope注册管理REST API
 */
@RestController
@RequestMapping("/api/v1/scopes")
public class ScopeRegistryController {
    
    @Autowired
    private ScopeRegistryService scopeRegistryService;
    
    /**
     * 获取所有 Scope 列表（SUPER_ADMIN/PLATFORM_ADMIN/SECURITY_ADMIN）
     */
    @GetMapping
    public ApiResponse<List<ScopeRegistry>> getAllScopes() {
        List<ScopeRegistry> scopes = scopeRegistryService.getAllScopes();
        return ApiResponse.ok(scopes);
    }
    
    /**
     * 获取可分配的 Scope 列表（仅 SUPER_ADMIN）
     */
    @GetMapping("/available")
    public ApiResponse<List<ScopeRegistry>> getAvailableScopes() {
        List<ScopeRegistry> scopes = scopeRegistryService.getAvailableScopes();
        return ApiResponse.ok(scopes);
    }
    
    /**
     * 获取已分配的 Scope 列表
     */
    @GetMapping("/assigned")
    public ApiResponse<List<ScopeRegistry>> getAssignedScopes() {
        List<ScopeRegistry> scopes = scopeRegistryService.getAssignedScopes();
        return ApiResponse.ok(scopes);
    }
    
    /**
     * 根据租户 ID 获取 Scope 列表
     */
    @GetMapping("/tenant/{tenantId}")
    public ApiResponse<List<ScopeRegistry>> getScopesByTenantId(@PathVariable String tenantId) {
        List<ScopeRegistry> scopes = scopeRegistryService.getScopesByTenantId(tenantId);
        return ApiResponse.ok(scopes);
    }
    
    /**
     * 修改租户的 Scope 分配（仅 SUPER_ADMIN）
     */
    @PutMapping("/tenant/{tenantId}")
    public ApiResponse<Void> updateTenantScopes(
            @PathVariable String tenantId,
            @RequestBody Map<String, List<String>> request) {
            
        try {
            List<String> newScopeIds = request.get("scopeIds");
            List<String> oldScopeIds = request.getOrDefault("oldScopeIds", List.of());
                
            // 释放旧 Scope
            if (!oldScopeIds.isEmpty()) {
                scopeRegistryService.batchReleaseScopes(oldScopeIds);
            }
                
            // 分配新 Scope
            if (newScopeIds != null && !newScopeIds.isEmpty()) {
                scopeRegistryService.batchAssignScopesToTenant(newScopeIds, tenantId);
            }
                
            return ApiResponse.ok(null);
        } catch (Exception e) {
            return ApiResponse.fail(50000, "Scope 分配失败：" + e.getMessage());
        }
    }
    
    /**
     * 创建新 Scope（仅 SUPER_ADMIN）
     */
    @PostMapping
    public ApiResponse<ScopeRegistry> createScope(@RequestBody Map<String, String> request) {
        try {
            ScopeRegistry scope = new ScopeRegistry();
            scope.setId(UUID.randomUUID().toString().replace("-", ""));
                
            // scope_id: 如果未提供则随机生成
            String scopeId = request.get("scopeId");
            if (scopeId == null || scopeId.trim().isEmpty()) {
                scopeId = "scope_" + UUID.randomUUID().toString().substring(0, 8);
            }
            scope.setScopeId(scopeId);
                
            scope.setScopeName(request.get("scopeName"));
            scope.setDescription(request.get("description"));
            scope.setStatus("unassigned");
            scope.setCreatedAt(LocalDateTime.now());
            scope.setUpdatedAt(LocalDateTime.now());
                
            boolean success = scopeRegistryService.save(scope);
            if (success) {
                return ApiResponse.ok(scope);
            } else {
                return ApiResponse.fail(50000, "Scope 创建失败");
            }
        } catch (Exception e) {
            return ApiResponse.fail(50000, "Scope 创建失败：" + e.getMessage());
        }
    }
    
    /**
     * 更新 Scope 信息（仅 SUPER_ADMIN）
     */
    @PutMapping("/{scopeId}")
    public ApiResponse<ScopeRegistry> updateScope(
            @PathVariable String scopeId,
            @RequestBody Map<String, String> request) {
        try {
            ScopeRegistry scope = scopeRegistryService.lambdaQuery()
                    .eq(ScopeRegistry::getScopeId, scopeId)
                    .one();
                
            if (scope == null) {
                return ApiResponse.fail(40401, "Scope 不存在");
            }
                
            // 更新字段
            if (request.containsKey("scopeName")) {
                scope.setScopeName(request.get("scopeName"));
            }
            if (request.containsKey("description")) {
                scope.setDescription(request.get("description"));
            }
            scope.setUpdatedAt(LocalDateTime.now());
                
            boolean success = scopeRegistryService.updateById(scope);
            if (success) {
                return ApiResponse.ok(scope);
            } else {
                return ApiResponse.fail(50000, "Scope 更新失败");
            }
        } catch (Exception e) {
            return ApiResponse.fail(50000, "Scope 更新失败：" + e.getMessage());
        }
    }
    
    /**
     * 删除 Scope（仅 SUPER_ADMIN）
     */
    @DeleteMapping("/{scopeId}")
    public ApiResponse<Void> deleteScope(@PathVariable String scopeId) {
        try {
            ScopeRegistry scope = scopeRegistryService.lambdaQuery()
                    .eq(ScopeRegistry::getScopeId, scopeId)
                    .one();
                
            if (scope == null) {
                return ApiResponse.fail(40401, "Scope 不存在");
            }
                
            // 只允许删除未分配的 Scope
            if ("assigned".equals(scope.getStatus())) {
                return ApiResponse.fail(40900, String.format("该 Scope 已分配给租户「%s」，请先解除绑定后再删除",
                    scope.getAssignedToTenantId()));
            }
                
            boolean success = scopeRegistryService.removeById(scope.getId());
            if (success) {
                return ApiResponse.ok(null);
            } else {
                return ApiResponse.fail(50000, "Scope 删除失败");
            }
        } catch (Exception e) {
            return ApiResponse.fail(50000, "Scope 删除失败：" + e.getMessage());
        }
    }
    
    /**
     * 根据 Scope ID 获取统计信息（包含绑定租户状态）
     */
    @GetMapping("/{scopeId}/stats")
    public ApiResponse<ScopeStatsDTO> getScopeStats(@PathVariable String scopeId) {
        ScopeStatsDTO stats = scopeRegistryService.getScopeStats(scopeId);
        if (stats == null) {
            return ApiResponse.fail(40401, "Scope 不存在");
        }
        return ApiResponse.ok(stats);
    }
}
