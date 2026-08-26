package com.openjiuwen.memory.scopecenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.util.ScopeIdValidator;
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
            // V3-DEFECT-003 修复：检查 scope_id 唯一性
            String scopeId = request.get("scopeId");
            if (scopeId == null || scopeId.trim().isEmpty()) {
                scopeId = "scope_" + UUID.randomUUID().toString().substring(0, 8);
            }

            // scope_id 格式校验（与内核 _validate_id 规则一致）
            ScopeIdValidator.validate(scopeId);

            // 检查是否已存在
            if (scopeRegistryService.existsByScopeId(scopeId)) {
                return ApiResponse.fail(40900, String.format("Scope '%s' 已存在，请勿重复注册", scopeId));
            }

            ScopeRegistry scope = new ScopeRegistry();
            scope.setId(UUID.randomUUID().toString().replace("-", ""));
            scope.setScopeId(scopeId);

            scope.setScopeName(request.get("scopeName"));
            scope.setDescription(request.get("description"));
            
            // V3-DEFECT-004 修复：生成并存储 scope_key（仅本次明文返回）
            String scopeKey = generateScopeKey();
            scope.setScopeKey(scopeKey);
            
            // V3-DEFECT-074 修复：max_memories 负数校验
            String maxMemoriesStr = request.get("maxMemories");
            Integer maxMemories = 0; // 默认不限（0）
            if (maxMemoriesStr != null && !maxMemoriesStr.trim().isEmpty()) {
                try {
                    maxMemories = Integer.parseInt(maxMemoriesStr);
                    if (maxMemories < 0) {
                        return ApiResponse.fail(40000, "max_memories 不能为负数，请输入>=0 的值或留空表示不限");
                    }
                } catch (NumberFormatException e) {
                    return ApiResponse.fail(40000, "max_memories 必须是有效的整数");
                }
            }
            scope.setMaxMemories(maxMemories);
            
            // V3-DEFECT-075 修复：禁止注册__default__Scope
            if ("__default__".equals(scopeId)) {
                return ApiResponse.fail(40001, "scope_id 不能为'__default__'，该保留名称由内核系统管理");
            }
            
            scope.setStatus("unassigned");
            scope.setCreatedAt(LocalDateTime.now());
            scope.setUpdatedAt(LocalDateTime.now());

            boolean success = scopeRegistryService.save(scope);
            if (success) {
                // 明文 scope_key 仅本次响应返回
                return ApiResponse.ok(scope);
            } else {
                return ApiResponse.fail(50000, "Scope 创建失败");
            }
        } catch (org.springframework.dao.DuplicateKeyException e) {
            return ApiResponse.fail(409, "scope_id 已存在");
        } catch (Exception e) {
            return ApiResponse.fail(50000, "Scope 创建失败：" + e.getMessage());
        }
    }
    
    /**
     * 生成 Scope Key（随机字符串）
     */
    private String generateScopeKey() {
        return "sk_" + UUID.randomUUID().toString().replace("-", "") + 
               "_" + System.currentTimeMillis();
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

            // max_memories 校验（如果提供，内核 KR-SCOPE-02 配额）
            if (request.containsKey("max_memories")) {
                try {
                    int maxMemories = Integer.parseInt(request.get("max_memories"));
                    if (maxMemories < 0) {
                        return ApiResponse.fail(422, "max_memories 不能为负数");
                    }
                } catch (NumberFormatException e) {
                    return ApiResponse.fail(422, "max_memories 格式无效，必须为非负整数");
                }
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
    
    /**
     * V3-DEFECT-008 修复：获取 Scope 配额使用情况
     */
    @GetMapping("/{scopeId}/quota")
    public ApiResponse<Map<String, Object>> getScopeQuota(@PathVariable String scopeId) {
        try {
            ScopeRegistry scope = scopeRegistryService.getByScopeId(scopeId);
            if (scope == null) {
                return ApiResponse.fail(40401, "Scope 不存在");
            }
            
            // 从后端表获取配额信息
            int maxMemories = scope.getMaxMemories() != null ? scope.getMaxMemories() : 0;
            
            // 当前暂时返回 0，因为后端不直接管理记忆数量
            int used = 0;
            
            double usagePercent = 0.0;
            if (maxMemories > 0) {
                usagePercent = ((double) used) / maxMemories * 100;
            }
            
            Map<String, Object> quotaInfo = Map.of(
                "scopeId", scopeId,
                "used", used,
                "max", maxMemories,
                "usagePercent", usagePercent
            );
            
            return ApiResponse.ok(quotaInfo);
        } catch (Exception e) {
            return ApiResponse.fail(50000, "查询配额失败：" + e.getMessage());
        }
    }
}
