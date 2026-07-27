package com.openjiuwen.memory.scopecenter.controller;

import com.openjiuwen.memory.common.CommonResult;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
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
     * 获取所有Scope列表（SUPER_ADMIN/PLATFORM_ADMIN/SECURITY_ADMIN）
     */
    @GetMapping
    public CommonResult<List<ScopeRegistry>> getAllScopes() {
        List<ScopeRegistry> scopes = scopeRegistryService.getAllScopes();
        return CommonResult.success(scopes);
    }
    
    /**
     * 获取可分配的Scope列表（仅SUPER_ADMIN）
     */
    @GetMapping("/available")
    public CommonResult<List<ScopeRegistry>> getAvailableScopes() {
        List<ScopeRegistry> scopes = scopeRegistryService.getAvailableScopes();
        return CommonResult.success(scopes);
    }
    
    /**
     * 获取已分配的Scope列表
     */
    @GetMapping("/assigned")
    public CommonResult<List<ScopeRegistry>> getAssignedScopes() {
        List<ScopeRegistry> scopes = scopeRegistryService.getAssignedScopes();
        return CommonResult.success(scopes);
    }
    
    /**
     * 根据租户ID获取Scope列表
     */
    @GetMapping("/tenant/{tenantId}")
    public CommonResult<List<ScopeRegistry>> getScopesByTenantId(@PathVariable String tenantId) {
        List<ScopeRegistry> scopes = scopeRegistryService.getScopesByTenantId(tenantId);
        return CommonResult.success(scopes);
    }
    
    /**
     * 修改租户的Scope分配（仅SUPER_ADMIN）
     */
    @PutMapping("/tenant/{tenantId}")
    public CommonResult<Void> updateTenantScopes(
            @PathVariable String tenantId,
            @RequestBody Map<String, List<String>> request) {
        
        List<String> newScopeIds = request.get("scopeIds");
        List<String> oldScopeIds = request.getOrDefault("oldScopeIds", List.of());
        
        try {
            // 释放旧Scope
            if (!oldScopeIds.isEmpty()) {
                scopeRegistryService.batchReleaseScopes(oldScopeIds);
            }
            
            // 分配新Scope
            if (newScopeIds != null && !newScopeIds.isEmpty()) {
                scopeRegistryService.batchAssignScopesToTenant(newScopeIds, tenantId);
            }
            
            return CommonResult.success();
        } catch (Exception e) {
            return CommonResult.error("Scope分配失败：" + e.getMessage());
        }
    }
    
    /**
     * 创建新Scope（仅SUPER_ADMIN）
     */
    @PostMapping
    public CommonResult<ScopeRegistry> createScope(@RequestBody Map<String, String> request) {
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
                return CommonResult.success(scope);
            } else {
                return CommonResult.error("Scope创建失败");
            }
        } catch (Exception e) {
            return CommonResult.error("Scope创建失败：" + e.getMessage());
        }
    }
    
    /**
     * 更新Scope信息（仅SUPER_ADMIN）
     */
    @PutMapping("/{scopeId}")
    public CommonResult<ScopeRegistry> updateScope(
            @PathVariable String scopeId,
            @RequestBody Map<String, String> request) {
        try {
            ScopeRegistry scope = scopeRegistryService.lambdaQuery()
                    .eq(ScopeRegistry::getScopeId, scopeId)
                    .one();
            
            if (scope == null) {
                return CommonResult.error("Scope不存在");
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
                return CommonResult.success(scope);
            } else {
                return CommonResult.error("Scope更新失败");
            }
        } catch (Exception e) {
            return CommonResult.error("Scope更新失败：" + e.getMessage());
        }
    }
    
    /**
     * 删除Scope（仅SUPER_ADMIN）
     */
    @DeleteMapping("/{scopeId}")
    public CommonResult<Void> deleteScope(@PathVariable String scopeId) {
        try {
            ScopeRegistry scope = scopeRegistryService.lambdaQuery()
                    .eq(ScopeRegistry::getScopeId, scopeId)
                    .one();
            
            if (scope == null) {
                return CommonResult.error("Scope不存在");
            }
            
            // 只允许删除未分配的Scope
            if ("assigned".equals(scope.getStatus())) {
                return CommonResult.error("已分配的Scope不能删除");
            }
            
            boolean success = scopeRegistryService.removeById(scope.getId());
            if (success) {
                return CommonResult.success();
            } else {
                return CommonResult.error("Scope删除失败");
            }
        } catch (Exception e) {
            return CommonResult.error("Scope删除失败：" + e.getMessage());
        }
    }
}
